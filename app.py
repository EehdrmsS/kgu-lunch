"""경기대 수원캠퍼스 점심 추천기.  실행: streamlit run app.py --server.address 0.0.0.0"""
import datetime
import json
import os
import random
import re
import socket
import threading
import urllib.parse
from pathlib import Path

DIR = Path(__file__).parent
DATA = json.loads((DIR / "data.json").read_text("utf-8"))
STATE_FILE = Path(os.environ.get("LUNCH_STATE") or DIR / "state.json")  # 테스트할 땐 다른 파일로
CENTER = "오늘 점심"
_lock = threading.Lock()  # 여러 친구가 동시에 눌러도 state.json이 꼬이지 않게


# ---------- 순수 로직 (test_app.py에서 검사) ----------
CATS = ["학식", "한식", "분식", "중식", "일식·아시안", "양식", "패스트푸드", "기타"]  # crawl.py KAKAO_CAT과 맞춤


def mandalart_cells(cats=CATS):
    """3x3 만다라트: 가운데 칸이 주제, 나머지 8칸이 후보 카테고리."""
    return cats[:4] + [CENTER] + cats[4:]


def filter_menus(restaurants, cats=None, lo=0, hi=10**9, where=None):
    # 가격 미확인(None) 메뉴는 가격 필터에서 빼지 않고 보여줌
    return [(r, m) for r in restaurants for m in r["menus"]
            if (cats is None or r["category"] in cats)
            and (where is None or r["where"] == where)
            and (m["price"] is None or lo <= m["price"] <= hi)]


def won(p):
    return "가격 미확인" if p is None else f"{p:,}원"


def candidate_groups(picked, cands):
    """고른 카테고리마다 식당 전부 [(식당, [메뉴...])]. 가격 아는 식당 → 최저가 싼 순."""
    out = {}
    for c in picked:
        groups = {}
        for r, m in cands:
            if r["category"] == c:
                groups.setdefault(r["name"], (r, []))[1].append(m)
        low = lambda g: min((m["price"] for m in g[1] if m["price"] is not None), default=None)
        out[c] = sorted(groups.values(), key=lambda g: (low(g) is None, low(g) or 0))
    return out


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


BOARD_CSS = """<style>
.bd{display:flex;flex-direction:column;gap:8px;margin:8px 0 12px}
.bd-row{display:flex;gap:8px;border:1px solid rgba(255,75,75,.45);border-radius:10px;padding:6px;background:rgba(255,75,75,.04)}
.bd-cat{flex:0 0 88px;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:8px;
  background:rgba(255,75,75,.6);color:#fff;font-weight:700;font-size:14px;text-align:center;padding:6px 2px}
.bd-cat small{font-weight:400;opacity:.85;font-size:11px}
.bd-chips{display:flex;flex-wrap:wrap;gap:6px;align-content:flex-start;padding:2px 0}
.bd-chip{padding:6px 11px;border-radius:999px;background:rgba(255,75,75,.13);font-size:13px;line-height:1.25}
.bd-chip small{opacity:.6;margin-left:5px;font-size:11px}
.bd-chip.chk{outline:2px solid rgba(255,75,75,.9);background:rgba(255,75,75,.28)}
.bd-none{opacity:.6;font-size:13px;padding:6px}
.bd-row.grow{animation:bdGrow .45s ease-out both;transform-origin:left center}
.bd-row.grow .bd-chip{animation:bdPop .3s ease-out both}
.bd-chip.flash{animation:bdFlash .25s ease-in-out both}
.bd-chip.pick{animation:bdPick 1s ease-in-out both}
@keyframes bdGrow{from{transform:scaleX(.2);opacity:0}to{transform:scaleX(1);opacity:1}}
@keyframes bdPop{from{transform:scale(.3);opacity:0}to{transform:scale(1);opacity:1}}
@keyframes bdFlash{50%{background:#ffd54a;color:#000;transform:scale(1.08)}}
@keyframes bdPick{0%{transform:scale(1)}50%{transform:scale(1.2);background:#ff4b4b;color:#fff}
  100%{transform:scale(1);background:#ff4b4b;color:#fff;outline:3px solid #ffd54a}}
@media (max-width:600px){.bd-cat{flex-basis:62px;font-size:12px}.bd-chip{font-size:12px;padding:5px 9px}}
@media (prefers-reduced-motion:reduce){.bd *,.bd-row{animation-duration:.01s!important;animation-delay:0s!important}}
</style>"""


