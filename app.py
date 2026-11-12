"""경기대 수원캠퍼스 점심 추천기.  실행: streamlit run app.py --server.address 0.0.0.0"""
import base64
import datetime
import hmac
import json
import os
import random
import re
import secrets
import socket
import urllib.parse
from pathlib import Path
from time import time as epoch

import store

DIR = Path(__file__).parent
DATA = json.loads((DIR / "data.json").read_text("utf-8"))
STATE_FILE = Path(os.environ.get("LUNCH_STATE") or DIR / "state.json")  # 테스트할 땐 다른 파일로


# ---------- 순수 로직 (test_app.py에서 검사) ----------
CATS = ["학식", "한식", "분식", "중식", "일식·아시안", "양식", "패스트푸드", "기타"]  # crawl.py KAKAO_CAT과 맞춤


def filter_menus(restaurants, cats=None, lo=0, hi=10**9, where=None):
    # 가격 미확인(None) 메뉴는 가격 필터에서 빼지 않고 보여줌
    return [(r, m) for r in restaurants for m in r["menus"]
            if (cats is None or r["category"] in cats)
            and (where is None or r["where"] == where)
            and (m["price"] is None or lo <= m["price"] <= hi)]


def won(p):
    return "가격 미확인" if p is None else f"{p:,}원"


def pick_list(rs, cat="전체", where="전체", budget=None):
    """고르기 목록 [(식당, 예산 안 식사 메뉴들)]. 예산 안 메뉴가 없으면 빠짐(가격 미확인 메뉴는 남김).
    최저가 싼 순 → 가격 모르는 곳은 뒤로."""
    out = []
    for r in rs:
        if cat != "전체" and r["category"] != cat:
            continue
        if where == "교내" and r["where"] != "교내" or where in ("정문", "후문") and r.get("gate") != where:
            continue
        ms = [m for m in r["menus"] if (m.get("type") or "") not in NOT_MEAL
              and (m["price"] is None or budget is None or m["price"] <= budget)]
        if ms:
            out.append((r, ms))
    low = lambda rm: min((m["price"] for m in rm[1] if m["price"]), default=None)
    return sorted(out, key=lambda rm: (low(rm) is None, low(rm) or 0, rm[0]["name"]))


DELIVERY = DATA.get("delivery", {}).get("restaurants", [])  # crawl.py가 모은 경기대 3km 안 음식점


def delivery_list(rs, cat="전체", max_m=3000):
    """배달 고르기: 종류·거리로 거른 음식점 (가까운 순 그대로)."""
    return [r for r in rs if (cat == "전체" or r["category"] == cat) and r["dist"] <= max_m]


def word_roller(words, hold=2.2, move=0.4):
    """제목 단어 교체 연출: words를 차례로 위로 밀어 올리며 바꿔 보여주는 HTML+CSS (자바스크립트 없음).
    첫 단어를 끝에 한 번 더 붙여 이음매 없이 반복. 움직임 줄이기 설정이면 첫 단어만."""
    from html import escape as e
    seq = list(words) + [words[0]]
    step = hold + move
    total = step * len(words)
    frames = []
    for k in range(len(words)):
        frames.append(f"{k * step / total * 100:.2f}%,{(k * step + hold) / total * 100:.2f}%{{transform:translateY(-{k * 1.3:.2f}em)}}")
    frames.append(f"100%{{transform:translateY(-{len(words) * 1.3:.2f}em)}}")
    return (f'<style>@keyframes wroll{{{"".join(frames)}}}'
            f'.wroll{{display:inline-block;height:1.3em;line-height:1.3em;overflow:hidden;vertical-align:bottom}}'
            f'.wroll>span{{display:block;animation:wroll {total:.1f}s ease-in-out infinite}}'
            f'.wroll i{{display:block;font-style:normal;color:var(--brand)}}.wroll i:first-child{{color:inherit}}'
            f'@media (prefers-reduced-motion:reduce){{.wroll>span{{animation:none}}}}</style>'
            f'<span class="wroll" aria-label="{e(words[0])}"><span>' + "".join(f"<i>{e(w)}</i>" for w in seq) + "</span></span>")


def price_range(menus):
    ps = sorted(m["price"] for m in menus if m["price"] is not None)
    if not ps:
        return "가격 미확인"
    return won(ps[0]) if ps[0] == ps[-1] else f"{ps[0]:,}~{won(ps[-1])}"


# (식당, 메뉴) → 주간 식단에서 오늘 반찬을 붙일 곳
TODAY_MENUS = {("감성코어 식당", "한식 백반"): ("weekly", None),
               ("경기드림타워(기숙사) 식당", "점심 백반"): ("dorm_weekly", "점심"),
               ("경기드림타워(기숙사) 식당", "저녁 백반"): ("dorm_weekly", "저녁")}


def with_today(rs, data, day):
    """감성코어·기숙사 백반 메뉴 이름에 오늘 식단을 붙인 사본 (예: '한식 백반 (오늘: 콩나물국, 김치제육볶음…)')."""
    def tag(r, m):
        src = TODAY_MENUS.get((r["name"], m["name"]))
        d = src and data.get(src[0], {}).get("days", {}).get(day)
        d = d and (d.get(src[1]) if src[1] else d)
        if not d:
            return m
        main = [x for x in d if x != "쌀밥"][:3]
        return m | {"name": f"{m['name']} (오늘: {', '.join(main)}…)", "dishes": list(d)}  # 재료 거르기용 전체 반찬
    return [r | {"menus": [tag(r, m) for m in r["menus"]]} for r in rs]


CAT_COLORS = {"학식": "#e8453c", "한식": "#f59f00", "분식": "#e64980", "중식": "#7048e8", "일식·아시안": "#1c7ed6",
              "양식": "#0ca678", "패스트푸드": "#868e96", "기타": "#495057"}
GEO_FILE = DIR / "campus_geo.json"  # OSM에서 받은 캠퍼스 외곽선·건물 (정적)


def label(r):
    """화면 표시용 이름: 푸드코트 매장은 '경슐랭 · 위델가'."""
    return f"{r['court']} · {r['name']}" if r.get("court") else r["name"]


def by_type(menus):
    """메뉴를 종류(덮밥·밥류, 면류…)별로 묶음. 처음 나온 순서 유지, 종류 없으면 '메뉴'."""
    out = {}
    for m in menus:
        out.setdefault(m.get("type") or "메뉴", []).append(m)
    return out


def place_groups(rs):
    """지도 핀·자세히 보기 단위: 푸드코트 매장들은 한 곳으로 묶음 → [(장소 이름, [식당...])]"""
    out = {}
    for r in rs:
        out.setdefault(r.get("court") or r["name"], []).append(r)
    return list(out.items())


def map_html(rs, geo, data, visits=None, focus=None):
    """캠퍼스 모양(외곽선+건물) 위에 식당 핀. 핀 클릭 → 팝업에 메뉴 전체·가격·길찾기.
    푸드코트는 핀 하나에 매장별 접이식 칸. focus 장소(식당 또는 푸드코트 이름)는 팝업을 열고 시작."""
    from html import escape as e
    visits = visits or {}

    def table(menus):
        groups = by_type(menus)
        rows = []
        for t, ms in groups.items():
            if len(groups) > 1:
                rows.append(f"<tr class=g><td colspan=2>{e(t)}</td></tr>")
            rows += [f"<tr><td>{e(m['name'])}</td><td class=p>{won(m['price'])}</td></tr>" for m in ms]
        return f"<div class=mt><table>{''.join(rows)}</table></div>"

    pins = []
    for place, members in place_groups(rs):
        r = members[0]
        court = bool(r.get("court"))
        info = "".join(f"<div class=i><b>{e(k)}</b> {e(v)}</div>" for k, v in (r.get("info") or {}).items())
        allm = [m for x in members for m in x["menus"]]
        head = (f"<div class=t>{e(place)}{' 푸드코트' if court else ''}</div>"
                f"<div class=s>{e(r['category'])} · {e(r.get('gate') or r['where'])} · {price_range(allm)}"
                + (f" · 매장 {len(members)}곳" if court else f" · 누적 {visits.get(r['name'], 0)}명") + f"</div>{info}")
        if court:
            body = "".join(f"<details><summary><b>{e(x['name'])}</b> · {price_range(x['menus'])} · {len(x['menus'])}개"
                           f" · 누적 {visits.get(x['name'], 0)}명</summary>{table(x['menus'])}</details>" for x in members)
        else:
            body = table(r["menus"])
        pop = (f"<div class=pp>{head}{body}<div class=f>{e(r.get('price_source', ''))}</div>"
               f"<a href=\"{e(route_url(r))}\" target=_blank rel=noopener>카카오맵 길찾기</a></div>")
        pins.append({"n": place, "c": r["category"], "lat": r["lat"], "lng": r["lng"], "in": r["where"] == "교내",
                     "pop": pop})
    payload = {"pins": pins, "campus": geo.get("campus") or [], "buildings": geo.get("buildings") or [],
               "gates": data["campus"].get("gates", []), "center": [data["campus"]["lat"], data["campus"]["lng"]],
               "colors": CAT_COLORS, "focus": focus}
    js = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")  # </script> 끊김 방지
    return """<!doctype html><html><head><meta charset=utf-8>
<link rel=stylesheet href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>html,body,#m{height:100%;margin:0;font-family:system-ui,'Malgun Gothic',sans-serif}
.leaflet-tile-pane{opacity:.45}.pp{min-width:200px;max-width:260px}.t{font-weight:700;font-size:15px}
.s{color:#666;font-size:12px;margin:2px 0 6px}.i{font-size:11px;color:#444}.mt{max-height:190px;overflow:auto;margin:6px 0;border-top:1px solid #eee}
.mt table{width:100%;border-collapse:collapse;font-size:12px}.mt td{padding:2px 0;border-bottom:1px solid #f1f1f1}
.mt td.p{text-align:right;white-space:nowrap;padding-left:8px}.mt tr.g td{font-weight:700;color:#c92a2a;padding-top:6px}
details{border-top:1px solid #eee;padding:3px 0}summary{cursor:pointer;font-size:12px}details .mt{max-height:170px}.f{font-size:10px;color:#999;margin-bottom:4px}
.bl{background:none;border:none;box-shadow:none;font-size:10px;color:#333;font-weight:600;text-shadow:0 0 3px #fff}
.gt{background:#222;color:#fff;border:none;font-weight:700}
.lg i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}</style></head>
<body><div id=m></div><script>
const D=""" + js + """;
const map=L.map('m',{zoomSnap:.5}).setView(D.center,16);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,
  attribution:'© OpenStreetMap contributors'}).addTo(map);
if(D.campus.length) L.polygon(D.campus,{color:'#c92a2a',weight:2,fillColor:'#ffe3e3',fillOpacity:.55}).addTo(map);
const caf=['감성코어','E-스퀘어','복지관','경기드림타워','학생회관'];
D.buildings.forEach(b=>{const hot=caf.includes(b.name);
  const p=L.polygon(b.pts,{color:hot?'#c92a2a':'#868e96',weight:1,fillColor:hot?'#ffa8a8':'#dee2e6',fillOpacity:.9}).addTo(map);
  if(b.name) p.bindTooltip(b.name.replace('경기대학교 ',''),{permanent:hot,direction:'center',className:'bl'});});
D.gates.forEach(g=>L.marker([g.lat,g.lng],{icon:L.divIcon({className:'',html:''})}).addTo(map)
  .bindTooltip(g.name,{permanent:true,direction:'top',className:'gt'}));
const groups={},byName={};
D.pins.forEach(p=>{const col=D.colors[p.c]||'#333';
  const mk=L.circleMarker([p.lat,p.lng],{radius:p.in?9:7,color:'#fff',weight:2,fillColor:col,fillOpacity:.95})
    .bindPopup(p.pop,{maxWidth:280}).bindTooltip(p.n);
  (groups[p.c]=groups[p.c]||L.layerGroup().addTo(map)).addLayer(mk); byName[p.n]=mk;});
const ov={};Object.keys(D.colors).forEach(c=>{if(groups[c])ov[`<span class=lg><i style="background:${D.colors[c]}"></i>${c}</span>`]=groups[c];});
L.control.layers(null,ov,{collapsed:window.innerWidth<600}).addTo(map);
// 스트림릿 탭이 숨겨진 채(크기 0)로 그려지거나 크기가 바뀌면 다시 맞춤 — 사용자가 직접 움직이기 전까지만
const el=document.getElementById('m'),all=D.pins.map(p=>[p.lat,p.lng]).concat(D.campus);let touched=false;
['mousedown','touchstart','wheel'].forEach(ev=>el.addEventListener(ev,()=>touched=true,{passive:true}));
function fit(){if(touched||el.clientWidth<200||el.clientHeight<200)return;map.invalidateSize();
  const f=D.focus&&byName[D.focus];
  if(f){map.setView(f.getLatLng(),17);f.closePopup();f.openPopup();}  // 다시 열어야 팝업이 화면 안으로 자동 이동
  else if(all.length) map.fitBounds(D.campus.length?D.campus:all,{padding:[30,30]});}
fit();new ResizeObserver(fit).observe(el);
</script></body></html>"""


def route_url(r):
    # 카카오맵: 현재 위치 → 식당 길찾기 (도보/대중교통은 지도에서 선택)
    return f"https://map.kakao.com/link/to/{urllib.parse.quote(r['name'])},{r['lat']},{r['lng']}"


KST = datetime.timezone(datetime.timedelta(hours=9))  # 배포 서버는 UTC라서 한국 시간으로 고정 (한국은 서머타임 없음)


def now_kst():
    return datetime.datetime.now(KST).replace(tzinfo=None)  # 기존 기록(시간대 없음)과 비교되게 naive로


def today():
    return now_kst().date().isoformat()


# ---------- 저장 (방문 수, 같이먹기 모집) ----------
def load_state():
    s = store.load(STATE_FILE)  # 배포: Supabase, 로컬: state.json (store.py)
    for i, p in enumerate(s["parties"]):
        p.setdefault("id", f"old{i}")  # 예전 형식(아이디 없음) 모집도 다룰 수 있게
    return s


def update_state(fn):
    return store.update(STATE_FILE, fn)


def add_visit(name, when=None, uid=None):
    """이용객 수 +1, 그리고 혼잡도 추정용으로 '언제 어디로 결정했는지' 기록 (최근 3000건만).
    uid를 주면 같은 사람·같은 식당은 하루 1번만 셈 (버튼 연타로 순위·혼잡도 조작 방지). 셌으면 True."""
    t = (when or now_kst()).isoformat(timespec="minutes")
    day, key, res = t[:10], f"{uid}|{name}", []

    def fn(s):
        seen = s.get("counted", {}).get(day, [])
        if uid and key in seen:
            return res.append(False)
        if uid:
            s["counted"] = {day: seen + [key]}  # 오늘 것만 남김
            meals = s.setdefault("meals", {})
            meals[uid] = (meals.get(uid, []) + [{"d": day, "r": name}])[-60:]  # 내 식사 기록 (최근 60끼)
        s["visits"][name] = s["visits"].get(name, 0) + 1
        s["log"] = (s.get("log", []) + [{"t": t, "r": name}])[-3000:]
        res.append(True)
    update_state(fn)
    return res[0]


# ---------- 혼잡도 추정 (실제 키오스크 데이터가 아니라 시간대 + 이 앱 사용 기록 기준) ----------
# 평일 시간대별 기본 붐빔 정도 (분, 0~1). 수업 끝나는 12시 전후가 가장 붐빔
_CURVE = [(0, .05), (660, .2), (700, .55), (720, .9), (760, .85), (790, .6), (820, .35), (870, .2),
          (1050, .45), (1090, .55), (1140, .25), (1440, .05)]
CROWD_LEVELS = ["여유", "보통", "붐빔"]


def base_crowd(dt):
    if dt.weekday() >= 5:
        return .15
    m = dt.hour * 60 + dt.minute
    for (m0, v0), (m1, v1) in zip(_CURVE, _CURVE[1:]):
        if m0 <= m <= m1:
            return v0 + (v1 - v0) * (m - m0) / (m1 - m0)
    return .05


def is_open(r, dt):
    """운영시간 정보가 있는 곳만 판단 (없으면 연 것으로 봄)."""
    hours = (r.get("info") or {}).get("운영시간", "")
    if not hours:
        return True
    if dt.weekday() >= 5 and "주말" in hours and ("휴무" in hours or "미운영" in hours):
        return False
    spans = re.findall(r"(\d{1,2}):(\d{2})\s*[~-]\s*(\d{1,2}):(\d{2})", hours)
    m = dt.hour * 60 + dt.minute
    return not spans or any(int(a) * 60 + int(b) <= m < int(c) * 60 + int(d) for a, b, c, d in spans)


def crowd(r, dt, log=(), parties=()):
    """{'level': 여유/보통/붐빔/운영 안 함, 'score', 'why': [...]}"""
    if not is_open(r, dt):
        return {"level": "운영 안 함", "score": 0, "why": ["운영시간이 아니에요"]}
    base = base_crowd(dt) * (1 if r["where"] == "교내" else .8)
    tod = dt.hour * 60 + dt.minute
    recent = hist = 0
    for x in log:
        if x["r"] != r["name"]:
            continue
        t = datetime.datetime.fromisoformat(x["t"])
        if dt - datetime.timedelta(hours=1) <= t <= dt:
            recent += 1
        elif t.weekday() == dt.weekday() and (dt - t).days <= 28 and abs(t.hour * 60 + t.minute - tod) <= 30:
            hist += 1
    joined = sum(len(members_of(p)) for p in parties if p.get("place") == r["name"] and p["date"] == dt.date().isoformat()
                 and abs(int(p["time"][:2]) * 60 + int(p["time"][3:]) - tod) <= 30)
    score = min(1.0, base + .12 * min(recent, 5) + .02 * hist + .1 * min(joined, 5))
    why = (["점심 피크 시간대"] if base >= .7 else []) + ([f"최근 1시간 {recent}명이 여기로 결정"] if recent else []) \
        + ([f"이 시간대 같이 먹기 {joined}명"] if joined else [])
    return {"level": CROWD_LEVELS[0 if score < .4 else 1 if score < .7 else 2], "score": round(score, 2), "why": why}


def better_time(r, dt, log=(), parties=()):
    """지금보다 한산해지는 가장 가까운 시각 (90분 안). 없으면 None."""
    cur = crowd(r, dt, log, parties)
    if cur["level"] not in ("보통", "붐빔"):
        return None
    for k in range(1, 10):
        t = dt + datetime.timedelta(minutes=10 * k)
        c = crowd(r, t, log, parties)
        if c["level"] != "운영 안 함" and CROWD_LEVELS.index(c["level"]) < CROWD_LEVELS.index(cur["level"]):
            return t.strftime("%H:%M")
    return None


# ---------- 조건 추천 ----------
NOT_MEAL = {"사이드", "음료·카페"}  # 한 끼 추천에서 기본으로 뺌

# '소고기'를 빼면 우삼겹·차돌·규동처럼 이름에 소고기가 안 들어간 메뉴도 빠지도록 재료별 단어 묶음.
# (단어, 예외들): 예외 단어가 같이 있으면 해당 안 됨. 예) 쌀국수는 소고기지만 '닭반마리쌀국수'는 아님
# shortcut: 메뉴 이름 기준 추정이라 실제 재료와 다를 수 있음 → 정확히 하려면 메뉴마다 재료 태그를 데이터에 넣기
FOOD_GROUPS = {
    "소고기": ["소고기", "쇠고기", "소불고기", "우삼겹", "차돌", "한우", "소머리", "육회", "규동", "갈비탕", "육개장", "스테이크",
            "비프", "양지", "아롱사태", "사골", "대창", "뚝배기불고기", "바싹불고기", "퍼싸오", "미트",
            "냉면", "해장국", "떡만두국", "만둣국", ("타코", ("치킨", "포크")),  # 보통 소고기 육수·고기
            ("쌀국수", ("닭",)), ("버거", ("치킨", "통살", "포크", "싸이"))],
    "돼지고기": ["돼지", "돈육", "돈까스", "돈카츠", "돈코츠", "가츠", ("카츠", ("치킨",)), "제육", ("삼겹", ("우삼겹",)), "항정", "목살",
             "부타", "돼껍", "수육", "차슈", "스팸", "햄에그", "햄부대", "부대찌개", "베이컨", "소세지", "순대", "불백", "포크",
             "고기듬뿍", "고기국밥", "고기국수", "오삼"],
    "닭고기": ["닭", "치킨", "순살", "너겟", "통살", "불닭", "싸이버거"],
    "해산물": ["해물", "새우", "에비", "오징어", "낙지", "연어", "참치", "게살", "크래미", "바지락", "어묵", "명란", "활어",
            "회덮밥", "물회", "서더리", "날치", "짬뽕", "오삼", "짜조"],
    "매운 것": ["매운", "매콤", "맵", "마라", "불닭", "얼큰", "짬뽕", "고추", "육개장", "부대찌개", "짜글이", "쫄면", "비빔냉면",
             "해장", "똠양", ("핫", ("핫도그",)), ("떡볶이", ("로제", "까르보", "짜장")), ("떡뽀끼", ("로제", "까르보", "짜장")),
             ("라볶이", ("로제", "까르보", "짜장"))],
    "면": ["면", "라멘", "우동", "국수", "소바", "모밀", "파스타", "스파게티", "짬뽕", "누들", "까르보나라", "알리오올리오", "분짜",
          "라볶이", "떡뽀끼"],
    "튀김": ["튀김", "까스", "카츠", "가츠", "너겟", "치킨", "고로케", "춘권", "짜조", "어니언링", "탕수육", "강정", "에비덴",
           "팝만두", "치즈스틱"],
    "치즈": ["치즈", "까르보"],
}
FOOD_ALIASES = {
    "소고기": ["소", "소고기", "쇠고기", "소고기류", "우육", "beef"],
    "돼지고기": ["돼지", "돼지고기", "돈육", "돼지고기류", "pork"],
    "닭고기": ["닭", "닭고기", "치킨", "닭고기류", "chicken"],
    "해산물": ["해산물", "해물", "생선", "수산물", "갑각류", "seafood"],
    "매운 것": ["매운", "매운것", "매운거", "매운음식", "맵", "매움", "매콤", "spicy"],
    "면": ["면", "면류", "면요리"],
    "튀김": ["튀김", "튀긴", "튀김류", "기름진"],
    "치즈": ["치즈", "유제품"],
}
_ALIAS_TO_GROUP = {a: g for g, al in FOOD_ALIASES.items() for a in al}


def expand_words(words):
    """사용자가 적은 단어 → (매칭 규칙들, 펼친 재료 묶음 이름들, 그대로 쓰는 단어들)"""
    rules, groups, literal = [], [], []
    for w in (x.strip().lower() for x in words):
        if not w:
            continue
        g = _ALIAS_TO_GROUP.get(w)
        if g:
            if g not in groups:
                groups.append(g)
                rules += [t if isinstance(t, tuple) else (t, ()) for t in FOOD_GROUPS[g]]
        elif len(w) >= 2:  # '매운 것'의 '것' 같은 한 글자는 무시
            literal.append(w)
            rules.append((w, ()))
    return rules, groups, literal


def text_has(text, rules):
    return any(t in text and not any(u in text for u in unless) for t, unless in rules)


def recommend(rs, budget=None, where="전체", no_types=(), no_words=(), with_unknown=False, visits=None,
              dt=None, log=(), parties=(), limit=12, per_rest=2, jitter=None):
    """조건에 맞는 메뉴를 점수순으로 [(식당, 메뉴, 점수, 이유들)]. 한 식당은 최대 per_rest개만 (여러 식당이 보이게)."""
    visits = visits or {}
    dt = dt or now_kst()
    top = max(visits.values(), default=0)
    rules, _, literal = expand_words(no_words)
    out = []
    for r in rs:
        if where != "전체" and (r["where"] != "교내" if where == "교내" else r.get("gate") != where):
            continue
        if any(w in r["name"].lower() or w in r["category"] for w in literal):  # '위델가'처럼 식당 이름을 적은 경우
            continue
        c = crowd(r, dt, log, parties)
        if c["level"] == "운영 안 함":
            continue
        for m in r["menus"]:
            p, t = m["price"], m.get("type") or r["category"]
            # 메뉴를 모르는 주변 식당은 식당 이름으로, 아니면 메뉴 이름(+오늘 반찬 전부)으로 재료 판단
            text = (r["name"] + " " + m["name"] if "카카오맵" in m["name"] else m["name"] + " " + " ".join(m.get("dishes", []))).lower()
            if t in NOT_MEAL or t in no_types or (p is not None and p < 3000) or text_has(text, rules):
                continue
            if p is None and not with_unknown or (budget and p is not None and p > budget):
                continue
            why, score = [], 0.0
            if p is not None and budget:
                fit = max(0.0, 1 - abs(budget * .8 - p) / budget)  # 예산의 80% 근처가 가장 좋음
                score += fit
                if fit > .8:
                    why.append("예산에 딱")
            elif p is not None:
                score += .5
            if r["where"] == "교내":
                score += .4  # 위치는 목록에 따로 보여서 이유에는 안 적음
            if "(오늘:" in m["name"]:
                score += .4
                why.append("오늘의 학식")
            if visits.get(r["name"]):
                score += .5 * visits[r["name"]] / top
                if visits[r["name"]] == top:
                    why.append("인기")
            score += {"여유": .3, "보통": 0, "붐빔": -.3}[c["level"]]
            why.append(f"지금 {c['level']}")
            if jitter:
                score += jitter.random() * .35  # '다시 추천'마다 순서가 조금씩 바뀌게
            out.append((r, m, round(score, 3), why))
    out.sort(key=lambda x: -x[2])
    picked, per = [], {}
    for x in out:
        if per.get(x[0]["name"], 0) < per_rest:
            per[x[0]["name"]] = per.get(x[0]["name"], 0) + 1
            picked.append(x)
        if len(picked) == limit:
            break
    return picked


# ---------- 같이 먹기 모집: state를 바꾸고, 실패하면 이유(문자열)를 돌려줌 ----------
def members_of(p):
    """예전 형식(이름 문자열)도 {name, menu}로 맞춤."""
    return [m if isinstance(m, dict) else {"name": m, "menu": ""} for m in p.get("members", [])]


def find_party(s, pid):
    return next((p for p in s["parties"] if p.get("id") == pid), None)


# 서버 쪽 한도: 화면의 max_chars는 브라우저에서만 막으니 저장 전에 다시 자름
MAX_PER_HOST, MAX_PER_DAY = 5, 300


def add_party(s, pid, host, place, time, spot="", max_n=4, note="", menu="", day=None, nick=""):
    day = day or today()
    if not re.fullmatch(r"\d\d:\d\d", time or ""):
        return "시간을 확인해주세요"
    todays = [p for p in s["parties"] if p["date"] == day]
    if len(todays) >= MAX_PER_DAY:
        return "오늘 모집이 너무 많아요"
    if sum(host_of(p) == host for p in todays) >= MAX_PER_HOST:
        return f"모집은 하루 {MAX_PER_HOST}개까지 올릴 수 있어요"
    s["parties"].append({"id": pid, "date": day, "place": str(place)[:60], "time": time,
                         "spot": spot if spot in SPOTS else "", "max": min(8, max(2, int(max_n))),
                         "note": str(note)[:60], "host": host, "closed": False,
                         "members": [{"name": host, "nick": str(nick)[:20], "menu": str(menu)[:100]}]})
    return None


def join_party(s, pid, name, menu="", nick=""):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    menu, ms = str(menu)[:100], members_of(p)
    for m in ms:
        if m["name"] == name:  # 이미 참여 중이면 메뉴만 바꿈
            m["menu"] = menu or m["menu"]
            p["members"] = ms
            return None
    if p.get("closed"):
        return "마감된 모집이에요"
    if len(ms) >= p.get("max", 99):
        return "자리가 다 찼어요"
    p["members"] = ms + [{"name": name, "nick": str(nick)[:20], "menu": menu}]
    return None


def set_profile(s, uid, nick, toss="", kakao="", split="메뉴별", budget="1만원", where="전체", avoid=""):
    """내 프로필: 표시 이름 · 결제(송금 정보, 기본 정산 방식) · 취향(기본 예산·위치·못 먹는 재료).
    회원번호(uid)별로 state["profiles"]에 보관."""
    nick = str(nick).strip()[:20]
    if not nick:
        return "이름을 적어주세요"
    if toss and not re.fullmatch(r"[\w.-]{1,30}", toss):
        return "토스 아이디는 toss.me/ 뒤에 붙는 영문·숫자만 적어주세요"
    if kakao and not re.fullmatch(r"https://qr\.kakaopay\.com/[A-Za-z0-9]{1,100}", kakao):
        return "카카오페이 링크는 https://qr.kakaopay.com/… 형식이어야 해요"
    if split not in ("메뉴별", "N빵") or budget not in BUDGETS or where not in WHERES:
        return "설정값을 확인해주세요"
    profiles = s.setdefault("profiles", {})
    if uid not in profiles and len(profiles) >= 5000:
        return "지금은 프로필을 더 저장할 수 없어요"
    profiles[uid] = {"nick": nick, "toss": toss, "kakao": kakao, "split": split,
                     "budget": budget, "where": where, "avoid": str(avoid).strip()[:60]}
    for p in s["parties"]:  # 이미 낀 모집에도 바뀐 이름 반영
        for m in p.get("members", []):
            if isinstance(m, dict) and m["name"] == uid:
                m["nick"] = nick
    return None


def recent_places(s, uid, day, days=3):
    """최근 n일(오늘 포함) 안에 내가 결정한 식당 이름들."""
    since = (datetime.date.fromisoformat(day) - datetime.timedelta(days=days - 1)).isoformat()
    return {m["r"] for m in s.get("meals", {}).get(uid, []) if m["d"] >= since}


def meal_summary(s, uid, day):
    """홈 프로필용: (이번 주 결정 횟수, 최근 간 곳 최대 3개 최신순)"""
    d = datetime.date.fromisoformat(day)
    monday = (d - datetime.timedelta(days=d.weekday())).isoformat()
    ms = s.get("meals", {}).get(uid, [])
    recent = list(dict.fromkeys(m["r"] for m in reversed(ms)))[:3]
    return sum(m["d"] >= monday for m in ms), recent


def mark_paid(s, pid, host_uid, member, paid=True):
    """결제 담당자가 '송금 받음' 표시. 담당자만, 모집에 있는 다른 사람만."""
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if payer_of(p) != host_uid:
        return "결제 담당자만 표시할 수 있어요"
    if member == host_uid or not any(m["name"] == member for m in members_of(p)):
        return "모집에 없는 사람이에요"
    done = [x for x in p.get("paid", []) if x != member]
    p["paid"] = done + [member] if paid else done
    return None