def board_html(groups, new=(), chosen=(), pick_name=None, roll=False):
    """카테고리별 식당 이름판. 카테고리 한 줄 + 식당 이름 칩(개수 제한 없이 줄바꿈).
    new: 새로 켠 카테고리(펼침 애니메이션), chosen: ②에서 고른 식당, pick_name: 🎲 결과, roll: 방금 굴렸으면 룰렛 연출."""
    from html import escape as e
    order = [r["name"] for c in groups for r, _ in groups[c]]
    step = min(0.08, 1.6 / max(len(order), 1))  # 식당이 많아도 룰렛은 1.6초 안에 끝나게
    out = [BOARD_CSS, '<div class="bd">']
    for cat, items in groups.items():
        chips = []
        for k, (r, _) in enumerate(items):
            cls, delay = "bd-chip" + (" chk" if r["name"] in chosen else ""), ""
            if cat in new:
                delay = f"animation-delay:{0.2 + min(k, 20) * 0.04:.2f}s"
            if r["name"] == pick_name:
                cls += " pick"
                delay = f"animation-delay:{len(order) * step + 0.1:.2f}s" if roll else ""
            elif roll:
                cls += " flash"
                delay = f"animation-delay:{order.index(r['name']) * step:.2f}s"
            tag = r.get("court") or r.get("gate") or r["where"]
            chips.append(f'<span class="{cls}" style="{delay}">{e(r["name"])}<small>{e(tag)}</small></span>')
        body = "".join(chips) or '<span class="bd-none">이 가격대엔 식당이 없어요</span>'
        out.append(f'<div class="bd-row{" grow" if cat in new else ""}"><div class="bd-cat">{e(cat)}'
                   f'<small>{len(items)}곳</small></div><div class="bd-chips">{body}</div></div>')
    return "".join(out) + "</div>"


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
    s = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {"visits": {}, "parties": []}
    for i, p in enumerate(s["parties"]):
        p.setdefault("id", f"old{i}")  # 예전 형식(아이디 없음) 모집도 다룰 수 있게
    return s


def update_state(fn):
    with _lock:
        s = load_state()
        fn(s)
        STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), "utf-8")
        return s


def add_visit(name, when=None):
    """이용객 수 +1, 그리고 혼잡도 추정용으로 '언제 어디로 결정했는지' 기록 (최근 3000건만)."""
    t = (when or now_kst()).isoformat(timespec="minutes")

    def fn(s):
        s["visits"][name] = s["visits"].get(name, 0) + 1
        s["log"] = (s.get("log", []) + [{"t": t, "r": name}])[-3000:]
    update_state(fn)


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


def add_party(s, pid, host, place, time, spot="", max_n=4, note="", menu="", day=None):
    s["parties"].append({"id": pid, "date": day or today(), "place": place, "time": time, "spot": spot,
                         "max": max_n, "note": note, "host": host, "closed": False,
                         "members": [{"name": host, "menu": menu}]})


def join_party(s, pid, name, menu=""):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    ms = members_of(p)
    for m in ms:
        if m["name"] == name:  # 이미 참여 중이면 메뉴만 바꿈
            m["menu"] = menu or m["menu"]
            p["members"] = ms
            return None
    if p.get("closed"):
        return "마감된 모집이에요"
    if len(ms) >= p.get("max", 99):
        return "자리가 다 찼어요"
    p["members"] = ms + [{"name": name, "menu": menu}]
    return None


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
    return None


def close_party(s, pid, name, closed=True):
    p = find_party(s, pid)
    if not p:
        return "없어진 모집이에요"
    if host_of(p) != name:
        return "방장만 마감할 수 있어요"
    p["closed"] = closed
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


def lan_url():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # 패킷은 안 나감, 내 IP만 알아냄
            return f"http://{s.getsockname()[0]}:8501"
    except OSError:
        return "http://localhost:8501"


# ---------- 화면 ----------
PAGES = ["홈", "지도", "고르기", "같이 먹기"]
BUDGETS = {"6천원": 6000, "8천원": 8000, "1만원": 10000, "1만5천원": 15000, "상관없음": None}
REC_KEYS = ["rec_budget", "rec_where", "rec_types", "rec_words", "rec_unknown"]
CROWD_CLS = {"여유": "ok", "보통": "mid", "붐빔": "busy", "운영 안 함": "no"}
SPOTS = ["식당 앞에서 바로", "정문", "후문", "중앙도서관 앞", "학생회관 앞", "경기드림타워 앞"]
WEEKDAY = "월화수목금토일"

# 앱처럼 보이게: 밝은 테마 + 카드 + 하단 탭바. 특정 영역만 꾸밀 땐 .xxx-marker를 넣고 :has()로 그 블록만 잡음
APP_CSS = """<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css');
:root{--brand:#FF5A1F;--soft:#FFF2EC;--ink:#191F28;--sub:#4E5968;--muted:#8B95A1;--line:#EEF0F3;--bg:#F4F5F7}
html,body,.stApp,.stApp *:not(.material-icons):not([class*="icon"]):not([data-testid*="Icon"]):not([class*="material"]){font-family:'Pretendard Variable',Pretendard,-apple-system,'Malgun Gothic',sans-serif}
.stApp{background:var(--bg);color:var(--ink)}
header[data-testid="stHeader"]{background:transparent;height:0}
[data-testid="stToolbar"],[data-testid="stDecoration"],#MainMenu,footer,[data-testid="stStatusWidget"]{display:none!important}
.block-container{max-width:720px;padding:.6rem 1rem 7rem}
h1,h2,h3{letter-spacing:-.03em;color:var(--ink)}
p,li{color:var(--sub)}
/* 버튼 */
.stButton>button,.stLinkButton>a,[data-testid="stFormSubmitButton"]>button{border-radius:14px;border:1px solid var(--line);background:#fff;
  color:var(--ink);font-weight:600;padding:.55rem .9rem;box-shadow:0 1px 2px rgba(0,0,0,.04);transition:transform .08s,box-shadow .15s}
.stButton>button:hover,.stLinkButton>a:hover{border-color:#ffd2c0;color:var(--brand);box-shadow:0 4px 14px rgba(255,90,31,.12)}
.stButton>button:active{transform:scale(.97)}
.stButton>button[kind="primary"],[data-testid="stFormSubmitButton"]>button{background:var(--brand);color:#fff;border:none}
.stButton>button[kind="primary"]:hover{color:#fff;background:#E84E15}
/* 입력창 */
.stTextInput input,.stSelectbox [data-baseweb="select"]>div,.stMultiSelect [data-baseweb="select"]>div,.stTimeInput input,.stNumberInput input{border-radius:12px!important;background:#fff}
[data-testid="stExpander"]{background:#fff;border:1px solid var(--line);border-radius:16px}
[data-testid="stForm"]{background:#fff;border:1px solid var(--line);border-radius:18px;padding:1rem}
/* 카드 */
.card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:16px 18px;margin:6px 0 10px;box-shadow:0 2px 10px rgba(16,24,40,.04)}
.sec{font-size:17px;font-weight:700;color:var(--ink);margin:18px 2px 6px;letter-spacing:-.02em}
.sec small{font-size:12px;color:var(--muted);font-weight:500;margin-left:6px}
.chip{display:inline-block;padding:3px 9px;border-radius:999px;background:var(--bg);color:var(--sub);font-size:12px;font-weight:600;margin:2px 4px 2px 0}
.chip.brand{background:var(--soft);color:var(--brand)}.chip.ok{background:#E8F7EE;color:#12B76A}.chip.no{background:#F2F4F6;color:var(--muted)}
.hero{background:var(--brand);color:#fff;border-radius:24px;padding:20px 22px 22px;margin:4px 0 12px;
  box-shadow:none}
.hero .top{display:flex;justify-content:space-between;align-items:center;font-size:13px;opacity:.95;font-weight:600}
.hero h1{color:#fff;font-size:26px;margin:14px 0 4px;padding:0;line-height:1.25}
.hero p{color:rgba(255,255,255,.9);margin:0;font-size:14px}
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
  display:flex;flex-wrap:nowrap;justify-content:space-around;gap:0;padding:8px 6px calc(8px + env(safe-area-inset-bottom));box-shadow:0 -4px 20px rgba(0,0,0,.05)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label{flex:1;justify-content:center;margin:0;padding:6px 0;border-radius:12px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label>div:first-child{display:none}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label p{font-size:13px;font-weight:600;color:var(--muted)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label:has(input:checked) p{color:var(--brand);font-weight:800}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .nav-marker) div[role="radiogroup"] label:has(input:checked){background:var(--soft)}
/* 칩 모양 라디오 (매장·종류 고르기) */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"]{gap:6px;flex-wrap:wrap}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label{background:#fff;border:1px solid var(--line);border-radius:999px;padding:6px 14px;margin:0}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label>div:first-child{display:none}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label p{font-size:13px;font-weight:600;color:var(--sub)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label:has(input:checked){background:var(--ink);border-color:var(--ink)}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .chips-marker) div[role="radiogroup"] label:has(input:checked) p{color:#fff}
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
.traybar{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(64px + env(safe-area-inset-bottom));z-index:998;width:min(688px,calc(100% - 24px));
  display:flex;justify-content:space-between;align-items:center;background:var(--ink);color:#fff!important;text-decoration:none!important;
  border-radius:16px;padding:13px 18px;font-weight:700;font-size:15px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
.traybar b{color:#FFB38A;font-size:14px}
/* 추천 목록 줄 */
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button{width:100%;display:block;text-align:left;
  border-radius:16px;padding:12px 14px;margin-bottom:-6px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button p{margin:0;font-size:12px;color:var(--muted);white-space:normal}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button p:first-child{font-size:15px;color:var(--ink);margin-bottom:3px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker) .stButton>button p em{font-style:normal;color:var(--brand);font-weight:800;margin-left:6px}
[data-testid="stVerticalBlock"]:has(> [data-testid="element-container"] .rec-marker.on) .stButton>button{border:2px solid var(--brand);background:var(--soft)}
.pickcard{border:2px solid var(--brand)}.pickcard .k{font-size:12px;color:var(--brand);font-weight:700}
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
</style>"""