def delete_me(s, uid):
    """내 정보 삭제: 프로필·송금 정보, 참여 중인 모집(방장이면 다음 사람에게 넘김), 오늘 결정 기록에서 회원번호 제거."""
    s.get("profiles", {}).pop(uid, None)
    s.get("meals", {}).pop(uid, None)
    for p in [p for p in s["parties"] if any(m["name"] == uid for m in members_of(p))]:
        leave_party(s, p["id"], uid)
    for p in s["parties"]:
        p["pending"] = [x for x in p.get("pending", []) if x["name"] != uid]
    s["counted"] = {d: [k for k in v if not k.startswith(uid + "|")] for d, v in s.get("counted", {}).items()}
    return None


def my_parties(s, uid, day):
    """오늘 내가 방장이거나 참여한 모집 [(모집, 방장 여부)]"""
    return [(p, host_of(p) == uid) for p in s["parties"]
            if p["date"] == day and any(m["name"] == uid for m in members_of(p))]


def stale(p, day, keep_days=7):
    """지울 모집: 카카오 로그인 전 방식(방장이 'k회원번호'가 아님 → 아무도 관리 못 함) 또는 7일 지난 모집."""
    old_day = (datetime.date.fromisoformat(day) - datetime.timedelta(days=keep_days)).isoformat()
    return not re.fullmatch(r"k\d+", str(host_of(p))) or str(p.get("date", "")) < old_day


def drop_legacy(s, day=None):
    """stale한 모집을 지움 (저장 파일이 끝없이 커지지 않게). 지운 개수 반환."""
    day = day or today()
    old = [p for p in s["parties"] if stale(p, day)]
    for p in old:
        s["parties"].remove(p)
    return len(old)


def mask(nick):
    """로그인 안 한 사람에게 보일 이름: '이동근' → '이*근', '민수' → '민*'."""
    nick = str(nick)
    return nick if len(nick) < 2 else nick[0] + "*" * (len(nick) - 2 if len(nick) > 2 else 1) + (nick[-1] if len(nick) > 2 else "")


def set_menu(s, pid, uid, menu):
    """이미 낀 모집에서 내 메뉴 바꾸기 (비우기도 가능). 마감·정원과 상관없이 내 것만."""
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    ms = members_of(p)
    for m in ms:
        if m["name"] == uid:
            m["menu"] = str(menu).strip()[:100]
            p["members"] = ms
            return None
    return "참여 중인 모집이 아니에요"


def nick_of(p, name):
    """화면에 보일 이름: 로그인 계정 id 대신 카카오 닉네임 (예전 데이터는 이름 그대로)."""
    return next((m.get("nick") or m["name"] for m in members_of(p) if m["name"] == name), name)


def leave_party(s, pid, name):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    was_host = host_of(p) == name
    ms = [m for m in members_of(p) if m["name"] != name]
    if not ms:
        s["parties"].remove(p)  # 마지막 사람이 나가면 모집 삭제
        return None
    p["members"] = ms
    if was_host:
        p["host"] = ms[0]["name"]  # 방장이 나가면 다음 사람이 방장
    if p.get("payer") == name:
        p.pop("payer")  # 결제 담당자가 나가면 방장이 다시 맡음
    p["paid"] = [x for x in p.get("paid", []) if x != name]
    return None


def payer_of(p):
    """정산에서 돈을 받는 사람: 방장이 넘겨준 결제 담당자, 없으면 방장."""
    return p.get("payer") if any(m["name"] == p.get("payer") for m in members_of(p)) else host_of(p)


def request_join(s, pid, uid, nick="", menu=""):
    """초대 링크 없이 참여: 방장 수락 대기 목록에 올림."""
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if any(m["name"] == uid for m in members_of(p)):
        return "이미 참여 중이에요"
    if p.get("closed") or len(members_of(p)) >= p.get("max", 99):
        return "마감된 모집이에요"
    pend = [x for x in p.get("pending", []) if x["name"] != uid]
    if len(pend) >= 20:
        return "참여 요청이 너무 많아요"
    p["pending"] = pend + [{"name": uid, "nick": str(nick)[:20], "menu": str(menu)[:100]}]
    return None


def answer_join(s, pid, host_uid, uid, ok):
    """방장이 참여 요청 수락(ok=True)/거절."""
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if host_of(p) != host_uid:
        return "방장만 수락할 수 있어요"
    req = next((x for x in p.get("pending", []) if x["name"] == uid), None)
    if not req:
        return "요청이 없어요"
    p["pending"] = [x for x in p["pending"] if x["name"] != uid]
    if not ok:
        return None
    if len(members_of(p)) >= p.get("max", 99):
        return "자리가 다 찼어요"
    p["members"] = members_of(p) + [req]
    return None


def set_payer(s, pid, host_uid, uid):
    """방장이 결제 담당자를 다른 참여자에게 넘김 (돈 받는 사람·송금 정보·송금 확인이 그 사람 기준으로)."""
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if host_of(p) != host_uid:
        return "방장만 바꿀 수 있어요"
    if not any(m["name"] == uid for m in members_of(p)):
        return "모집에 없는 사람이에요"
    if uid != payer_of(p):
        p["payer"], p["paid"] = uid, []
        p.pop("pay", None)  # 받는 사람이 바뀌면 송금 정보도 새로 (예전 사람 계좌로 보내지 않게)
    return None


def close_party(s, pid, name, closed=True):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if host_of(p) != name:
        return "방장만 마감할 수 있어요"
    p["closed"] = closed
    return None


def edit_party(s, pid, name, time, spot, max_n, note, place=None):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if host_of(p) != name:
        return "방장만 고칠 수 있어요"
    if not re.fullmatch(r"\d\d:\d\d", time or ""):
        return "시간을 확인해주세요"
    max_n = min(8, max(2, int(max_n)))
    if max_n < len(members_of(p)):
        return f"이미 {len(members_of(p))}명이 모였어요"
    p.update(time=time, spot=spot if spot in SPOTS else "", max=max_n, note=str(note)[:60])
    if place and place != p["place"]:  # 식당이 바뀌면 고른 메뉴·정산 금액이 안 맞으니 비우고 다시 고르게
        p["place"] = str(place)[:60]
        for m in p["members"]:
            if isinstance(m, dict):
                m["menu"] = ""
        p["paid"] = []
    return None


# ---------- 정산: 메뉴별(각자 고른 메뉴값) 또는 N빵(총액 ÷ 인원) ----------
def menu_total(r, menu):
    """'치킨버거, 타코' → 메뉴판 가격 합. 메뉴가 없거나 가격 모르는 게 끼면 None."""
    names = [x.strip() for x in (menu or "").split(",") if x.strip()]
    prices = {m["name"]: m["price"] for m in (r or {}).get("menus", [])}
    vals = [prices.get(n) for n in names]
    return sum(vals) if names and None not in vals else None


def settle(p, r):
    """[(이름, 낼 돈 또는 None)]. N빵은 10원 단위 올림."""
    ms, pay = members_of(p), p.get("pay") or {}
    own = [(m["name"], menu_total(r, m["menu"])) for m in ms]
    if pay.get("mode") != "N빵":
        return own
    total = pay.get("total") or (sum(v for _, v in own) if all(v is not None for _, v in own) else None)
    each = -(-total // (len(ms) * 10)) * 10 if total else None
    return [(n, each) for n, _ in own]


def pay_links(pay, amount=None):
    """{'토스': url, '카카오페이': url}. 토스는 toss.me/아이디/금액 이라 금액이 미리 채워짐."""
    out = {}
    if pay.get("toss"):
        out["토스"] = f"https://toss.me/{urllib.parse.quote(pay['toss'])}" + (f"/{amount}" if amount else "")
    if pay.get("kakao"):
        out["카카오페이"] = pay["kakao"]
    return out


def set_pay(s, pid, name, toss, kakao, mode, total=None):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if payer_of(p) != name:
        return "결제 담당자만 정산을 설정할 수 있어요"
    if toss and not re.fullmatch(r"[\w.-]{1,30}", toss):
        return "토스 아이디는 toss.me/ 뒤에 붙는 영문·숫자만 적어주세요"
    if kakao and not re.fullmatch(r"https://qr\.kakaopay\.com/[A-Za-z0-9]{1,100}", kakao):
        return "카카오페이 링크는 https://qr.kakaopay.com/… 형식이어야 해요"
    if total is not None and not 0 < total <= 1_000_000:
        return "총 금액을 확인해주세요"
    p["pay"] = {"toss": toss, "kakao": kakao, "mode": mode, "total": total}
    return None


def is_admin(uid):
    """운영자: Streamlit Secrets의 ADMIN_IDS (카카오 회원번호, 쉼표로 여러 명. 예: "k123,k456")"""
    return bool(uid) and uid in {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}


def delete_party(s, pid, name):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if host_of(p) != name and not is_admin(name):
        return "방장만 삭제할 수 있어요"
    s["parties"].remove(p)
    return None


def host_of(p):
    return p.get("host") or (members_of(p) or [{"name": ""}])[0]["name"]  # 예전 형식은 첫 사람이 방장


def default_meet_time(now):
    """모집 기본 시간: 오전이면 12:00, 아니면 지금 이후 가장 가까운 10분 단위 (23:50까지)."""
    if now.hour < 12:
        return datetime.time(12, 0)
    m = min(now.hour * 60 + now.minute // 10 * 10 + 10, 23 * 60 + 50)
    return datetime.time(m // 60, m % 60)


def count_text(p):
    """'2/4명' (정원 없는 예전 모집은 '2명')"""
    n = len(members_of(p))
    return f"{n}/{p['max']}명" if p.get("max") else f"{n}명"


def party_status(p, now_hm):
    ms = members_of(p)
    if p["time"] < now_hm:
        return "지난 모집"
    if p.get("closed") or len(ms) >= p.get("max", 99):
        return "마감"
    return "모집중"


# ---------- 키오스크 장바구니 ----------
def tray_add(tray, r, m, n=1):
    k = f"{r['name']}|{m['name']}"
    it = tray.setdefault(k, {"rest": r["name"], "label": label(r), "menu": m["name"], "price": m["price"], "qty": 0})
    it["qty"] += n
    if it["qty"] <= 0:
        del tray[k]


def tray_total(tray):
    """(아는 가격 합계, 가격 미확인 메뉴가 섞였는지)"""
    return (sum(i["price"] * i["qty"] for i in tray.values() if i["price"] is not None),
            any(i["price"] is None for i in tray.values()))


def tray_text(tray):
    return ", ".join(f"{i['menu'].split(' (')[0]}{'×' + str(i['qty']) if i['qty'] > 1 else ''}" for i in tray.values())


def share_base(host=None):
    """초대 링크 앞부분: APP_URL(배포 주소, Streamlit secrets) → 접속한 공개 주소 → 같은 와이파이 주소 순."""
    if os.environ.get("APP_URL"):
        return os.environ["APP_URL"].rstrip("/")
    h = (host or "").split(":")[0]
    if h and h != "localhost" and not re.fullmatch(r"[\d.]+", h):
        return f"https://{host}"
    return lan_url()


# ---------- 카카오 로그인 (OAuth): 비밀번호·토큰은 저장 안 하고 카카오 회원번호·닉네임만 씀 ----------
def _local_secret(env, file):  # 배포 서버는 Streamlit Secrets(환경변수), 내 컴퓨터는 업로드 안 하는 파일에서
    return os.environ.get(env) or ((DIR / file).read_text("utf-8").strip() if (DIR / file).exists() else "")


KAKAO_KEY = _local_secret("KAKAO_REST_KEY", ".kakao_key")
KAKAO_SECRET = _local_secret("KAKAO_CLIENT_SECRET", ".kakao_secret")
def _app_secret():
    """로그인 서명 키 (카카오 로그인 state, 로그인 유지 쿠키). 매번 새로 만들면 서명이 안 맞으니 한 번 만들어 보관:
    APP_SECRET 환경변수 → 없으면 저장소(배포: Supabase, 로컬: state.json)에 처음 한 번 만들어 둠.
    DB에 두면 서버가 재시작돼도 같은 키라 로그인 유지 쿠키가 계속 유효함."""
    if os.environ.get("APP_SECRET"):
        return os.environ["APP_SECRET"].encode()
    s = store.load(STATE_FILE)
    if not s.get("_secret"):
        s = store.update(STATE_FILE, lambda s: s.setdefault("_secret", secrets.token_hex(32)))
    return s["_secret"].encode()


SECRET = _app_secret()


def login_redirect(host=None):
    """카카오 개발자 콘솔에 등록한 Redirect URI와 글자 하나까지 같아야 함."""
    base = share_base(host)
    return base if base != lan_url() else f"http://{host or 'localhost:8501'}"


def sign_state(party="", now=None):
    """로그인 후 돌아올 초대 모집 id + 시각에 서명 → 남이 만든 로그인 요청(CSRF)·변조를 막음."""
    body = f"{party}.{int(now or epoch())}"
    return body + "." + hmac.new(SECRET, body.encode(), "sha256").hexdigest()[:32]


def check_state(state, now=None, max_age=1800):
    """서명이 맞고 30분 안이면 초대 모집 id(없으면 ''), 아니면 None."""
    try:
        party, ts, sig = str(state).rsplit(".", 2)
    except ValueError:
        return None
    good = hmac.new(SECRET, f"{party}.{ts}".encode(), "sha256").hexdigest()[:32]
    if not hmac.compare_digest(sig, good) or not ts.isdigit() or (now or epoch()) - int(ts) > max_age:
        return None
    return party if re.fullmatch(r"[0-9a-z]{0,12}", party) else None


LOGIN_COOKIE, LOGIN_DAYS = "kgu_login", 7


def make_login_token(uid, nick, now=None, days=LOGIN_DAYS):
    """로그인 유지 쿠키 값: base64(회원번호·닉네임·만료시각) + 서명. 비밀번호·카카오 토큰은 넣지 않음."""
    body = base64.urlsafe_b64encode(json.dumps({"id": uid, "nick": nick, "exp": int((now or epoch()) + days * 86400)},
                                               ensure_ascii=False).encode()).decode().rstrip("=")
    return body + "." + hmac.new(SECRET, body.encode(), "sha256").hexdigest()[:40]


def read_login_token(tok, now=None):
    """서명이 맞고 안 지났으면 {'id', 'nick'}, 아니면 None (위조·만료·깨진 값)."""
    try:
        body, sig = str(tok).rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(SECRET, body.encode(), "sha256").hexdigest()[:40]):
            return None
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not re.fullmatch(r"k\d+", str(d.get("id"))) or d.get("exp", 0) < (now or epoch()):
        return None
    return {"id": d["id"], "nick": str(d.get("nick") or "친구")[:20]}


KAKAO_JS_KEY = os.environ.get("KAKAO_JS_KEY", "22f4180c9fb5e0f8412bc7e80bb4556e")  # 공개용 키(브라우저에 노출됨, 등록 도메인만 동작)
KAKAO_SDK = ('<script src="https://t1.kakaocdn.net/kakao_js_sdk/2.8.5/kakao.min.js" crossorigin="anonymous" '
             'integrity="sha384-v6JKY3z4FUkWq21oZDHZe74YvuPk/WUrVLrDHTBojHXClOSa3iXy7sCXPY1jkFbj"></script>')


def kakao_share_html(text, url):
    """카카오톡 공유 버튼 (카카오 JS SDK). 글·링크는 JSON으로 넣고 '</'는 막아 스크립트가 끊기지 않게."""
    def js(v):
        return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")
    return (KAKAO_SDK + '<button id="k" style="width:100%;border:none;border-radius:12px;padding:12px;background:#FEE500;'
            "color:#191919;font:600 15px/1.2 'Pretendard Variable',-apple-system,'Malgun Gothic',sans-serif;cursor:pointer\">"
            "카카오톡으로 초대 보내기</button><script>"
            f"if(!Kakao.isInitialized())Kakao.init({js(KAKAO_JS_KEY)});"
            'document.getElementById("k").onclick=()=>Kakao.Share.sendDefault({objectType:"text",'
            f'text:{js(text)},link:{{mobileWebUrl:{js(url)},webUrl:{js(url)}}},buttonTitle:"모집 보기"}});</script>')


def kakao_auth_url(key, redirect, state):
    return "https://kauth.kakao.com/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": key, "redirect_uri": redirect, "response_type": "code", "state": state})