def main():
    import uuid
    from html import escape as e

    import streamlit as st
    import streamlit.components.v1 as components

    st.set_page_config(page_title="경기대 점심 뭐먹지", page_icon="🍚", layout="centered", initial_sidebar_state="collapsed")
    st.markdown(APP_CSS, unsafe_allow_html=True)
    ss = st.session_state
    ss.setdefault("tray", {})
    if ss.get("nav") not in PAGES:  # 탭 이름이 바뀐 뒤 예전 값이 남아 있으면 초기화
        ss.pop("nav", None)

    # 초대 링크(?party=아이디)로 들어오면 그 모집으로 바로 이동
    pid_link = st.query_params.get("party")
    if pid_link and ss.get("_linked") != pid_link:
        ss._linked, ss.focus_party, ss.nav = pid_link, pid_link, PAGES[3]

    with st.sidebar:
        st.markdown("### 데이터")
        if st.button("학식 정보 새로 가져오기", use_container_width=True):
            import crawl
            with st.spinner("경기대 홈페이지에서 가져오는 중..."):
                crawl.main()
            st.rerun()
        st.caption(DATA.get("_note", ""))

    rs = with_today(DATA["restaurants"], DATA, today())
    by_name = {r["name"]: r for r in rs}
    places = dict(place_groups(sorted(rs, key=lambda r: (r["where"] != "교내", r.get("gate") or "", r.get("court") or "", r["name"]))))
    state = load_state()
    visits = state["visits"]
    now = now_kst()
    now_hm = now.strftime("%H:%M")

    # ---- 공용 동작 (버튼 on_click 콜백: 다음 화면 그리기 전에 실행됨) ----
    def go(page, **kw):
        ss.nav = page
        for k, v in kw.items():
            ss[k] = v

    def decide(names):
        for n in dict.fromkeys(names):
            add_visit(n)
        ss.decided = names[0]
        st.toast(f"{label(by_name[names[0]])}(으)로 결정했어요")

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

    def chips_radio(label_, options, key):
        with st.container():
            st.markdown('<span class="chips-marker"></span>', unsafe_allow_html=True)
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

    # ---- 하단 탭바 ----
    with st.container():
        st.markdown('<span class="nav-marker"></span>', unsafe_allow_html=True)
        page = st.radio("화면", PAGES, key="nav", horizontal=True, label_visibility="collapsed")

    # =============== 🏠 홈 ===============
    if page == PAGES[0]:
        n_in = sum(1 for p in places.values() if p[0]["where"] == "교내")
        st.markdown(f'<div class="hero"><div class="top"><span>경기대 점심</span>'
                    f'<span>{now.month}월 {now.day}일 ({WEEKDAY[now.weekday()]})</span></div>'
                    f'<h1>오늘 점심,<br>뭐 먹을까요?</h1><p>수원캠퍼스 학식 {n_in}곳 · 주변 식당 {len(places) - n_in}곳</p></div>',
                    unsafe_allow_html=True)
        # ---- 조건 추천: 조건 → 추천 목록 → 메뉴를 누르면 식당·혼잡도 → 결정 / 메뉴판 / 같이 먹기 ----
        for k in REC_KEYS:  # 다른 탭 다녀와도 조건 유지
            if k not in ss and "_" + k in ss:
                ss[k] = ss["_" + k]
        ss.setdefault("rec_budget", "1만원")
        all_types = sorted({m.get("type") or r["category"] for r in rs for m in r["menus"]} - NOT_MEAL)
        section("조건으로 추천받기", "예산 · 위치 · 싫은 것")
        budget_lbl = chips_radio("예산", list(BUDGETS), key="rec_budget")
        where = chips_radio("위치", ["전체", "교내", "정문", "후문"], key="rec_where")
        no_types = st.multiselect("먹기 싫은 종류", all_types, key="rec_types", label_visibility="collapsed",
                                  placeholder="먹기 싫은 종류 (예: 면류, 돈까스)")
        no_words = st.text_input("빼고 싶은 단어", key="rec_words", label_visibility="collapsed", max_chars=60,
                                 placeholder="빼고 싶은 재료·단어 (예: 소고기, 해산물, 매운, 김치)")
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
                f'<div class="s">{e(", ".join(m["name"] for m in members_of(p)))}</div></div>'
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
        where = chips_radio("어디서", ["전체", "교내", "정문", "후문"], key="home_where")
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

    # =============== 🧩 고르기 (만다라트) ===============
    elif page == PAGES[2]:
        st.markdown('<div class="hero" style="padding:16px 20px"><div class="top"><span>만다라트로 고르기</span></div>'
                    '<h1 style="font-size:20px;margin-top:8px">당기는 칸을 켜면<br>식당이 펼쳐져요</h1></div>', unsafe_allow_html=True)
        lo, hi = st.slider("가격대(원)", 0, 20000, (0, 12000), step=500, key="price2")
        picked = []
        cells = mandalart_cells()
        for row in range(0, len(cells), 3):
            for col, cell in zip(st.columns(3), cells[row:row + 3]):
                if cell == CENTER:
                    col.button(CENTER, disabled=True, use_container_width=True)
                elif col.toggle(cell, key=f"cat_{cell}"):
                    picked.append(cell)

        groups = candidate_groups(picked, filter_menus(rs, cats=picked, lo=lo, hi=hi))
        new = [c for c in picked if c not in ss.get("prev_cats", [])]  # 새로 켠 줄만 애니메이션
        ss.prev_cats = picked
        board = st.empty()  # 식당 이름판: 아래 🎲 결과까지 반영해서 마지막에 그림
        menus_of = {r["name"]: ms for c in groups for r, ms in groups[c]}
        rest_of = {r["name"]: r for c in groups for r, _ in groups[c]}
        names = list(menus_of)
        roll, chosen_r = False, []
        if picked:
            chosen_r = st.multiselect("식당", names, key="pick_rest", label_visibility="collapsed",
                                      placeholder="② 식당 좁히기 (안 고르면 위 판 전부가 후보)",
                                      format_func=lambda n: f"{label(rest_of[n])} · {price_range(menus_of[n])}")
            chosen_m = []
            if chosen_r:
                opts = [(n, i) for n in chosen_r for i in range(len(menus_of[n]))]
                chosen_m = st.multiselect("메뉴", opts, key="pick_menu", label_visibility="collapsed",
                                          placeholder="③ 메뉴 좁히기 (안 고르면 고른 식당 메뉴 전부)",
                                          format_func=lambda o: f"{menus_of[o[0]][o[1]]['name']} · "
                                                                f"{won(menus_of[o[0]][o[1]]['price'])} — {o[0]}")
            pool = ([(rest_of[n], menus_of[n][i]) for n, i in chosen_m]
                    or [(rest_of[n], m) for n in (chosen_r or names) for m in menus_of[n]])
            if not pool:
                st.warning("이 가격대엔 후보가 없어요. 가격대를 넓혀보세요.")
            elif st.button("이 중에서 골라줘", type="primary", use_container_width=True):
                ss.suggest = random.choice(pool)
                roll = True
        sug = ss.get("suggest")
        board.markdown(board_html(groups, new, chosen_r, sug and sug[0]["name"], roll), unsafe_allow_html=True)
        if sug:
            result_card(*sug, key="mand")

    # =============== 👫 같이 먹기 ===============
    else:
        st.markdown('<div class="hero" style="padding:16px 20px"><div class="top"><span>같이 먹기</span>'
                    f'<span>{now_hm} 기준</span></div><h1 style="font-size:20px;margin-top:8px">혼밥 말고,<br>같이 먹을 사람 구해요</h1></div>',
                    unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        me = c1.text_input("내 이름", key="me", max_chars=20, placeholder="친구들이 알아볼 이름").strip()
        my_menu = c2.text_input("내가 먹을 메뉴 (선택)", key="my_menu", max_chars=40, placeholder="예: 숑숑카츠").strip()
        if not me:
            st.caption("이름을 먼저 적어야 모집하거나 참여할 수 있어요.")

        today_parties = sorted((p for p in state["parties"] if p["date"] == today()), key=lambda p: p["time"])
        ctx = getattr(st, "context", None)  # 최신 Streamlit(배포 서버)에만 있음, 로컬 1.32엔 없음
        base = share_base(ctx.headers.get("host") if ctx else None)
        public = base != lan_url()
        with st.expander("새 모집 만들기", expanded=bool(ss.get("party_place")) or not today_parties):
            with st.form("new_party", clear_on_submit=True, border=False):
                names = sorted(by_name, key=lambda n: (by_name[n]["where"] != "교내", label(by_name[n])))
                want = ss.get("party_place") or ss.get("decided")
                place = st.selectbox("식당", names, index=names.index(want) if want in names else 0,
                                     format_func=lambda n: f"{label(by_name[n])} · {by_name[n].get('gate') or by_name[n]['where']}")
                a, b = st.columns(2)
                when = a.time_input("몇 시에?", default_meet_time(now), step=600)
                spot = b.selectbox("어디서 만나요?", SPOTS)
                a, b = st.columns(2)
                max_n = a.slider("최대 인원", 2, 8, 4)
                note = b.text_input("한마디 (선택)", max_chars=60, placeholder="예: 3층 강의 끝나고 바로!")
                if st.form_submit_button("모집 올리기", use_container_width=True):
                    if not me:
                        st.toast("위에 이름을 먼저 적어주세요")
                    else:
                        pid = uuid.uuid4().hex[:6]
                        update_state(lambda s: add_party(s, pid, me, place, when.strftime("%H:%M"), spot, max_n, note.strip(), my_menu))
                        ss.focus_party = pid
                        ss.pop("party_place", None)
                        st.rerun()

        section("오늘 모집", f"{len(today_parties)}건")
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
                    f'<span><b>{e(m["name"])}</b>{"<em>방장</em>" if m["name"] == host else ""}{" · " + e(m["menu"]) if m.get("menu") else ""}</span>'
                    for m in ms) + "</div>"
                + (f'<div class="bar"><i style="width:{min(100, 100 * len(ms) // p["max"])}%"></i></div>' if p.get("max") else "")
                + "</div>",
                unsafe_allow_html=True)
            # 상황에 맞는 버튼만 골라 그 개수만큼 칸을 나눔 (휴대폰에서 글자가 안 찌그러지게)
            acts = []
            if mine:
                acts.append(lambda c: c.button("메뉴 수정", key=f"upd_{p['id']}", disabled=not my_menu, use_container_width=True,
                                               on_click=act, args=(join_party, p["id"], me, my_menu)))
                acts.append(lambda c: c.button("나가기", key=f"out_{p['id']}", use_container_width=True,
                                               on_click=act, args=(leave_party, p["id"], me)))
            elif st_ != "지난 모집":
                acts.append(lambda c: c.button("참여하기", key=f"in_{p['id']}", type="primary", use_container_width=True,
                                               disabled=not me or st_ != "모집중", on_click=act, args=(join_party, p["id"], me, my_menu)))
            if me and me == host:
                acts.append(lambda c: c.button("다시 열기" if p.get("closed") else "마감", key=f"cl_{p['id']}",
                                               use_container_width=True, on_click=act, args=(close_party, p["id"], me, not p.get("closed"))))
            if r:
                acts.append(lambda c: c.link_button("길찾기", route_url(r), use_container_width=True))
            for c, draw in zip(st.columns(len(acts)), acts):
                draw(c)
            with st.expander("친구 초대 링크", expanded=ss.get("focus_party") == p["id"] and mine):
                st.caption("오른쪽 위 복사 버튼 → 카톡에 붙여넣기. 링크를 누르면 이 모집이 바로 열려요"
                           + ("" if public else " (지금은 같은 와이파이에서만 열려요)"))
                st.code(f"🍚 {p['time']} {label(r) if r else p['place']} 같이 먹을 사람! ({count_text(p)})\n"
                        f"{p.get('spot') or '식당 앞'}에서 만나요\n{base}/?party={p['id']}", language=None)


if __name__ == "__main__":
    main()