def kakao_user(key, code, redirect, client_secret=""):
    """인가 코드 → {'id': 'k회원번호', 'nick': 닉네임}. 액세스 토큰은 여기서만 쓰고 버림."""
    import urllib.request
    form = {"grant_type": "authorization_code", "client_id": key, "redirect_uri": redirect, "code": code}
    if client_secret:
        form["client_secret"] = client_secret
    req = urllib.request.Request("https://kauth.kakao.com/oauth/token", urllib.parse.urlencode(form).encode(),
                                 {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        token = json.load(r)["access_token"]
    req = urllib.request.Request("https://kapi.kakao.com/v2/user/me", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=10) as r:
        me = json.load(r)
    profile = (me.get("kakao_account") or {}).get("profile") or {}
    nick = (me.get("properties") or {}).get("nickname") or profile.get("nickname") or "친구"
    return {"id": f"k{int(me['id'])}", "nick": str(nick)[:20]}


def lan_url():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # 패킷은 안 나감, 내 IP만 알아냄
            return f"http://{s.getsockname()[0]}:8501"
    except OSError:
        return "http://localhost:8501"


# ---------- 화면 ----------
PAGES = ["홈", "지도", "고르기", "같이 먹기"]
WHERES = ["전체", "교내", "정문", "후문"]
BUDGETS = {"6천원": 6000, "8천원": 8000, "1만원": 10000, "1만5천원": 15000, "상관없음": None}
REC_KEYS = ["rec_budget", "rec_where", "rec_types", "rec_words", "rec_unknown"]
CROWD_CLS = {"여유": "ok", "보통": "mid", "붐빔": "busy", "운영 안 함": "no"}
SPOTS = ["식당 앞에서 바로", "정문", "후문", "중앙도서관 앞", "학생회관 앞", "경기드림타워 앞"]
WEEKDAY = "월화수목금토일"

# 앱처럼 보이게: 밝은 테마 + 카드 + 하단 탭바. 특정 영역만 꾸밀 땐 .xxx-marker를 넣고 :has()로 그 블록만 잡음
APP_CSS = """<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');
:root{--navgap:48px;--brand:#FF5A1F;--soft:#FFF2EC;--ink:#191F28;--sub:#4E5968;--muted:#8B95A1;--line:#EEF0F3;--bg:#F4F5F7}
html,body,.stApp,.stApp *:not(.material-icons):not([class*="icon"]):not([data-testid*="Icon"]):not([class*="material"]){font-family:'Pretendard Variable',Pretendard,-apple-system,'Malgun Gothic',sans-serif}
.stApp{background:var(--bg);color:var(--ink)}
header[data-testid="stHeader"]{background:transparent;height:0}
[data-testid="stToolbar"],[data-testid="stDecoration"],#MainMenu,footer,[data-testid="stStatusWidget"]{display:none!important}
.block-container{max-width:720px;padding:.6rem 1rem calc(7rem + var(--navgap))}
h1,h2,h3{letter-spacing:-.03em;color:var(--ink)}
p,li{color:var(--sub)}
/* 버튼 */
.stButton>button,.stLinkButton>a,[data-testid="stFormSubmitButton"]>button,[data-testid="stPopover"] button{border-radius:12px;border:none;background:#F2F4F6;
  color:#333D4B;font-weight:600;padding:.6rem .9rem;box-shadow:none;transition:transform .08s,background .15s}
.stButton>button:hover,.stLinkButton>a:hover{background:#E5E8EB;color:var(--ink)}
.stButton>button:active{transform:scale(.97)}
.stButton>button[kind="primary"],[data-testid="stFormSubmitButton"]>button{background:var(--brand);color:#fff;border:none}
.stButton>button[kind="primary"]:hover{color:#fff;background:#E84E15}
.stLinkButton>a[kind="primary"],[data-testid="stBaseLinkButton-primary"]{background:var(--brand);color:#fff!important;border:none}
.stLinkButton>a[kind="primary"]:hover,[data-testid="stBaseLinkButton-primary"]:hover{background:#E84E15;color:#fff}
/* 입력창 */
.stTextInput input,.stSelectbox [data-baseweb="select"]>div,.stMultiSelect [data-baseweb="select"]>div,.stTimeInput input,.stNumberInput input{border-radius:12px!important;background:#fff}
[data-testid="stExpander"]{background:#fff;border:none;border-radius:16px}[data-testid="stExpander"] details{border:none}
[data-testid="stForm"]{background:#fff;border:none;border-radius:16px;padding:1rem}
/* 카드 */
.card{background:#fff;border:none;border-radius:16px;padding:16px 18px;margin:6px 0 10px}
.prof{display:flex;align-items:center;gap:14px}.pf-av{flex:0 0 44px;height:44px;border-radius:50%;background:var(--soft);color:var(--brand);font-weight:700;font-size:18px;display:flex;align-items:center;justify-content:center}.pf-av.off{background:#E5E8EB}
.pf-pay{font-size:12px;color:var(--brand);font-weight:600;margin-top:4px}.fh{font-size:13px;font-weight:700;color:var(--sub);margin:6px 0 -6px}
.pf-n{font-size:16px;font-weight:700;color:var(--ink)}.pf-s{font-size:13px;color:var(--muted);margin-top:2px}
.kbtn{display:block;text-align:center;background:#FEE500;color:#191919!important;text-decoration:none!important;font-weight:600;border-radius:12px;padding:.7rem;margin:2px 0 6px}.kbtn:hover{filter:brightness(.97)}.kcap{font-size:12px;color:var(--muted);text-align:center;margin-bottom:6px}
.opt{margin:10px 2px 4px}.opt b{display:block;font-size:15px;color:var(--ink)}.opt span{font-size:12px;color:var(--muted)}
.card.empty{text-align:center;color:var(--muted);font-size:14px;line-height:1.6;padding:28px 18px}
.sec{font-size:18px;font-weight:700;color:var(--ink);margin:26px 2px 8px;letter-spacing:-.02em}
.sec small{font-size:13px;color:var(--muted);font-weight:500;margin-left:6px}
.ph{margin:10px 2px 18px}.ph h2{font-size:24px;font-weight:700;color:var(--ink);letter-spacing:-.03em;margin:0;padding:0;line-height:1.35}
.ph p{font-size:14px;color:var(--muted);margin:4px 0 0}
.chip{display:inline-block;padding:3px 9px;border-radius:999px;background:var(--bg);color:var(--sub);font-size:12px;font-weight:600;margin:2px 4px 2px 0}
.chip.brand{background:var(--soft);color:var(--brand)}.chip.ok{background:#E8F7EE;color:#12B76A}.chip.no{background:#F2F4F6;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.today{background:#fff;border:1px solid var(--line);border-radius:18px;padding:14px 16px}
.today .l{font-size:13px;color:var(--brand);font-weight:700}.today .t{font-size:11px;color:var(--muted);margin-left:4px;font-weight:500}
.today .m{font-size:14px;color:var(--ink);margin-top:6px;line-height:1.5}
.row{display:flex;justify-content:space-between;align-items:center;padding:10px 2px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:none}.row .n{font-weight:600;color:var(--ink);font-size:14px}.row .s{font-size:12px;color:var(--muted)}
.row .p{font-weight:700;color:var(--ink);white-space:nowrap;margin-left:10px}
.rank{display:inline-flex;width:22px;height:22px;border-radius:7px;background:var(--soft);color:var(--brand);font-weight:800;font-size:12px;
  align-items:center;justify-content:center;margin-right:8px}
.place{font-size:22px;font-weight:800;color:var(--ink);letter-spacing:-.03em}
.result{background:#fff;border:2px solid var(--brand);border-radius:20px;padding:16px 18px;margin:8px 0}
.result .k{font-size:12px;color:var(--brand);font-weight:700}.result .n{font-size:20px;font-weight:800;color:var(--ink);margin-top:2px}
.party .h{display:flex;justify-content:space-between;align-items:center}.party .tm{font-size:20px;font-weight:800;color:var(--ink)}
.party .pl{font-size:15px;font-weight:700;color:var(--ink);margin-top:4px}.party .note{font-size:13px;color:var(--sub);margin-top:6px}
.party .mem{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
.party .mem em{font-style:normal;font-size:10px;font-weight:700;color:var(--brand);margin-left:5px}
.party .mem span{background:var(--bg);border-radius:12px;padding:5px 10px;font-size:12px;color:var(--sub)}.party .mem b{color:var(--ink)}
.party.focus{border:2px solid var(--brand);box-shadow:0 8px 24px rgba(255,90,31,.18)}
.bar{height:6px;border-radius:9px;background:var(--line);margin-top:10px;overflow:hidden}.bar i{display:block;height:100%;background:var(--brand)}
/* 하단 탭바 */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"]{position:fixed;left:50%;transform:translateX(-50%);bottom:0;z-index:999;
  width:min(720px,100%);background:rgba(255,255,255,.97);backdrop-filter:blur(8px);border-top:1px solid var(--line);
  display:flex;flex-wrap:nowrap;justify-content:space-around;gap:0;padding:8px 6px calc(8px + var(--navgap) + env(safe-area-inset-bottom));box-shadow:0 -4px 20px rgba(0,0,0,.05)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"]>*{flex:1;display:flex;min-width:0}  /* 새 Streamlit은 칸마다 div로 한 번 더 감쌈 → 감싼 div도 칸 전체로 */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label{flex:1;justify-content:center;margin:0;padding:12px 0;border-radius:12px;position:relative;z-index:1;background:none!important;cursor:pointer}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label>div:first-child,[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label>div>div:first-child:not([data-testid]){display:none}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label>div{flex:1;display:flex;justify-content:center;min-width:0}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label p{font-size:15px;font-weight:600;color:var(--muted);transition:color .25s;text-align:center;margin:0}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label:has(input:checked) p{color:var(--brand);font-weight:800}
/* 선택 표시: 탭바 위 한 장의 하이라이트가 고른 칸으로 미끄러져 감 (탭 4개 기준) */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"]::before{content:"";position:absolute;left:6px;top:8px;bottom:calc(8px + var(--navgap) + env(safe-area-inset-bottom));width:calc((100% - 12px) / 4);
  border-radius:12px;background:var(--soft);box-shadow:inset 0 0 0 1px rgba(255,90,31,.18);transition:transform .38s cubic-bezier(.34,1.3,.5,1)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"]:has(>:nth-child(2) input:checked)::before{transform:translateX(100%)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"]:has(>:nth-child(3) input:checked)::before{transform:translateX(200%)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"]:has(>:nth-child(4) input:checked)::before{transform:translateX(300%)}
/* 칩 모양 라디오 (매장·종류 고르기) */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"]{gap:6px;flex-wrap:wrap}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label{background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 14px;margin:0}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label>div:first-child,[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label>div>div:first-child:not([data-testid]){display:none}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label p{font-size:13px;font-weight:600;color:var(--sub)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label:has(input:checked){background:var(--ink);border-color:var(--ink)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label:has(input:checked) p{color:#fff}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker){gap:.2rem}  /* 칩 줄 사이 간격을 좁게 */
/* 종류 칩 한 줄: 넘치면 옆으로 밀어서 봄 (배달앱 카테고리처럼) */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker.scroll) div[role="radiogroup"]{flex-wrap:nowrap!important;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;padding-bottom:2px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker.scroll) div[role="radiogroup"]::-webkit-scrollbar{display:none}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker.scroll) div[role="radiogroup"]>*{flex:0 0 auto}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker.scroll) div[role="radiogroup"] label p{white-space:nowrap}
/* 키오스크 메뉴 카드 */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) [data-testid="stHorizontalBlock"]{flex-wrap:nowrap;gap:8px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) [data-testid="column"]{min-width:0!important;flex:1 1 0!important}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button{min-height:96px;width:100%;display:flex;flex-direction:column;
  align-items:flex-start;justify-content:space-between;text-align:left;border-radius:18px;padding:12px 13px;background:#fff}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button p{margin:0;font-size:14px;line-height:1.35;color:var(--ink);white-space:normal;word-break:keep-all}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button p strong{font-weight:700}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button p em{font-style:normal;color:var(--brand);font-weight:800;font-size:15px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button p code{background:var(--brand);color:#fff;
  border-radius:8px;padding:1px 7px;font-size:11px;font-weight:700;margin-left:4px}
.traybar{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(72px + var(--navgap) + env(safe-area-inset-bottom));z-index:998;width:min(688px,calc(100% - 24px));
  display:flex;justify-content:space-between;align-items:center;background:var(--ink);color:#fff!important;text-decoration:none!important;
  border-radius:16px;padding:13px 18px;font-weight:700;font-size:15px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
.traybar b{color:#FFB38A;font-size:14px}
/* 고르기 첫 화면: 세로 카드 두 장 */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .mode-marker) .stButton>button{min-height:120px;width:100%;
  display:block;text-align:left;background:#fff;border-radius:20px;padding:22px 20px;margin-bottom:6px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .mode-marker) .stButton>button>div,
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .mode-marker) .stButton>button>div>span{justify-content:flex-start;width:100%}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .mode-marker) .stButton>button p{margin:0;font-size:14px;color:var(--muted);white-space:normal}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .mode-marker) .stButton>button p:first-child{font-size:20px;color:var(--ink);margin-bottom:6px}
.row .p a{color:var(--brand);text-decoration:none;font-weight:700}
/* 추천 목록 줄 */
/* 새 Streamlit은 버튼 글자를 div·span으로 감싸 가운데 정렬함 → 목록·메뉴 카드는 왼쪽 정렬로 */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button>div,[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button>div>span{justify-content:flex-start;width:100%}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button>div,[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button>div>span{justify-content:flex-start;width:100%}

[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button{width:100%;display:block;text-align:left;
  border-radius:16px;padding:14px 16px;margin-bottom:-8px;background:#fff}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button p{margin:0;font-size:12px;color:var(--muted);white-space:normal}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button p:first-child{font-size:15px;color:var(--ink);margin-bottom:3px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button p em{font-style:normal;color:var(--brand);font-weight:800;margin-left:6px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker.on) .stButton>button{box-shadow:inset 0 0 0 2px var(--brand);background:#fff}
.pickcard{border:none;margin-top:4px}.pickcard .k{font-size:12px;color:var(--brand);font-weight:700}
.pickcard .pm{font-size:20px;font-weight:800;color:var(--ink);margin-top:2px}.pickcard .pm span{font-size:16px;color:var(--brand);margin-left:8px}
.pickcard .pr{font-size:15px;font-weight:700;color:var(--ink);margin-top:10px}.pickcard .pr .chip{margin-left:6px}
.pickcard .s{font-size:12px;color:var(--muted);margin-top:3px}
.crowdrow{margin-top:10px;display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.crowd{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:800}
.crowd.ok{background:#E8F7EE;color:#12B76A}.crowd.mid{background:#FFF6E0;color:#D98E04}.crowd.busy{background:#FFECEC;color:#E5484D}
.crowd.no{background:var(--bg);color:var(--muted)}.cw{font-size:12px;color:var(--sub)}.note2{font-size:11px;color:var(--muted);margin-top:4px}
/* 장바구니 줄 */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .tray-marker) [data-testid="stHorizontalBlock"]{flex-wrap:nowrap;align-items:center;gap:6px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .tray-marker) [data-testid="column"]{min-width:0!important}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .tray-marker) .stButton>button{padding:.2rem .6rem;min-height:0;border-radius:10px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .tray-marker){background:#fff;border:1px solid var(--line);border-radius:20px;padding:14px 14px 10px;
  box-shadow:0 8px 24px rgba(16,24,40,.08)}
/* 휴대폰에서도 버튼 줄이 세로로 쌓이지 않고 앱처럼 가로로 */
[data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:8px}
[data-testid="column"]{min-width:0!important}
@media (max-width:640px){.hero h1{font-size:22px}.grid2{grid-template-columns:1fr}
  .stButton>button,.stLinkButton>a{padding:.5rem .4rem;font-size:13px}
  [data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .kiosk-marker) .stButton>button p{font-size:13px}}
.settle{margin:4px 0 10px}.settle div{display:flex;justify-content:space-between;padding:8px 2px;border-bottom:1px solid var(--line);font-size:14px}
.settle div:last-child{border-bottom:none}.settle em.ok{color:#12B76A}.settle em{font-style:normal;font-size:11px;color:var(--brand);font-weight:700;margin-left:6px}
/* 휴대폰: 터치 영역 넉넉히, 입력창 16px(아이폰 자동 확대 방지), 하단 탭바에 가리지 않게 */
@media (max-width:640px){.block-container{padding:.4rem .75rem calc(8rem + var(--navgap))}
  .stButton>button,.stLinkButton>a,[data-testid="stFormSubmitButton"]>button{min-height:44px}
  input,textarea,[data-baseweb="select"] *{font-size:16px!important}
  .card{padding:14px 15px}.party .tm{font-size:18px}.place{font-size:19px}}
</style>""".replace(  # 배포 서버의 최신 Streamlit은 data-testid 이름이 바뀜(column→stColumn 등) → 둘 다 맞춤
    '[data-testid="element-container"]', ':is([data-testid="element-container"],[data-testid="stElementContainer"])').replace(
    '[data-testid="column"]', ':is([data-testid="column"],[data-testid="stColumn"])').replace(
    '.stButton>button[kind="primary"]', ':is(.stButton>button[kind="primary"],[data-testid="stBaseButton-primary"])')


def main():
    import uuid
    from html import escape as e

    import streamlit as st
    import streamlit.components.v1 as components
    cookie_bridge = components.declare_component("cookie_bridge", path=str(DIR / "cookie_bridge"))

    st.set_page_config(page_title="경기대 점심 뭐먹지", page_icon="🍚", layout="centered", initial_sidebar_state="collapsed")
    st.markdown(APP_CSS, unsafe_allow_html=True)
    ss = st.session_state
    ss.setdefault("tray", {})
    if ss.get("nav") not in PAGES:  # 탭 이름이 바뀐 뒤 예전 값이 남아 있으면 초기화
        ss.pop("nav", None)

    ctx = getattr(st, "context", None)  # 최신 Streamlit(배포 서버)에만 있음, 로컬 1.32엔 없음
    host = ctx.headers.get("host") if ctx else None
    public = share_base(host) != lan_url()

    # 카카오 로그인에서 돌아옴 (?code=…&state=…): 서명 확인 → 회원번호·닉네임만 세션에 보관
    qp = st.query_params
    if qp.get("code") and qp.get("state"):
        back = check_state(qp.get("state"))
        if back is None:
            st.error("로그인 요청이 만료됐어요. 다시 로그인해 주세요.")
        else:
            try:
                ss.user = kakao_user(KAKAO_KEY, qp.get("code"), login_redirect(host), KAKAO_SECRET)
                ss.cookie_set, ss._logged_out = make_login_token(ss.user["id"], ss.user["nick"]), False
            except Exception as ex:  # 키·시크릿·Redirect URI 설정 오류, 코드 재사용 등 → 이유는 서버 로그에만
                body = ex.read().decode("utf-8", "replace")[:300] if hasattr(ex, "read") else ""
                print("kakao login failed:", repr(ex), body, flush=True)
                st.error("카카오 로그인에 실패했어요. 잠시 후 다시 해주세요.")
            ss.nav = PAGES[3] if back else PAGES[0]
            if back and back != "g":
                ss._linked = ss.focus_party = back
        qp.clear()  # 주소창에 인가 코드가 남지 않게

    # 로그인 유지: 브라우저 쿠키를 작은 컴포넌트(cookie_bridge/index.html)로 읽고 굽고 지움.
    # 처음 실행엔 None이고, 컴포넌트가 쿠키를 읽어 보내면 한 번 더 실행됨.
    # cookie_set: None이면 그대로 두기, 값이면 굽기, ""이면 지우기
    try:
        # 굽기/지우기는 한 번만 보냄: 세션에 남겨 두면 그 탭이 다시 실행될 때마다(배포·재연결)
        # 다른 탭에서 새로 로그인한 쿠키까지 지워 버림
        cookie_val = cookie_bridge(name=LOGIN_COOKIE, set=ss.pop("cookie_set", None), age=LOGIN_DAYS * 86400,
                                   key="cookie_bridge", default=None)
    except Exception:  # shortcut: 로컬 Anaconda엔 컴포넌트에 필요한 pyarrow가 없어 로그인 유지만 건너뜀 (배포 서버엔 있음)
        cookie_val = None
    if not ss.get("user") and not ss.get("_logged_out") and cookie_val:
        u = read_login_token(cookie_val)
        if u:
            ss.user = u

    # 초대 링크(?party=아이디)로 들어오면 그 모집으로 바로 이동
    pid_link = st.query_params.get("party")
    if pid_link and ss.get("_linked") != pid_link:
        ss._linked, ss.focus_party, ss.nav = pid_link, pid_link, PAGES[3]

    with st.sidebar:
        st.markdown("### 데이터")
        # 서버 크롤링은 내 컴퓨터에서만 (배포 서버에서 아무나 반복 실행 못 하게)
        if not public and st.button("학식 정보 새로 가져오기", use_container_width=True):
            import crawl
            with st.spinner("경기대 홈페이지에서 가져오는 중..."):
                crawl.main()
            st.rerun()
        st.caption(DATA.get("_note", ""))

    rs = with_today(DATA["restaurants"], DATA, today())
    by_name = {r["name"]: r for r in rs}
    places = dict(place_groups(sorted(rs, key=lambda r: (r["where"] != "교내", r.get("gate") or "", r.get("court") or "", r["name"]))))
    state = load_state()
    if not ss.get("_unlink_checked"):  # 세션마다 한 번: 카카오에서 연결을 끊은 사람 정보 지우기
        ss._unlink_checked = True
        try:
            for uid in store.pop_unlinked():
                state = update_state(lambda s, uid=uid: delete_me(s, uid))
        except OSError as ex:
            print("unlink check failed:", ex, flush=True)
    if any(stale(p, today()) for p in state["parties"]):
        state = update_state(drop_legacy)
    visits = state["visits"]
    now = now_kst()
    now_hm = now.strftime("%H:%M")

    # ---- 공용 동작 (버튼 on_click 콜백: 다음 화면 그리기 전에 실행됨) ----
    def go(page, **kw):
        ss.nav = page
        for k, v in kw.items():
            ss[k] = v

    def decide(names):
        ss.decided = names[0]
        if not me or guest:
            st.toast(f"{label(by_name[names[0]])}(으)로 결정했어요 · 카카오로 로그인하면 이용객 수에 반영돼요")
            return
        counted = [add_visit(n, uid=me) for n in dict.fromkeys(names)]
        st.toast(f"{label(by_name[names[0]])}(으)로 결정했어요" + ("" if any(counted) else " · 오늘은 이미 반영됐어요"))

    def open_menu(name):  # 그 식당 메뉴판(키오스크)으로
        r = by_name[name]
        go(PAGES[1], _map_sel=r.get("court") or name, map_sel=r.get("court") or name)
        if r.get("court"):
            ss[f"stall_{r['court']}"] = name

    def act(fn, *args):  # 모집 변경: 잠금 안에서 실행하고 실패 이유는 토스트로
        res = []
        update_state(lambda s: res.append(fn(s, *args)))
        if res and res[0]:
            st.toast(res[0])

    def section(title, sub=""):
        st.markdown(f'<div class="sec">{title}{f"<small>{sub}</small>" if sub else ""}</div>', unsafe_allow_html=True)

    def page_head(title, sub=""):  # 실제 앱처럼: 색 박스 대신 굵은 제목 + 회색 설명 한 줄
        st.markdown(f'<div class="ph"><h2>{title}</h2>{f"<p>{sub}</p>" if sub else ""}</div>', unsafe_allow_html=True)

    def chips_radio(label_, options, key, scroll=False):
        with st.container():
            st.markdown(f'<span class="chips-marker{" scroll" if scroll else ""}"></span>', unsafe_allow_html=True)
            return st.radio(label_, options, key=key, horizontal=True, label_visibility="collapsed")

    def result_card(r, m, key):
        st.markdown(f'<div class="result"><div class="k">오늘의 추천</div><div class="n">{e(m["name"].split(" (")[0])}</div>'
                    f'<div style="margin-top:6px"><span class="chip brand">{e(label(r))}</span>'
                    f'<span class="chip">{won(m["price"])}</span><span class="chip">{e(r.get("gate") or r["where"])}</span></div></div>',
                    unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        c1.button("메뉴판", key=f"{key}_menu", on_click=open_menu, args=(r["name"],), use_container_width=True)
        c2.button("결정", key=f"{key}_ok", on_click=decide, args=([r["name"]],), use_container_width=True, type="primary")
        c3.link_button("길찾기", route_url(r), use_container_width=True)

    def crowd_html(r):
        c = crowd(r, now, state.get("log", []), state["parties"])
        bt = better_time(r, now, state.get("log", []), state["parties"])
        tip = f" · {bt} 이후 한산해질 것 같아요" if bt else ""
        return (f'<span class="crowd {CROWD_CLS[c["level"]]}">지금 {c["level"]}</span>'
                f'<span class="cw">{e(" · ".join(c["why"]))}{tip}</span>')

    def rec_detail(r, m, budget):
        """추천 목록에서 메뉴를 누르면: 식당 정보 + 혼잡도 + 같은 식당 다른 메뉴 + 다음 행동"""
        info = (r.get("info") or {}).get("운영시간", "")
        others = [x for x in r["menus"] if x is not m and (x.get("type") or "") not in NOT_MEAL
                  and x["price"] and (not budget or x["price"] <= budget)][:6]
        st.markdown(
            f'<div class="card pickcard"><div class="k">고른 메뉴</div>'
            f'<div class="pm">{e(m["name"].split(" (")[0])}<span>{won(m["price"])}</span></div>'
            + (f'<div class="s">{e(m["name"].split(" (", 1)[1].rstrip(")"))}</div>' if " (오늘:" in m["name"] else "")
            + f'<div class="pr">{e(label(r))}<span class="chip">{e(r.get("gate") or r["where"])}</span></div>'
            + (f'<div class="s">{e(info)}</div>' if info else "")
            + f'<div class="crowdrow">{crowd_html(r)}</div><div class="note2">혼잡도는 시간대와 이 앱의 결정·모집 기록으로 추정한 값이에요</div>'
            + (f'<div class="s" style="margin-top:10px">같은 식당 다른 메뉴</div><div>'
               + "".join(f'<span class="chip">{e(x["name"])} {won(x["price"])}</span>' for x in others) + "</div>" if others else "")
            + "</div>", unsafe_allow_html=True)
        k = f"{r['name']}|{m['name']}"
        c1, c2 = st.columns(2)
        c1.button("이걸로 결정", key=f"rd_ok|{k}", type="primary", use_container_width=True, on_click=decide, args=([r["name"]],))
        c2.button("같이 먹을 사람 찾기", key=f"rd_party|{k}", use_container_width=True, on_click=go, args=(PAGES[3],),
                  kwargs={"party_place": r["name"], "my_menu": m["name"].split(" (")[0]})
        c1, c2 = st.columns(2)
        c1.button("담고 메뉴판 보기", key=f"rd_menu|{k}", use_container_width=True,
                  on_click=lambda: (tray_add(ss.tray, r, m), open_menu(r["name"])))
        c2.link_button("길찾기", route_url(r), use_container_width=True)

    # ---- 로그인 상태 (모든 화면 공용) ----
    user = ss.get("user")  # 카카오 로그인 → {"id": "k회원번호", "nick": 닉네임}
    me = user["id"] if user else ""
    guest = bool(user and user.get("guest"))  # 비회원: 초대받은 모집 참여만 (세션 동안만 유지)
    profile = state.get("profiles", {}).get(me, {}) if me else {}
    my_nick = profile.get("nick") or (user["nick"] if user else "")

    def shown(nick):  # 다른 사람 이름은 로그인한 사람에게만 그대로 보임
        return nick if me else mask(nick)

    def apply_prefs(force=False):  # 로그인했거나 프로필을 저장하면 추천·고르기 필터를 내 취향으로
        if profile and (force or ss.get("_pref_for") != me):
            for keys, v in ((("rec_budget", "pk_budget"), profile.get("budget")), (("rec_where", "pk_where"), profile.get("where")),
                            (("rec_words",), profile.get("avoid"))):
                for k in keys:
                    if v is not None:
                        ss[k] = ss["_" + k] = v
            ss._pref_for = me

    def save_profile():
        nonlocal profile
        act(set_profile, me, ss.pf_nick, ss.pf_toss.strip().removeprefix("toss.me/"), ss.pf_kakao.strip(),
            ss.pf_split, ss.pf_budget, ss.pf_where, ss.pf_avoid)
        profile = load_state().get("profiles", {}).get(me, {})
        apply_prefs(force=True)

    apply_prefs()

    def logout():  # 세션 로그아웃 + 쿠키 삭제. 이번 접속의 쿠키 값은 그대로라 자동 로그인도 막아 둠
        ss.pop("user", None)
        ss.pop("_pref_for", None)
        ss._logged_out, ss.cookie_set = True, ""

    def login_button(party=""):  # party: 돌아올 초대 모집 id, "g"면 같이 먹기 화면, 비우면 홈
        if not KAKAO_KEY:
            st.warning("카카오 로그인 설정(KAKAO_REST_KEY)이 아직 없어요.")
        else:  # 배포 서버의 앱 화면(iframe)은 페이지 이동(top)이 막혀 있고 새 탭만 허용 → 새 탭에서 로그인
            url = kakao_auth_url(KAKAO_KEY, login_redirect(host), sign_state(party))
            st.markdown(f'<a class="kbtn" href="{e(url)}" target="_blank" rel="noopener">카카오로 로그인</a>'
                        '<div class="kcap">새 탭에서 로그인돼요. 원래 탭은 새로고침하면 로그인 상태가 돼요</div>', unsafe_allow_html=True)

    # ---- 하단 탭바 ----
    with st.container():
        st.markdown('<span class="nav-marker"></span>', unsafe_allow_html=True)
        page = st.radio("화면", PAGES, key="nav", horizontal=True, label_visibility="collapsed")

    # =============== 🏠 홈 ===============
    if page == PAGES[0]:
        n_in = sum(1 for p in places.values() if p[0]["where"] == "교내")
        if "roll_words" not in ss:  # 세션마다 한 번 뽑음: 크롤링한 메뉴 중 짧은 이름 6개 (가격 아는 식사 메뉴)
            pool = sorted({m["name"].split(" (")[0] for r in rs for m in r["menus"]
                           if m["price"] and (m.get("type") or "") not in NOT_MEAL and len(m["name"].split(" (")[0]) <= 9})
            ss.roll_words = ["점심 뭐 먹지"] + [w + "?" for w in random.sample(pool, min(6, len(pool)))]
        page_head("오늘 " + word_roller(ss.roll_words), f"{now.month}월 {now.day}일 {WEEKDAY[now.weekday()]}요일 · 학식 {n_in}곳 · 주변 식당 {len(places) - n_in}곳")
        # ---- 내 프로필 ----
        if guest:
            st.markdown(f'<div class="card prof"><div class="pf-av">{e(my_nick[:1])}</div><div>'
                        f'<div class="pf-n">{e(my_nick)} <span style="font-size:12px;color:#8B95A1">비회원</span></div>'
                        f'<div class="pf-s">카카오로 로그인하면 프로필·식사 기록을 저장할 수 있어요</div></div></div>',
                        unsafe_allow_html=True)
            st.button("나가기", key="guest_out", use_container_width=True, on_click=logout)
            login_button()
        elif user:
            mine_today = my_parties(state, me, today())
            nxt = min((p for p, _ in mine_today if p["time"] >= now_hm), key=lambda p: p["time"], default=None)
            n_week, recent = meal_summary(state, me, today())
            meal_txt = (f'이번 주 {n_week}번 결정 · 최근 {e(", ".join(label(by_name[r]) if r in by_name else r for r in recent))}'
                        if recent else "아직 결정한 식당이 없어요")
            pay_txt = " · ".join(n + " 연결됨" for n, k in (("토스", "toss"), ("카카오페이", "kakao")) if profile.get(k)) or "송금 정보 없음"
            nxt_txt = (f'{e(nxt["time"])} {e(label(by_name[nxt["place"]]) if nxt["place"] in by_name else nxt["place"])}'
                       if nxt else "예정된 모집 없음")
            st.markdown(f'<div class="card prof"><div class="pf-av">{e(my_nick[:1])}</div><div>'
                        f'<div class="pf-n">{e(my_nick)}</div>'
                        f'<div class="pf-s">오늘 내 모집 {len(mine_today)}건 · {nxt_txt}</div>'
                        f'<div class="pf-s">{meal_txt}</div>'
                        f'<div class="pf-pay">{pay_txt}</div></div></div>', unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            c1.button("내 모집 보기", use_container_width=True, on_click=go, args=(PAGES[3],), kwargs={"only_mine": True})
            c2.button("로그아웃", key="home_logout", use_container_width=True, on_click=logout)
            with st.expander("프로필 · 결제 설정"):
                with st.form("profile", border=False):
                    st.markdown('<div class="fh">기본 정보</div>', unsafe_allow_html=True)
                    st.text_input("표시 이름", my_nick, key="pf_nick", max_chars=20, help="모집과 정산에 보이는 이름이에요")
                    st.markdown('<div class="fh">결제 · 정산</div>', unsafe_allow_html=True)
                    st.text_input("토스 아이디", profile.get("toss", ""), key="pf_toss", placeholder="toss.me/ 뒤에 오는 아이디")
                    st.text_input("카카오페이 송금 링크", profile.get("kakao", ""), key="pf_kakao", placeholder="https://qr.kakaopay.com/…",
                                  help="카카오페이 → 송금 → 송금코드 → 링크 복사")
                    st.radio("기본 정산 방식", ["메뉴별", "N빵"], key="pf_split", horizontal=True,
                             index=1 if profile.get("split") == "N빵" else 0, help="내가 방장인 모집의 정산 화면에 먼저 선택돼요")
                    st.markdown('<div class="fh">내 취향</div>', unsafe_allow_html=True)
                    a, b = st.columns(2)
                    a.selectbox("기본 예산", list(BUDGETS), key="pf_budget", index=list(BUDGETS).index(profile.get("budget", "1만원")))
                    b.selectbox("자주 가는 곳", WHERES, key="pf_where", index=WHERES.index(profile.get("where", "전체")))
                    st.text_input("못 먹는 재료", profile.get("avoid", ""), key="pf_avoid", max_chars=60, placeholder="예: 해산물, 오이")
                    st.form_submit_button("저장", type="primary", use_container_width=True, on_click=save_profile)
                st.caption("저장한 예산·위치·못 먹는 재료는 추천과 고르기에 미리 채워져요. "
                           "무료 서버라 서버가 재시작되면 지워질 수 있어요.")
                st.markdown('<div class="fh">내 정보 삭제</div>', unsafe_allow_html=True)
                sure = st.checkbox("프로필·송금 정보와 참여 중인 모집에서 나를 지울게요 (되돌릴 수 없어요)", key="del_me_sure")
                st.button("내 정보 삭제하고 로그아웃", disabled=not sure, use_container_width=True,
                          on_click=lambda: (act(delete_me, me), logout()))
                st.caption("카카오 연결까지 끊으려면 카카오톡 → 설정 → 카카오계정 → 연결된 서비스 관리에서 이 앱을 해제하세요.")
        else:
            st.markdown('<div class="card prof"><div class="pf-av off"></div><div><div class="pf-n">로그인이 필요해요</div>'
                        '<div class="pf-s">로그인하면 내 모집과 프로필을 볼 수 있어요</div></div></div>', unsafe_allow_html=True)
            login_button()
        # ---- 조건 추천: 조건 → 추천 목록 → 메뉴를 누르면 식당·혼잡도 → 결정 / 메뉴판 / 같이 먹기 ----
        for k in REC_KEYS:  # 다른 탭 다녀와도 조건 유지
            if k not in ss and "_" + k in ss:
                ss[k] = ss["_" + k]
        ss.setdefault("rec_budget", "1만원")
        all_types = sorted({m.get("type") or r["category"] for r in rs for m in r["menus"]} - NOT_MEAL)
        section("조건으로 추천받기", "예산 · 위치 · 싫은 것")
        budget_lbl = chips_radio("예산", list(BUDGETS), key="rec_budget", scroll=True)
        where = chips_radio("위치", WHERES, key="rec_where")
        no_types = st.multiselect("먹기 싫은 종류", all_types, key="rec_types", label_visibility="collapsed",
                                  placeholder="먹기 싫은 종류 (예: 면류, 돈까스)")
        no_words = st.text_input("빼고 싶은 단어", key="rec_words", label_visibility="collapsed", max_chars=60,
                                 placeholder="빼고 싶은 재료 (예: 해산물, 매운)")
        _, used, _ = expand_words(re.split(r"[,\s]+", no_words))
        if used:
            st.caption("함께 빼는 메뉴 → " + " / ".join(
                f"{g}: {', '.join(t if isinstance(t, str) else t[0] for t in FOOD_GROUPS[g][:8])} 등" for g in used))
        with_unknown = st.checkbox("가격 모르는 메뉴도 포함", key="rec_unknown")
        for k in REC_KEYS:
            ss["_" + k] = ss[k]
        c1, c2 = st.columns([2, 1])
        lucky = False
        if c1.button("추천 받기", type="primary", use_container_width=True):
            ss.rec_on, ss.rec_seed, ss.rec_sel = True, random.random(), None
        if c2.button("아무거나 하나", use_container_width=True):
            ss.rec_on, ss.rec_seed, lucky = True, random.random(), True

        if ss.get("rec_on"):
            words = [w for w in re.split(r"[,\s]+", no_words) if w]
            recs = recommend(rs, BUDGETS[budget_lbl], where, no_types, words, with_unknown, visits, now,
                             state.get("log", []), state["parties"], jitter=random.Random(ss.get("rec_seed", 0)))
            if lucky and recs:
                r_, m_ = random.choice(recs)[:2]
                ss.rec_sel = f"{r_['name']}|{m_['name']}"
            section(f"추천 메뉴 {len(recs)}개", "누르면 식당 정보와 혼잡도")
            if not recs:
                st.markdown('<div class="card" style="text-align:center;color:#8B95A1">조건에 맞는 메뉴가 없어요. '
                            '예산을 올리거나 빼는 조건을 줄여보세요.</div>', unsafe_allow_html=True)
            for i, (r, m, _, why) in enumerate(recs, 1):
                k = f"{r['name']}|{m['name']}"
                sel_ = ss.get("rec_sel") == k
                with st.container():
                    st.markdown(f'<span class="rec-marker{" on" if sel_ else ""}"></span>', unsafe_allow_html=True)
                    st.button(f"**{i}. {m['name'].split(' (')[0]}** *{won(m['price'])}*\n\n"
                              f"{label(r)} · {r.get('gate') or r['where']} · {' · '.join(why)}",
                              key=f"rec|{k}", use_container_width=True,
                              on_click=lambda k=k: ss.__setitem__("rec_sel", None if ss.get("rec_sel") == k else k))
                if sel_:
                    rec_detail(r, m, BUDGETS[budget_lbl])

        weekly = DATA.get("weekly", {}).get("days", {}).get(today())
        dorm = DATA.get("dorm_weekly", {}).get("days", {}).get(today(), {})
        cards = []
        if weekly:
            cards.append(("감성코어 한식", "11:00~14:30 · 6,500원", weekly))
        if dorm.get("점심"):
            cards.append(("기숙사 점심", "11:00~13:30", dorm["점심"]))
        if dorm.get("저녁"):
            cards.append(("기숙사 저녁", "17:00~19:00", dorm["저녁"]))
        if cards:
            section("오늘의 학식", f"{now.month}/{now.day} 홈페이지 식단")
            st.markdown('<div class="grid2">' + "".join(
                f'<div class="today"><span class="l">{t}</span><span class="t">{s}</span>'
                f'<div class="m">{" · ".join(e(x) for x in d if x != "쌀밥")}</div></div>' for t, s, d in cards) + "</div>",
                unsafe_allow_html=True)

        open_parties = [p for p in state["parties"] if p["date"] == today() and party_status(p, now_hm) == "모집중"]
        section("같이 먹을 사람", f"모집중 {len(open_parties)}건")
        if open_parties:
            st.markdown('<div class="card">' + "".join(
                f'<div class="row"><div><div class="n">{e(p["time"])} · {e(label(by_name[p["place"]]) if p["place"] in by_name else p["place"])}</div>'
                f'<div class="s">{e(", ".join(shown(m.get("nick") or m["name"]) for m in members_of(p)))}</div></div>'
                f'<div class="p">{count_text(p)}</div></div>' for p in open_parties[:3]) + "</div>",
                unsafe_allow_html=True)
        st.button("같이 먹기 보러가기" if open_parties else "내가 먼저 모집하기", use_container_width=True,
                  on_click=go, args=(PAGES[3],))

        ranking = sorted(((n, v) for n, v in visits.items() if n in by_name), key=lambda kv: -kv[1])[:5]
        if ranking:
            section("인기 식당", "누적 이용객")
            st.markdown('<div class="card">' + "".join(
                f'<div class="row"><div><span class="rank">{i}</span><span class="n">{e(label(by_name[n]))}</span></div>'
                f'<div class="p">{v}명</div></div>' for i, (n, v) in enumerate(ranking, 1)) + "</div>", unsafe_allow_html=True)

        section("메뉴 찾기", "교내·주변 전체")
        q = st.text_input("검색", placeholder="메뉴나 식당 이름 (예: 돈까스, 쌀국수, 위델가)", label_visibility="collapsed")
        where = chips_radio("어디서", WHERES, key="home_where")
        lo, hi = st.slider("가격대(원)", 0, 20000, (0, 20000), step=500, key="price1")
        rows = [(r, m) for r, m in filter_menus(rs, lo=lo, hi=hi)
                if (where == "전체" or (r["where"] == "교내" if where == "교내" else r.get("gate") == where))
                and (not q.strip() or q.strip() in m["name"] or q.strip() in label(r))]
        rows.sort(key=lambda rm: (rm[1]["price"] is None, rm[1]["price"] or 0))
        st.markdown(f'<div class="card">' + ("".join(
            f'<div class="row"><div><div class="n">{e(m["name"])}</div><div class="s">{e(label(r))} · {e(r.get("gate") or r["where"])}</div></div>'
            f'<div class="p">{won(m["price"])}</div></div>' for r, m in rows[:40]) or '<div class="s">찾는 메뉴가 없어요</div>')
            + (f'<div class="s" style="padding-top:8px">외 {len(rows) - 40}개 · 검색어를 더 구체적으로 적어보세요</div>' if len(rows) > 40 else "")
            + "</div>", unsafe_allow_html=True)

    # =============== 🗺️ 지도 + 키오스크 ===============
    elif page == PAGES[1]:
        geo = json.loads(GEO_FILE.read_text("utf-8")) if GEO_FILE.exists() else {}
        pnames = list(places)
        if "map_sel" not in ss and ss.get("_map_sel") in pnames:  # 다른 탭 다녀와도 고른 식당 유지
            ss.map_sel = ss._map_sel

        def place_label(p):
            r = places[p][0]
            kind = f"푸드코트 · {len(places[p])}개 매장" if r.get("court") else r["category"]
            return f"{p} · {kind} · {r.get('gate') or r['where']}"

        sel = st.selectbox("식당", pnames, key="map_sel", index=None, placeholder="식당을 고르거나 지도에서 핀을 눌러보세요",
                           format_func=place_label, label_visibility="collapsed",
                           on_change=lambda: ss.__setitem__("_map_sel", ss.map_sel))
        components.html(map_html(rs, geo, DATA, visits, sel), height=430 if sel else 520)
        st.caption("© OpenStreetMap contributors · 식당 위치: 카카오 로컬 API")

        if sel:
            members = places[sel]
            r0 = members[0]
            info = " · ".join(f"{v}" for k, v in (r0.get("info") or {}).items() if k in ("위치", "운영시간"))
            st.markdown(f'<div class="card"><div class="place">{e(sel)}{" 푸드코트" if r0.get("court") else ""}</div>'
                        f'<div style="margin-top:6px"><span class="chip brand">{e(r0["category"])}</span>'
                        f'<span class="chip">{e(r0.get("gate") or r0["where"])}</span>'
                        + (f'<span class="chip">매장 {len(members)}곳</span>' if r0.get("court") else "")
                        + f'</div><div class="s" style="margin-top:8px;font-size:12px;color:#8B95A1">{e(info)}</div>'
                        f'<div class="crowdrow">{crowd_html(r0)}</div></div>',
                        unsafe_allow_html=True)
            if r0.get("court"):
                stall = chips_radio("매장", [m["name"] for m in members], key=f"stall_{sel}")
                r = by_name[stall]
            else:
                r = r0
            groups = by_type(r["menus"])
            t = chips_radio("종류", ["전체"] + list(groups), key=f"type_{r['name']}") if len(groups) > 1 else "전체"
            ms = r["menus"] if t == "전체" else groups[t]
            section(f"{r['name']} 메뉴", f"{len(ms)}개 · 눌러서 담기 · {r.get('price_source', '')}")
            with st.container():
                st.markdown('<span class="kiosk-marker"></span>', unsafe_allow_html=True)
                for i in range(0, len(ms), 3):
                    for col, m in zip(st.columns(3), ms[i:i + 3]):
                        q_ = ss.tray.get(f"{r['name']}|{m['name']}", {}).get("qty", 0)
                        col.button(f"**{m['name']}**\n\n*{won(m['price'])}*" + (f" `{q_}개`" if q_ else ""),
                                   key=f"k|{r['name']}|{m['name']}", on_click=tray_add, args=(ss.tray, r, m),
                                   use_container_width=True)
            c1, c2 = st.columns(2)
            c1.link_button("길찾기", route_url(r), use_container_width=True)
            c2.button("메뉴 안 고르고 결정", key="decide_plain", on_click=decide, args=([r["name"]],), use_container_width=True)

        if ss.tray:  # 담은 메뉴 (식당 상관없이 유지)
            total, unknown = tray_total(ss.tray)
            n_items = sum(i["qty"] for i in ss.tray.values())
            st.markdown(f'<a class="traybar" href="#tray" target="_self">{n_items}개 · {total:,}원{"+" if unknown else ""}'
                        f'<b>담은 메뉴 보기 ›</b></a>', unsafe_allow_html=True)
            with st.container():
                st.markdown(f'<span class="tray-marker" id="tray"></span><div class="sec" style="margin-top:0">담은 메뉴'
                            f'<small>{sum(i["qty"] for i in ss.tray.values())}개</small></div>', unsafe_allow_html=True)
                for k, it in list(ss.tray.items()):
                    a, b, c, d = st.columns([6, 1, 1, 1])
                    a.markdown(f'<div class="n" style="font-weight:600;color:#191F28">{e(it["menu"].split(" (")[0])}</div>'
                               f'<div style="font-size:12px;color:#8B95A1">{e(it["label"])} · {won(it["price"])}</div>',
                               unsafe_allow_html=True)
                    b.button("−", key=f"t-{k}", on_click=tray_add, args=(ss.tray, by_name[it["rest"]], {"name": it["menu"], "price": it["price"]}, -1))
                    c.markdown(f'<div style="text-align:center;font-weight:800;padding-top:6px">{it["qty"]}</div>', unsafe_allow_html=True)
                    d.button("+", key=f"t+{k}", on_click=tray_add, args=(ss.tray, by_name[it["rest"]], {"name": it["menu"], "price": it["price"]}))
                st.markdown(f'<div class="row" style="border:none"><span class="n">합계</span><span class="p" style="font-size:20px;color:#FF5A1F">'
                            f'{total:,}원{" + 가격 미확인" if unknown else ""}</span></div>', unsafe_allow_html=True)
                c1, c2, c3 = st.columns([1, 2, 2])
                c1.button("비우기", on_click=lambda: ss.tray.clear(), use_container_width=True)
                rest_names = [i["rest"] for i in ss.tray.values()]
                c2.button("이걸로 결정", type="primary", on_click=decide, args=(rest_names,), use_container_width=True)
                c3.button("같이 먹을 사람", use_container_width=True, on_click=go,
                          args=(PAGES[3],), kwargs={"party_place": rest_names[0], "my_menu": tray_text(ss.tray)})

    # =============== 고르기: 배달앱처럼 필터 칩 → 식당 목록 → 누르면 메뉴·결정 ===============
    elif page == PAGES[2]:
        mode = ss.get("pk_mode")
        if not mode:  # 첫 화면: 어떻게 먹을지 먼저
            page_head("고르기", "오늘은 어떻게 드실 건가요?")
            with st.container():
                st.markdown('<span class="mode-marker"></span>', unsafe_allow_html=True)
                st.button(f"**근처에서 먹어요**\n\n교내 학식과 정문·후문 식당 {len(places)}곳에서 메뉴까지 골라요",
                          key="mode_near", use_container_width=True, on_click=lambda: ss.__setitem__("pk_mode", "근처"))
                st.button(f"**오늘은 배달**\n\n경기대에서 3km 안, 배달 가능한 음식점 {len(DELIVERY):,}곳에서 골라요",
                          key="mode_dl", use_container_width=True, on_click=lambda: ss.__setitem__("pk_mode", "배달"))
        elif mode == "배달":
            page_head("오늘은 배달", f"경기대에서 3km 안 음식점 {len(DELIVERY):,}곳 · 카카오맵 기준")
            st.button("← 근처에서 먹기로 바꾸기", key="mode_back_dl", on_click=lambda: ss.pop("pk_mode", None))
            dcat = chips_radio("종류", ["전체"] + [c for c in CATS if c != "학식"], key="dl_cat", scroll=True)
            dist_lbl = chips_radio("거리", ["1km 안", "2km 안", "3km 안"], key="dl_dist")
            dl = delivery_list(DELIVERY, dcat, {"1km 안": 1000, "2km 안": 2000, "3km 안": 3000}[dist_lbl])
            if ss.get("dl_filter") != (dcat, dist_lbl):  # 조건 바뀌면 다시 20곳부터
                ss.dl_filter, ss.dl_n, ss.dl_pick = (dcat, dist_lbl), 20, None
            a_, b_ = st.columns([3, 1])
            a_.markdown(f'<div class="sec" style="margin-top:14px">{len(dl):,}곳<small>가까운 순</small></div>', unsafe_allow_html=True)
            if b_.button("랜덤", key="dl_rand", disabled=not dl, use_container_width=True):
                ss.dl_pick = random.choice(dl)
            pick = ss.get("dl_pick")
            if pick:
                st.markdown(f'<div class="result"><div class="k">오늘은 이거 시켜요</div><div class="n">{e(pick["name"])}</div>'
                            f'<div style="margin-top:6px"><span class="chip brand">{e(pick["sub"] or pick["category"])}</span>'
                            f'<span class="chip">{pick["dist"] / 1000:.1f}km</span></div></div>', unsafe_allow_html=True)
                st.link_button("카카오맵에서 메뉴·전화 보기", pick["url"], type="primary", use_container_width=True)
            if not dl:
                st.markdown('<div class="card empty">조건에 맞는 음식점이 없어요.<br>거리를 넓혀보세요.</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="card">' + "".join(
                    f'<div class="row"><div><div class="n">{e(r["name"])}</div>'
                    f'<div class="s">{e(r["sub"] or r["category"])} · {e(r["address"].split(" ", 2)[-1] if r["address"] else "")}</div></div>'
                    f'<div class="p"><a href="{e(r["url"])}" target="_blank" rel="noopener">{r["dist"] / 1000:.1f}km ›</a></div></div>'
                    for r in dl[:ss.dl_n]) + "</div>", unsafe_allow_html=True)
                if len(dl) > ss.dl_n:
                    st.button(f"더 보기 ({len(dl) - ss.dl_n:,}곳 남음)", key="dl_more", use_container_width=True,
                              on_click=lambda: ss.__setitem__("dl_n", ss.dl_n + 20))
            st.caption("메뉴와 가격은 카카오맵·배달앱에서 확인하세요. 실제 배달 가능 여부는 가게마다 달라요.")
        else:
            st.button("← 배달로 바꾸기", key="mode_back_near", on_click=lambda: ss.pop("pk_mode", None))
            page_head("고르기", "종류와 예산을 정하면 갈 만한 곳만 추려 드려요")
            ss.setdefault("pk_budget", "1만원")
            cat = chips_radio("종류", ["전체"] + CATS, key="pk_cat", scroll=True)
            where = chips_radio("위치", WHERES, key="pk_where")
            budget_lbl = chips_radio("예산", list(BUDGETS), key="pk_budget", scroll=True)
            lst = pick_list(rs, cat, where, BUDGETS[budget_lbl])
            ate = recent_places(state, me, today()) if me else set()
            if ate and st.toggle(f"최근 3일 간 곳 빼기 ({len(ate)}곳)", key="pk_fresh"):
                lst = [(r, ms) for r, ms in lst if r["name"] not in ate]
            if ss.get("pk_filter") != (cat, where, budget_lbl):  # 조건 바뀌면 다시 20곳부터
                ss.pk_filter, ss.pk_n = (cat, where, budget_lbl), 20
            a, b = st.columns([3, 1])
            a.markdown(f'<div class="sec" style="margin-top:14px">{len(lst)}곳<small>싼 메뉴 순</small></div>', unsafe_allow_html=True)
            if b.button("랜덤", disabled=not lst, use_container_width=True):
                i = random.randrange(len(lst))
                ss.pk_sel, ss.pk_n = lst[i][0]["name"], max(ss.pk_n, i + 1)  # 뽑힌 곳이 목록 밖이면 거기까지 펼침
            if not lst:
                st.markdown('<div class="card empty">조건에 맞는 식당이 없어요.<br>예산을 올리거나 위치를 전체로 바꿔보세요.</div>',
                            unsafe_allow_html=True)
            for r, ms in lst[:ss.pk_n]:
                sel = ss.get("pk_sel") == r["name"]
                top = [m["name"].split(" (")[0] for m in sorted(ms, key=lambda m: m["price"] or 10**9)[:2]]
                with st.container():
                    st.markdown(f'<span class="rec-marker{" on" if sel else ""}"></span>', unsafe_allow_html=True)
                    st.button(f"**{label(r)}** *{price_range(ms)}*\n\n{r['category']} · {r.get('gate') or r['where']} · {', '.join(top)}",
                              key=f"pk|{r['name']}", use_container_width=True,
                              on_click=lambda n=r["name"]: ss.__setitem__("pk_sel", None if ss.get("pk_sel") == n else n))
                if sel:
                    rows = sorted(ms, key=lambda m: (m["price"] is None, m["price"] or 0))
                    st.markdown('<div class="card pickcard">' + "".join(
                        f'<div class="row"><div class="n">{e(m["name"].split(" (")[0])}</div><div class="p">{won(m["price"])}</div></div>'
                        for m in rows[:8])
                        + (f'<div class="s" style="padding-top:8px">외 {len(rows) - 8}개는 메뉴판에서</div>' if len(rows) > 8 else "")
                        + f'<div class="crowdrow">{crowd_html(r)}</div></div>', unsafe_allow_html=True)
                    c1, c2 = st.columns(2)
                    c1.button("여기로 결정", key=f"pk_ok|{r['name']}", type="primary", use_container_width=True,
                              on_click=decide, args=([r["name"]],))
                    c2.button("같이 먹을 사람 찾기", key=f"pk_party|{r['name']}", use_container_width=True,
                              on_click=go, args=(PAGES[3],), kwargs={"party_place": r["name"]})
                    c1, c2 = st.columns(2)
                    c1.button("메뉴판", key=f"pk_menu|{r['name']}", use_container_width=True, on_click=open_menu, args=(r["name"],))
                    c2.link_button("길찾기", route_url(r), use_container_width=True)
            if len(lst) > ss.pk_n:
                st.button(f"더 보기 ({len(lst) - ss.pk_n}곳 남음)", use_container_width=True,
                          on_click=lambda: ss.__setitem__("pk_n", ss.pk_n + 20))

    # =============== 👫 같이 먹기 ===============
    else:
        page_head("같이 먹기", f"점심 같이 먹을 사람을 구해요 · {now_hm} 기준")

        # 초대 링크로 들어왔고 아직 안 낀 모집이면: 배민 함께주문처럼 로그인 → 메뉴만 고르면 참여
        inv = find_party(state, ss.get("_linked") or "")
        joinable = bool(inv) and inv["date"] == today() and inv["time"] >= now_hm and party_status(inv, now_hm) == "모집중"
        if not user:  # 입장 화면: 카카오 로그인 또는 (초대받았으면) 비회원으로 이름만 정하고 참여
            with st.container(border=True):
                if inv:
                    ir0 = by_name.get(inv["place"])
                    st.markdown(f'<div class="sec" style="margin-top:4px">{e(mask(nick_of(inv, host_of(inv))))}님이 초대했어요'
                                f'<small>{e(inv["time"])} · {count_text(inv)}</small></div>'
                                f'<div class="pl" style="font-weight:700">{e(label(ir0) if ir0 else inv["place"])}</div>',
                                unsafe_allow_html=True)
                    if not joinable:
                        st.caption("이 모집은 시간이 지났거나 마감됐어요. 로그인하면 다른 모집을 보거나 직접 올릴 수 있어요.")
                elif ss.get("_linked"):
                    st.markdown('<div class="sec" style="margin-top:4px">초대받은 모집을 찾을 수 없어요</div>', unsafe_allow_html=True)
                    st.caption("모집이 삭제됐거나 끝났어요. 로그인하면 다른 모집을 보거나 직접 올릴 수 있어요.")
                else:
                    st.markdown('<div class="sec" style="margin-top:4px">같이 먹으려면 들어오기</div>', unsafe_allow_html=True)
                st.markdown('<div class="opt"><b>카카오로 로그인</b><span>모집 올리기·참여·정산까지 모두 쓸 수 있어요</span></div>',
                            unsafe_allow_html=True)
                login_button(inv["id"] if inv else "g")
                if joinable:
                    st.markdown('<div class="opt"><b>비회원으로 참여</b><span>이름만 정하면 이 모집에 바로 들어가요 (이 창을 닫으면 다시 정해야 해요)</span></div>',
                                unsafe_allow_html=True)
                    with st.form("guest", border=False):
                        gname = st.text_input("이름", max_chars=20, placeholder="친구들이 알아볼 이름", label_visibility="collapsed")
                        if st.form_submit_button("이 이름으로 들어가기", use_container_width=True):
                            if gname.strip():
                                ss.user = {"id": "g" + uuid.uuid4().hex[:10], "nick": gname.strip()[:20], "guest": True}
                                st.rerun()
                            st.error("이름을 적어주세요")
        elif inv and not joinable and not any(m["name"] == me for m in members_of(inv)):
            st.caption("초대받은 모집은 시간이 지났거나 마감됐어요.")
        if user and joinable and not any(m["name"] == me for m in members_of(inv)):
            ir = by_name.get(inv["place"])
            with st.container(border=True):
                st.markdown(f'<div class="sec">{e(shown(nick_of(inv, host_of(inv))))}님이 초대했어요<small>{e(inv["time"])} · {count_text(inv)}</small></div>'
                            f'<div class="pl" style="font-weight:700">{e(label(ir) if ir else inv["place"])}</div>'
                            f'<div style="color:#8B95A1;font-size:14px">{e(inv.get("spot") or "식당 앞")}에서 만나요</div>',
                            unsafe_allow_html=True)
                with st.form("invite", border=False):
                    opts = [f'{m["name"]} · {won(m["price"])}' for m in (ir or {}).get("menus", [])]
                    picks = st.multiselect("먹고 싶은 메뉴", opts, placeholder="메뉴판에서 골라요 (여러 개 가능)")
                    if st.form_submit_button(f"{my_nick}(으)로 참여하기", type="primary", use_container_width=True):
                        menu, res = ", ".join(p.split(" · ")[0] for p in picks), []
                        update_state(lambda s: res.append(join_party(s, inv["id"], me, menu, my_nick)))
                        if res[0]:
                            st.error(res[0])
                        else:
                            ss.my_menu = menu
                            st.rerun()
        if user:
            a, b = st.columns([3, 1])
            a.markdown(f'<div style="padding-top:8px"><b>{e(my_nick)}</b>님{"(비회원)" if guest else "으로 로그인됨"}</div>',
                       unsafe_allow_html=True)
            b.button("나가기" if guest else "로그아웃", key="party_logout", on_click=logout, use_container_width=True)
        my_menu = st.text_input("내가 먹을 메뉴 (선택)", key="my_menu", max_chars=40, placeholder="예: 숑숑카츠").strip()

        today_parties = sorted((p for p in state["parties"] if p["date"] == today()), key=lambda p: p["time"])
        base = share_base(host)
        with st.expander("새 모집 만들기", expanded=bool(ss.get("party_place")) or not today_parties):
            with st.form("new_party", clear_on_submit=True, border=False):
                names = sorted(by_name, key=lambda n: (by_name[n]["where"] != "교내", label(by_name[n])))
                want = ss.get("party_place") or ss.get("decided")
                shown_of = {n: f"{label(by_name[n])} · {by_name[n].get('gate') or by_name[n]['where']}" for n in names}
                name_of = {v: k for k, v in shown_of.items()}
                place = name_of[st.selectbox("식당", list(shown_of.values()),
                                             index=names.index(want) if want in names else 0)]
                a, b = st.columns(2)
                when = a.time_input("몇 시에?", default_meet_time(now), step=600)
                spot = b.selectbox("어디서 만나요?", SPOTS)
                a, b = st.columns(2)
                max_n = a.slider("최대 인원", 2, 8, 4)
                note = b.text_input("한마디 (선택)", max_chars=60, placeholder="예: 3층 강의 끝나고 바로!")
                if st.form_submit_button("모집 올리기", use_container_width=True):
                    if not me or guest:
                        st.toast("모집은 카카오로 로그인해야 올릴 수 있어요")
                    else:
                        pid, res = uuid.uuid4().hex[:6], []
                        update_state(lambda s: res.append(add_party(s, pid, me, place, when.strftime("%H:%M"), spot, max_n,
                                                                    note.strip(), my_menu, nick=my_nick)))
                        if res[0]:
                            st.error(res[0])
                        else:
                            ss.focus_party = pid
                            ss.pop("party_place", None)
                            st.rerun()

        def manage(c, p):  # 방장 전용: 식당·시간·장소·인원·한마디 수정, 결제 담당자, 마감, 삭제
            pid = p["id"]
            with c.popover("관리", use_container_width=True):
                with st.form(f"edit_{pid}", border=False):
                    places_ = {label(by_name[n]) + " · " + (by_name[n].get("gate") or by_name[n]["where"]): n
                               for n in sorted(by_name, key=lambda n: (by_name[n]["where"] != "교내", label(by_name[n])))}
                    keys_ = list(places_)
                    cur_place = next((i for i, k in enumerate(keys_) if places_[k] == p["place"]), 0)
                    pl = st.selectbox("식당", keys_, index=cur_place, help="바꾸면 모두의 메뉴가 비워져서 다시 골라야 해요")
                    t = st.time_input("시간", datetime.time.fromisoformat(p["time"]), step=600)
                    sp = st.selectbox("만나는 곳", SPOTS, index=SPOTS.index(p["spot"]) if p.get("spot") in SPOTS else 0)
                    mx = st.slider("최대 인원", 2, 8, p.get("max") or 4)
                    nt = st.text_input("한마디", p.get("note", ""), max_chars=60)
                    if st.form_submit_button("수정 저장", use_container_width=True):
                        act(edit_party, pid, me, t.strftime("%H:%M"), sp, mx, nt.strip(), places_[pl])
                        st.rerun()
                ms_ = members_of(p)
                if len(ms_) > 1:  # 결제 담당자 넘기기
                    opts = [f"{m.get('nick') or m['name']}{' (나)' if m['name'] == me else ''}" for m in ms_]
                    cur = [m["name"] for m in ms_].index(payer_of(p))
                    st.selectbox("결제 담당자 (돈 받는 사람)", opts, index=cur, key=f"payer_{pid}",
                                 on_change=lambda: act(set_payer, pid, me, ms_[opts.index(ss[f"payer_{pid}"])]["name"]))
                st.button("다시 열기" if p.get("closed") else "모집 마감", key=f"cl_{pid}", use_container_width=True,
                          on_click=act, args=(close_party, pid, me, not p.get("closed")))
                sure = st.checkbox("정말 삭제할게요 (되돌릴 수 없어요)", key=f"sure_{pid}")
                st.button("모집 삭제", key=f"del_{pid}", type="primary", disabled=not sure, use_container_width=True,
                          on_click=act, args=(delete_party, pid, me))

        def menu_editor(c, p, r):  # 내 메뉴 수정: 그 식당 메뉴판에서 고르거나 직접 적기
            pid = p["id"]
            mine_m = next((m["menu"] for m in members_of(p) if m["name"] == me), "")
            names = [m["name"].split(" (")[0] for m in (r or {}).get("menus", [])]
            price = {m["name"].split(" (")[0]: m["price"] for m in (r or {}).get("menus", [])}
            cur = [x.strip() for x in mine_m.split(",") if x.strip()]
            shown_m = {f"{n} · {won(price.get(n))}": n for n in names}
            with c.popover("메뉴 수정", use_container_width=True):
                st.caption(f"{label(r) if r else p['place']} 메뉴에서 내가 먹을 것만 바꿔요")
                st.multiselect("메뉴판에서 고르기", list(shown_m), default=[k for k, v in shown_m.items() if v in cur],
                               key=f"mm_{pid}", placeholder="여러 개 고를 수 있어요")
                st.text_input("메뉴판에 없으면 직접 적기", ", ".join(x for x in cur if x not in names), key=f"mt_{pid}", max_chars=40)
                st.button("저장", key=f"ms_{pid}", type="primary", use_container_width=True,
                          on_click=lambda: act(set_menu, pid, me, ", ".join(
                              [shown_m[k] for k in ss[f"mm_{pid}"]] + [x for x in [ss[f"mt_{pid}"].strip()] if x])))

        def settle_panel(p, r):
            pid, pay, host = p["id"], p.get("pay") or {}, payer_of(p)  # host = 돈 받는 결제 담당자
            rows = settle(p, r)
            if me == host:
                with st.form(f"pay_{pid}", border=False):
                    mode = st.radio("나누는 방법", ["메뉴별", "N빵"], horizontal=True,
                                    index=1 if (pay.get("mode") or profile.get("split")) == "N빵" else 0)
                    total = st.number_input("N빵 총 금액 (0이면 메뉴값 합계)", 0, 1_000_000, pay.get("total") or 0, step=1000)
                    toss = st.text_input("토스 아이디", pay.get("toss") or profile.get("toss", ""), placeholder="toss.me/ 뒤에 오는 아이디")
                    kakao = st.text_input("카카오페이 송금 링크", pay.get("kakao") or profile.get("kakao", ""),
                                          placeholder="카카오페이 → 송금 → 송금코드 → 링크 복사")
                    if st.form_submit_button("정산 정보 저장", use_container_width=True):
                        act(set_pay, pid, me, toss.strip().removeprefix("toss.me/"), kakao.strip(), mode, int(total) or None)
                        st.rerun()
            paid = set(p.get("paid", []))
            payers = [n for n, _ in rows if n != host]
            st.markdown(f'<div class="s">송금 완료 {sum(n in paid for n in payers)}/{len(payers)}명</div><div class="settle">' + "".join(
                f'<div><span>{e(nick_of(p, n))}{" <em>받는 사람</em>" if n == host else ""}'
                f'{" <em class=ok>송금 완료</em>" if n in paid else ""}</span>'
                f'<b>{won(v) if v is not None else "금액 미정"}</b></div>' for n, v in rows) + "</div>", unsafe_allow_html=True)
            if me == host and payers:  # 방장: 받은 사람 체크
                for n in payers:
                    k = f"paid_{pid}_{n}"
                    st.checkbox(f"{nick_of(p, n)} 송금 받음", value=n in paid, key=k,
                                on_change=lambda n=n, k=k: act(mark_paid, pid, me, n, ss[k]))
            elif me in paid:
                st.caption("결제 담당자가 송금 완료로 표시했어요.")
                return
            if not pay_links(pay):
                st.caption("결제 담당자가 토스 아이디나 카카오페이 링크를 저장하면 여기서 바로 송금할 수 있어요.")
                return
            if me == host:  # 만나서 친구가 찍을 수 있게 QR
                url = next(iter(pay_links(pay).values()))
                components.html(
                    '<div id="q" style="display:flex;justify-content:center"></div>'
                    '<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>'
                    f'<script>new QRCode(document.getElementById("q"),{{text:{json.dumps(url)},width:170,height:170}})</script>',
                    height=185)
                st.caption("친구가 휴대폰 카메라로 찍으면 송금 화면이 열려요.")
            else:
                amt = dict(rows).get(me)
                if p["time"] > now_hm:  # 송금 정보는 실제로 만날 시간이 지나야 공개 (아무 모집에 끼어 계좌만 보는 것 방지)
                    st.caption(f"{p['time']} 이후에 송금 버튼이 열려요.")
                    return
                for i, (k, url) in enumerate(pay_links(pay, amt).items()):
                    st.link_button(f"{k}로 {won(amt) if amt and k == '토스' else ''} 보내기".replace("  ", " "), url,
                                   type="primary" if i == 0 else "secondary", use_container_width=True)
                if amt and "카카오페이" in pay_links(pay):
                    st.caption(f"카카오페이는 금액({won(amt)})을 직접 입력해야 해요.")

        section("오늘 모집", f"{len(today_parties)}건")
        if me and st.toggle("내 모집만 보기", key="only_mine"):
            today_parties = [p for p in today_parties if any(m["name"] == me for m in members_of(p))]
        # 시간 지난 모집은 숨김. 단 내가 낀 모집은 밥 먹고 정산해야 하니 남김
        past = [p for p in today_parties if party_status(p, now_hm) == "지난 모집" and not any(m["name"] == me for m in members_of(p))]
        if past and not st.toggle(f"지난 모집 {len(past)}건도 보기", key="show_past"):
            today_parties = [p for p in today_parties if p not in past]
        if not today_parties:
            st.markdown('<div class="card" style="text-align:center;color:#8B95A1">아직 모집이 없어요. 첫 모집을 올려보세요.</div>',
                        unsafe_allow_html=True)
        for p in today_parties:
            ms, st_ = members_of(p), party_status(p, now_hm)
            r = by_name.get(p["place"])
            badge = {"모집중": "ok", "마감": "no", "지난 모집": "no"}[st_]
            mine = any(m["name"] == me for m in ms)
            host = host_of(p)
            st.markdown(
                f'<div class="card party{" focus" if ss.get("focus_party") == p["id"] else ""}"><div class="h">'
                f'<span class="tm">{e(p["time"])}</span><span class="chip {badge}">{st_} · {count_text(p)}</span></div>'
                f'<div class="pl">{e(label(r) if r else p["place"])}</div>'
                f'<div style="margin-top:4px"><span class="chip">{e(p.get("spot") or "식당 앞")}</span>'
                f'<span class="chip">{e(r.get("gate") or r["where"]) if r else ""}</span></div>'
                + (f'<div class="note">{e(p["note"])}</div>' if p.get("note") else "")
                + '<div class="mem">' + "".join(
                    f'<span><b>{e(shown(m.get("nick") or m["name"]))}</b>{"<em>방장</em>" if m["name"] == host else ""}'
                    f'{"<em>결제</em>" if m["name"] == payer_of(p) and len(ms) > 1 else ""}{" · " + e(m["menu"]) if m.get("menu") else ""}</span>'
                    for m in ms) + "</div>"
                + (f'<div class="bar"><i style="width:{min(100, 100 * len(ms) // p["max"])}%"></i></div>' if p.get("max") else "")
                + "</div>",
                unsafe_allow_html=True)
            # 상황에 맞는 버튼만 골라 그 개수만큼 칸을 나눔 (휴대폰에서 글자가 안 찌그러지게)
            acts = []
            if mine:
                acts.append(lambda c, p=p, r=r: menu_editor(c, p, r))
                acts.append(lambda c: c.button("나가기", key=f"out_{p['id']}", use_container_width=True,
                                               on_click=act, args=(leave_party, p["id"], me)))
            elif st_ != "지난 모집":  # 초대 링크 없이 들어온 사람은 방장 수락이 필요
                asked = any(x["name"] == me for x in p.get("pending", []))
                acts.append(lambda c, asked=asked: c.button(
                    "요청 보냄" if asked else "참여 요청", key=f"in_{p['id']}", type="primary", use_container_width=True,
                    disabled=not me or asked or st_ != "모집중", on_click=act, args=(request_join, p["id"], me, my_nick, my_menu)))
            if me and me == host:
                acts.append(lambda c: manage(c, p))
            elif is_admin(me):  # 운영자: 부적절한 모집 삭제
                def admin_del(c, pid=p["id"]):
                    with c.popover("운영", use_container_width=True):
                        sure = st.checkbox("운영자 권한으로 이 모집을 삭제할게요", key=f"adm_{pid}")
                        st.button("모집 삭제", key=f"admdel_{pid}", type="primary", disabled=not sure,
                                  use_container_width=True, on_click=act, args=(delete_party, pid, me))
                acts.append(admin_del)
            if r:
                acts.append(lambda c: c.link_button("길찾기", route_url(r), use_container_width=True))
            for c, draw in zip(st.columns(len(acts)), acts):
                draw(c)
            if me and me == host and p.get("pending"):  # 방장: 참여 요청 수락/거절
                st.markdown(f'<div class="s" style="margin-top:6px">참여 요청 {len(p["pending"])}건</div>', unsafe_allow_html=True)
                for q in p["pending"]:
                    qa, qb, qc = st.columns([3, 1, 1])
                    qa.markdown(f'<div style="padding-top:8px"><b>{e(q.get("nick") or "친구")}</b>'
                                f'{" · " + e(q["menu"]) if q.get("menu") else ""}</div>', unsafe_allow_html=True)
                    qb.button("수락", key=f"ok_{p['id']}_{q['name']}", type="primary", use_container_width=True,
                              on_click=act, args=(answer_join, p["id"], me, q["name"], True))
                    qc.button("거절", key=f"no_{p['id']}_{q['name']}", use_container_width=True,
                              on_click=act, args=(answer_join, p["id"], me, q["name"], False))
            if mine and len(ms) > 1:
                with st.expander("정산하기"):
                    settle_panel(p, r)
            if mine:  # 초대 링크(수락 없이 바로 참여)는 참여자만 볼 수 있음
                with st.expander("친구 초대 링크", expanded=ss.get("focus_party") == p["id"]):
                    msg = (f"🍚 {p['time']} {label(r) if r else p['place']} 같이 먹을 사람! ({count_text(p)})\n"
                           f"{p.get('spot') or '식당 앞'}에서 만나요")
                    link = f"{base}/?party={p['id']}"
                    if public:  # 카카오 SDK는 콘솔에 등록한 도메인(배포 주소)에서만 동작
                        components.html(kakao_share_html(msg, link), height=56)
                    st.caption("링크를 누르면 이 모집이 바로 열려요. 카톡 말고 다른 곳엔 아래 글을 복사해서 보내세요"
                               + ("" if public else " (지금은 같은 와이파이에서만 열려요)"))
                    st.code(f"{msg}\n{link}", language=None)


if __name__ == "__main__":
    main()
