import tempfile
from pathlib import Path

import app
import crawl

rs = app.DATA["restaurants"]

assert all(r["category"] in app.CATS for r in rs)

# 가격 필터: 범위 밖은 빠지고, 가격 미확인(None)은 남음
rows = app.filter_menus(rs, hi=5000)
assert all(m["price"] is None or m["price"] <= 5000 for _, m in rows)
assert any(m["price"] is None for _, m in rows)
assert all(r["where"] == "교내" for r, _ in app.filter_menus(rs, where="교내"))
assert app.filter_menus(rs, cats=[]) == []
assert app.won(None) == "가격 미확인" and app.won(5500) == "5,500원"
assert app.route_url(rs[0]).startswith("https://map.kakao.com/link/to/")

# 고르기 목록: 종류·위치·예산으로 거르고 싼 순, 음료·사이드는 빼고 가격 미확인 식당은 뒤로
pl = app.pick_list(rs, "학식")
assert {r["name"] for r, _ in pl} == {r["name"] for r in rs if r["category"] == "학식"}
assert {"경기드림타워(기숙사) 식당", "샐리박스 경기대점", "E-스퀘어 스퀘어식당"} <= {r["name"] for r, _ in pl}
assert all(r["gate"] == "정문" for r, _ in app.pick_list(rs, where="정문"))
assert all(r["where"] == "교내" for r, _ in app.pick_list(rs, where="교내"))
cheap = app.pick_list(rs, budget=6000)
assert all(m["price"] is None or m["price"] <= 6000 for _, ms in cheap for m in ms)
assert all((m.get("type") or "") not in app.NOT_MEAL for _, ms in cheap for m in ms)
lows = [min((m["price"] for m in ms if m["price"]), default=None) for _, ms in app.pick_list(rs)]
known = [x for x in lows if x is not None]
assert known == sorted(known) and lows[:len(known)] == known  # 싼 순, 가격 모르는 곳은 맨 뒤
assert app.price_range([{"price": 3500}, {"price": None}, {"price": 6000}]) == "3,500~6,000원"
esq = next(r for r in rs if r["name"] == "E-스퀘어 스퀘어식당")
assert app.price_range([{"price": None}]) == "가격 미확인" and app.price_range([{"price": 7000}]) == "7,000원"

# 캠퍼스 지도: 모든 식당 핀, 팝업에 메뉴 전부, 스크립트 끊김 방지
import json
geo = json.loads(app.GEO_FILE.read_text("utf-8"))
assert len(geo["campus"]) > 10 and any(b["name"] == "감성코어" for b in geo["buildings"])
mh = app.map_html(rs, geo, app.DATA, {}, "감성코어 식당")
payload = json.loads(mh.split("const D=")[1].split(";\nconst map")[0].replace("<\\/", "</"))
places = app.place_groups(rs)
assert len(payload["pins"]) == len(places) and payload["focus"] == "감성코어 식당"
assert all(c in app.CAT_COLORS for c in {p["c"] for p in payload["pins"]})
esq_pop = next(p["pop"] for p in payload["pins"] if p["n"] == "E-스퀘어 스퀘어식당")
assert all(m["name"] in esq_pop for m in esq["menus"])
assert "<tr class=g>" in esq_pop  # 종류별 묶음 머리줄

# 푸드코트: 경슐랭 매장 5곳이 핀 하나로, 팝업에 매장별 접이식 칸
stalls = [r for r in rs if r.get("court") == "경슐랭"]
assert [r["name"] for r in stalls] == ["위델가", "만권화밥", "숑숑돈까스", "버거&타코", "신머이쌀국수"]
assert all(len(r["menus"]) > 10 for r in stalls)
court = dict(places)["경슐랭"]
assert len(court) == 5
kpop = next(p["pop"] for p in payload["pins"] if p["n"] == "경슐랭")
assert kpop.count("<details>") == 5 and "숑숑카츠" in kpop and "버거&amp;타코" in kpop
assert app.label(stalls[0]) == "경슐랭 · 위델가" and app.label(esq) == "E-스퀘어 스퀘어식당"
assert list(app.by_type([{"name": "a", "type": "면류"}, {"name": "b"}, {"name": "c", "type": "면류"}])) == ["면류", "메뉴"]
evil = app.map_html([{"name": "</script><b>x", "category": "학식", "where": "교내", "lat": 1, "lng": 2,
                      "menus": [{"name": "<img onerror=1>", "price": None}]}], {}, app.DATA)
assert "</script><b>" not in evil and "<img onerror" not in evil

# 카테고리 보정 (prices.json)
cat = {r["name"]: r["category"] for r in rs}
assert cat.get("핵밥 수원경기대점") == "일식·아시안" and cat.get("김가네한식뷔페") == "한식"

# 오늘 식단이 백반 메뉴 이름에 붙음
data = {"weekly": {"days": {"D": ["쌀밥", "콩나물국", "제육"]}},
        "dorm_weekly": {"days": {"D": {"점심": ["쌀밥", "수제비국"], "저녁": []}}}}
t = app.with_today(rs, data, "D")
names = {(r["name"], m["name"]) for r in t for m in r["menus"]}
assert ("감성코어 식당", "한식 백반 (오늘: 콩나물국, 제육…)") in names
assert ("경기드림타워(기숙사) 식당", "점심 백반 (오늘: 수제비국…)") in names
assert ("경기드림타워(기숙사) 식당", "저녁 백반") in names  # 빈 식단이면 그대로
assert app.with_today(rs, {}, "D") == rs

# 크롤러: 기숙사 금주의 식단
dorm = crawl.parse_dorm((Path(__file__).parent / "fixtures/dorm_week.html").read_bytes().decode("cp949"))
assert list(dorm) == ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
assert dorm["2026-11-10"]["점심"][:3] == ["쌀밥", "호박수제비국", "숯불제육불고기"], dorm["2026-11-10"]

# 방문 수 저장
app.STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"
app.add_visit("A"); app.add_visit("A")
assert app.load_state()["visits"]["A"] == 2
assert app.add_visit("C", uid="k1") is True and app.add_visit("C", uid="k1") is False  # 같은 사람 하루 1번
assert app.add_visit("C", uid="k2") is True and app.load_state()["visits"]["C"] == 2

# 같이 먹기 모집: 정원·마감·나가기·방장 넘기기·옛 형식
s = {"parties": [], "visits": {}}
app.add_party(s, "p1", "철수", "감성코어 식당", "12:30", "정문", 3, "고고", "백반", day="D")
assert app.join_party(s, "p1", "영희", "돈까스") is None
assert app.join_party(s, "p1", "영희", "라면") is None and len(app.members_of(s["parties"][0])) == 2  # 중복 참여 X, 메뉴만 수정
assert app.members_of(s["parties"][0])[1]["menu"] == "라면"
assert app.join_party(s, "p1", "민수") is None
assert app.join_party(s, "p1", "지수") == "자리가 다 찼어요"
assert app.party_status(s["parties"][0], "12:00") == "마감" and app.party_status(s["parties"][0], "13:00") == "지난 모집"
assert app.close_party(s, "p1", "영희") == "방장만 마감할 수 있어요"
assert app.leave_party(s, "p1", "철수") is None and app.host_of(s["parties"][0]) == "영희"  # 방장 넘김
assert app.leave_party(s, "p1", "민수") is None and app.party_status(s["parties"][0], "12:00") == "모집중"
assert app.close_party(s, "p1", "영희") is None and app.join_party(s, "p1", "지수") == "마감된 모집이에요"
assert app.leave_party(s, "p1", "영희") is None and s["parties"] == []  # 마지막 사람 나가면 삭제
assert app.join_party(s, "p1", "x") == "없어진 모집이에요"
old = {"id": "old0", "time": "12:00", "members": ["동근"]}
assert app.members_of(old) == [{"name": "동근", "menu": ""}] and app.host_of(old) == "동근"
assert app.count_text(old) == "1명" and app.count_text({"max": 4, "members": ["a", "b"]}) == "2/4명"
s2 = {"parties": [old]}
assert app.join_party(s2, "old0", "철수") is None and app.close_party(s2, "old0", "동근") is None
assert app.edit_party(s2, "old0", "철수", "12:30", "정문", 4, "") == "방장만 고칠 수 있어요"
assert app.edit_party(s2, "old0", "동근", "12:30", "정문", 1, "") is None and s2["parties"][0]["max"] == 2  # 2~8로 맞춤
s2["parties"][0]["members"].append({"name": "영희", "menu": ""})
assert app.edit_party(s2, "old0", "동근", "12:30", "정문", 2, "") == "이미 3명이 모였어요"
s2["parties"][0]["members"].pop()
assert app.edit_party(s2, "old0", "동근", "1230; x", "정문", 4, "") == "시간을 확인해주세요"
assert app.edit_party(s2, "old0", "동근", "12:30", "<script>", 4, "x" * 500) is None
assert s2["parties"][0]["spot"] == "" and len(s2["parties"][0]["note"]) == 60  # 목록 밖 장소·긴 글은 막음
assert app.edit_party(s2, "old0", "동근", "12:30", "정문", 4, "늦지마") is None and s2["parties"][0]["time"] == "12:30"
assert app.delete_party(s2, "old0", "철수") == "방장만 삭제할 수 있어요" and len(s2["parties"]) == 1
assert app.delete_party(s2, "old0", "동근") is None and s2["parties"] == []
assert app.delete_party(s2, "old0", "동근") == "없어진 모집이에요"

# 정산
rr = {"menus": [{"name": "A", "price": 5000}, {"name": "B", "price": 3000}, {"name": "C", "price": None}]}
assert app.menu_total(rr, "A, B") == 8000 and app.menu_total(rr, "A, C") is None and app.menu_total(rr, "") is None
s3 = {"parties": []}
app.add_party(s3, "q1", "방장", "x", "12:00", menu="A")
app.join_party(s3, "q1", "친구", "A, B")
pp = s3["parties"][0]
assert app.settle(pp, rr) == [("방장", 5000), ("친구", 8000)]  # 기본은 메뉴별
assert app.set_pay(s3, "q1", "친구", "me", "", "N빵") == "결제 담당자만 정산을 설정할 수 있어요"
assert app.set_pay(s3, "q1", "방장", "bad id!", "", "N빵") and app.set_pay(s3, "q1", "방장", "", "http://evil.com", "N빵")
assert app.set_pay(s3, "q1", "방장", "me", "https://qr.kakaopay.com/Ej7abc", "N빵") is None
assert app.settle(pp, rr) == [("방장", 6500), ("친구", 6500)]  # 13000 ÷ 2
assert app.set_pay(s3, "q1", "방장", "me", "", "N빵", 10001) is None and app.settle(pp, rr)[0][1] == 5010  # 10원 올림
assert app.pay_links(pp["pay"], 5010) == {"토스": "https://toss.me/me/5010"}

import datetime as dt
T = lambda h, m: dt.datetime(2026, 11, 10, h, m)
assert app.default_meet_time(T(9, 5)) == dt.time(12, 0)
assert app.default_meet_time(T(13, 51)) == dt.time(14, 0) and app.default_meet_time(T(13, 50)) == dt.time(14, 0)
assert app.default_meet_time(T(23, 58)) == dt.time(23, 50)

# 혼잡도 추정: 시간대 + 앱 기록 + 같이 먹기
gam = next(r for r in rs if r["name"] == "감성코어 식당")      # 운영 10:30~16:00, 주말 휴무
dorm_r = next(r for r in rs if r["name"].startswith("경기드림타워"))  # 중식 11:00~13:30, 석식 17:00~19:00
assert app.crowd(gam, T(12, 10))["level"] == "붐빔" and app.crowd(gam, T(15, 0))["level"] == "여유"
assert app.crowd(gam, T(9, 0))["level"] == "운영 안 함" and app.crowd(gam, dt.datetime(2026, 11, 14, 12, 0))["level"] == "운영 안 함"
assert app.crowd(dorm_r, T(15, 0))["level"] == "운영 안 함" and app.crowd(dorm_r, T(17, 30))["level"] != "운영 안 함"
log = [{"t": "2026-11-10T14:40", "r": "감성코어 식당"}] * 3
c = app.crowd(gam, T(15, 0), log)
assert c["level"] == "보통" and "최근 1시간 3명이 여기로 결정" in c["why"]
party = [{"place": "감성코어 식당", "date": "2026-11-10", "time": "15:10", "members": ["a", "b", "c", "d"]}]
assert app.crowd(gam, T(15, 0), (), party)["score"] > app.crowd(gam, T(15, 0))["score"]
assert app.better_time(gam, T(12, 10)) is not None and app.better_time(gam, T(15, 0)) is None
app.STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"
app.add_visit("B", T(12, 0))
assert app.load_state()["log"] == [{"t": "2026-11-10T12:00", "r": "B"}]

# 조건 추천
recs = app.recommend(rs, budget=8000, where="교내", no_types=["면류"], no_words=["돈까스", "카츠"], dt=T(12, 10))
assert recs and all(r["where"] == "교내" and m["price"] <= 8000 and m.get("type") != "면류"
                    and "돈까스" not in m["name"] and "카츠" not in m["name"] for r, m, _, _ in recs)
assert all(m.get("type") not in app.NOT_MEAL and m["price"] >= 3000 for _, m, _, _ in recs)  # 사이드·음료 제외
from collections import Counter
assert max(Counter(r["name"] for r, *_ in recs).values()) <= 2  # 한 식당 최대 2개
assert [x[2] for x in recs] == sorted((x[2] for x in recs), reverse=True)
assert all(m["price"] is not None for _, m, _, _ in recs)
assert any(m["price"] is None for _, m, _, _ in app.recommend(rs, where="후문", with_unknown=True, limit=80, per_rest=5, dt=T(12, 10)))
# 토요일 교내: 주말 휴무인 곳은 빠지고 주말에도 여는 E-스퀘어만 남음
assert {r["name"] for r, *_ in app.recommend(rs, dt=dt.datetime(2026, 11, 14, 12, 0), where="교내", limit=50)} == {"E-스퀘어 스퀘어식당"}
gate = app.recommend(rs, where="정문", dt=T(12, 10), limit=50)
assert gate and all(r.get("gate") == "정문" for r, *_ in gate)
import random as _r
assert app.recommend(rs, 10000, jitter=_r.Random(1), dt=T(12, 10)) != app.recommend(rs, 10000, jitter=_r.Random(2), dt=T(12, 10))

# 재료 거르기: '소고기'를 빼면 이름에 소고기가 없는 소고기 메뉴도 빠짐
allm = lambda **kw: app.recommend(rs, budget=None, limit=999, per_rest=999, dt=T(12, 10), with_unknown=True, **kw)
names = lambda xs: {m["name"] for _, m, _, _ in xs}
no_beef = names(allm(no_words=["소고기"]))
for beef in ["우삼겹 짬뽕라면", "육개장", "갈비탕", "규동(M)", "차돌된장찌개", "한우소머리국밥", "직화소고기덮밥", "큐브스테이크덮밥",
             "비프치즈 핫", "뚝배기불고기", "사골 떡만두국", "양지쌀국수", "퍼싸오", "대창덮밥", "육회동(M)",
             "물냉면", "비빔냉면", "역전해장국", "떡만두국", "슈프림타코"]:
    assert beef not in no_beef, beef
for keep in ["돼지불백덮밥", "닭반마리쌀국수", "치킨버거 마일드", "삼겹가지덮밥", "직화제육덮밥", "맑은돼지곰탕(보통)",
             "핫치킨타코", "폴드포크타코", "물막국수"]:
    assert keep in no_beef, keep
assert names(allm(no_words=["소"])) == no_beef and names(allm(no_words=["쇠고기"])) == no_beef  # 다른 말로 적어도 같음
no_sea = names(allm(no_words=["해산물"]))
assert not {"오징어덮밥", "새우볶음밥", "연어덮밥", "참치마요덮밥", "어묵우동", "불맛해물짬뽕", "오삼덮밥"} & no_sea
no_spicy = names(allm(no_words=["매운", "것"]))
assert not {"마라우동", "핵매운돈까스", "육개장", "얼큰순대라면", "맵달라볶이", "국물떡볶이"} & no_spicy
assert {"로제떡볶이", "까르보 말랑떡볶이", "샐리핫도그"} <= no_spicy
no_pork = names(allm(no_words=["돼지고기"]))
assert not {"숑숑카츠", "돈까스", "제육덮밥", "스팸마요덮밥", "순대국"} & no_pork
assert "치킨까스(140g)" in no_pork and "직화제육덮밥" not in no_pork
assert "우삼겹 짬뽕라면" in no_pork  # '우삼겹'은 소고기라 돼지고기 거르기에선 남음
# 주변 식당(메뉴 모름)은 식당 이름으로 거름: '닭' 빼면 숯닭집이 빠짐
assert not any("숯닭" in r["name"] for r, *_ in allm(no_words=["닭"]))
# 오늘 학식은 제목에 안 보이는 반찬까지 봄
wk = {"weekly": {"days": {"D": ["쌀밥", "콩나물국", "김치", "깍두기", "소불고기"]}}}
t_rs = app.with_today(rs, wk, "D")
assert "한식 백반" not in {m["name"].split(" (")[0] for r, m, *_ in app.recommend(t_rs, no_words=["소고기"], limit=999, per_rest=999, dt=T(12, 10))
                              if r["name"] == "감성코어 식당"}
rules, groups, lit = app.expand_words(["소고기", "매운 것".split()[1], "위델가"])
assert groups == ["소고기"] and lit == ["위델가"]
assert not any(r["name"] == "위델가" for r, *_ in allm(no_words=["위델가"]))

# 키오스크 장바구니
tray = {}
r_ = {"name": "만권화밥", "court": "경슐랭"}
app.tray_add(tray, r_, {"name": "돼지불백덮밥", "price": 5500})
app.tray_add(tray, r_, {"name": "돼지불백덮밥", "price": 5500})
app.tray_add(tray, r_, {"name": "라면", "price": None})
assert app.tray_total(tray) == (11000, True) and app.tray_text(tray) == "돼지불백덮밥×2, 라면"
app.tray_add(tray, r_, {"name": "라면", "price": None}, -1)
assert app.tray_total(tray) == (11000, False) and list(tray.values())[0]["label"] == "경슐랭 · 만권화밥"

# 크롤러: 홈페이지 HTML 조각
page = '''<h3>A식당</h3><span class="title">운영시간</span>
<span class="text">주말 휴무<br>
 10:30~19:00</span><h3>B식당</h3><span class="title">위치</span> <span class="text"> 1층</span>
<a data-pdf="/x/w.pdf" data-start="2026-11-09" data-end="2026-11-14">'''
assert crawl.parse_cafeterias(page) == {"A식당": {"운영시간": "주말 휴무 / 10:30~19:00"}, "B식당": {"위치": "1층"}}
assert crawl.week_pdfs(page) == [("/x/w.pdf", "2026-11-09", "2026-11-14")]

# 크롤러: 실제 감성코어 주간메뉴 PDF (2026-11-09 주)
days = crawl.parse_week_pdf((Path(__file__).parent / "fixtures/gamseong_week.pdf").read_bytes(), "2026-11-09")
assert days["2026-11-09"][1] == "모둠햄부대찌개", days
assert days["2026-11-11"][2] == "시골장터국밥(대)", days
assert days["2026-11-13"] == ["쌀밥", "콩나물밥(양념장)", "된장국", "춘천닭갈비", "막국수", "계절나물", "배추김치"], days

# 크롤러: 카카오 응답 → 식당
r = crawl.kakao_to_restaurant({"place_name": "X", "category_name": "음식점 > 일식 > 돈까스", "x": "127.04", "y": "37.29",
                               "place_url": "u", "id": "1"}, "후문")
assert r["category"] == "일식·아시안" and r["lat"] == 37.29 and r["menus"][0]["price"] is None
r = crawl.kakao_to_restaurant({"place_name": "X", "category_name": "음식점 > 한식", "x": "1", "y": "2", "place_url": "u", "id": "1"},
                              "후문", {"X": {"source": "s", "menus": [["국밥", 9000]]}})
assert r["menus"] == [{"name": "국밥", "price": 9000}] and r["price_source"] == "s"
assert crawl.kakao_to_restaurant({"place_name": "Y", "category_name": "음식점 > 술집", "x": "1", "y": "2",
                                  "place_url": "u", "id": "2"}, "정문")["category"] == "기타"
# 초대 링크 주소: APP_URL > 접속한 공개 주소 > 같은 와이파이 주소
import datetime as _dt
import os as _os
_os.environ.pop("APP_URL", None)
assert app.share_base("kgu-lunch.streamlit.app") == "https://kgu-lunch.streamlit.app"
assert app.share_base("localhost:8501") == app.share_base("192.168.0.5:8501") == app.share_base(None) == app.lan_url()
_os.environ["APP_URL"] = "https://my.app/"
assert app.share_base("anything.com") == "https://my.app"
_os.environ.pop("APP_URL")

# 한국 시간 (서버가 UTC여도)
diff = app.now_kst() - _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
assert abs(diff - _dt.timedelta(hours=9)) < _dt.timedelta(minutes=1), diff
assert app.today() == app.now_kst().date().isoformat()

# 카카오 로그인 state 서명
st_ = app.sign_state("ab12cd", now=1000)
assert app.check_state(st_, now=1100) == "ab12cd" and app.check_state(app.sign_state(now=1000), now=1000) == ""
assert app.check_state(st_, now=1000 + 1801) is None  # 30분 지나면 만료
assert app.check_state(st_.replace("ab12cd", "zz99zz"), now=1100) is None  # 모집 id 바꿔치기
assert app.check_state("garbage", now=1) is None and app.check_state("a.b.c", now=1) is None
u = app.kakao_auth_url("KEY", "https://x.app", st_)
assert u.startswith("https://kauth.kakao.com/oauth/authorize?") and "redirect_uri=https%3A%2F%2Fx.app" in u
_os.environ.pop("APP_URL", None)
assert app.login_redirect("localhost:8501") == "http://localhost:8501" and app.login_redirect(None) == "http://localhost:8501"
assert app.login_redirect("kgu-lunch.streamlit.app") == "https://kgu-lunch.streamlit.app"
# 모집 한도
s4 = {"parties": []}
for i in range(app.MAX_PER_HOST):
    assert app.add_party(s4, f"h{i}", "k1", "x", "12:00") is None
assert app.add_party(s4, "h9", "k1", "x", "12:00") == f"모집은 하루 {app.MAX_PER_HOST}개까지 올릴 수 있어요"
assert app.add_party(s4, "h9", "k2", "x", "12:00", max_n=99, menu="m" * 999) is None
assert s4["parties"][-1]["max"] == 8 and len(s4["parties"][-1]["members"][0]["menu"]) == 100
assert app.nick_of({"members": [{"name": "k1", "nick": "동근", "menu": ""}]}, "k1") == "동근"
s5 = {"parties": [{"id": "a", "host": "이동근", "members": [{"name": "이동근", "menu": ""}]},
                  {"id": "b", "host": "k123", "members": [{"name": "k123", "nick": "동근", "menu": ""}]}]}
for p in s5["parties"]:
    p["date"] = "2026-11-11"
s5["parties"].append({"id": "c", "date": "2026-11-03", "host": "k9", "members": [{"name": "k9", "menu": ""}]})
s5["parties"].append({"id": "d", "date": "2026-11-04", "host": "k9", "members": [{"name": "k9", "menu": ""}]})
assert app.drop_legacy(s5, "2026-11-11") == 2 and [p["id"] for p in s5["parties"]] == ["b", "d"]  # 로그인 전 + 7일 지난 것
# 내 프로필
s6 = {"parties": []}
app.add_party(s6, "p6", "k7", "x", "12:00", nick="카톡닉", day="2026-11-11")
assert app.set_profile(s6, "k7", "  ") == "이름을 적어주세요"
assert app.set_profile(s6, "k7", "동근", toss="bad id!") and app.set_profile(s6, "k7", "동근", kakao="http://evil.com")
assert app.set_profile(s6, "k7", "동근" * 20, toss="dong") is None and s6["profiles"]["k7"]["nick"] == ("동근" * 20)[:20]
assert s6["parties"][0]["members"][0]["nick"] == ("동근" * 20)[:20]  # 이미 낀 모집 이름도 바뀜
assert app.my_parties(s6, "k7", "2026-11-11") == [(s6["parties"][0], True)] and app.my_parties(s6, "k8", "2026-11-11") == []
assert app.set_profile(s6, "k7", "동근", split="반반") == "설정값을 확인해주세요"
assert app.set_profile(s6, "k7", "동근", budget="100만원") and app.set_profile(s6, "k7", "동근", where="달나라")
assert app.set_profile(s6, "k7", "동근", "dong", "", "N빵", "8천원", "정문", "  해산물, 매운  ") is None
assert s6["profiles"]["k7"] == {"nick": "동근", "toss": "dong", "kakao": "", "split": "N빵",
                                "budget": "8천원", "where": "정문", "avoid": "해산물, 매운"}
assert [app.mask(x) for x in ("이동근", "민수", "김", "Dongeun")] == ["이*근", "민*", "김", "D*****n"]
s7 = {"parties": [], "profiles": {"k1": {"nick": "a"}, "k2": {"nick": "b"}}, "counted": {"2026-11-11": ["k1|X", "k2|X"]}}
app.add_party(s7, "q", "k1", "x", "12:00", day="2026-11-11"); app.join_party(s7, "q", "k2")
app.add_party(s7, "r", "k1", "x", "13:00", day="2026-11-11")
assert app.delete_me(s7, "k1") is None and list(s7["profiles"]) == ["k2"] and s7["counted"]["2026-11-11"] == ["k2|X"]
assert [(p["id"], app.host_of(p)) for p in s7["parties"]] == [("q", "k2")]  # 혼자였던 모집은 사라지고 방장은 넘어감
# 로그인 유지 쿠키
tok = app.make_login_token("k42", "동근", now=1000)
assert app.read_login_token(tok, now=1000 + 86400 * 7 - 1) == {"id": "k42", "nick": "동근"}
assert app.read_login_token(tok, now=1000 + 86400 * 7 + 1) is None  # 7일 지나면 만료
body, sig = tok.rsplit(".", 1)
forged = app.base64.urlsafe_b64encode(app.json.dumps({"id": "k1", "nick": "x", "exp": 10**12}).encode()).decode().rstrip("=")
assert app.read_login_token(forged + "." + sig, now=1000) is None  # 남의 회원번호로 바꾸면 서명 불일치
assert all(app.read_login_token(x, now=1000) is None for x in ("", "garbage", "a.b", None, tok + "x"))
assert app.make_login_token("k42", "동근", now=1000) == tok and "." in tok
# 정산 완료 체크
s8 = {"parties": []}
app.add_party(s8, "z", "k1", "x", "12:00", day="2026-11-12"); app.join_party(s8, "z", "k2"); app.join_party(s8, "z", "k3")
assert app.mark_paid(s8, "z", "k2", "k3") == "결제 담당자만 표시할 수 있어요"
assert app.mark_paid(s8, "z", "k1", "k1") == app.mark_paid(s8, "z", "k1", "k9") == "모집에 없는 사람이에요"
assert app.mark_paid(s8, "z", "k1", "k2") is None and app.mark_paid(s8, "z", "k1", "k2") is None and s8["parties"][0]["paid"] == ["k2"]
assert app.mark_paid(s8, "z", "k1", "k2", False) is None and s8["parties"][0]["paid"] == []
# 내 식사 기록 (결정이 집계될 때만)
import datetime as _d2
app.STATE_FILE = Path(tempfile.mkdtemp()) / "state.json"
for day, r in (("2026-11-09", "A"), ("2026-11-10", "B"), ("2026-11-12", "C"), ("2026-11-12", "C")):
    app.add_visit(r, when=_d2.datetime.fromisoformat(day + "T12:00"), uid="k5")
st9 = app.load_state()
assert [m["r"] for m in st9["meals"]["k5"]] == ["A", "B", "C"]  # 같은 날 같은 곳은 한 번
assert app.recent_places(st9, "k5", "2026-11-12") == {"B", "C"} and app.recent_places(st9, "k6", "2026-11-12") == set()
assert app.meal_summary(st9, "k5", "2026-11-12") == (3, ["C", "B", "A"])  # 11/9(월)부터 이번 주
app.delete_me(st9, "k5"); assert "k5" not in st9["meals"]
# 카카오톡 공유 버튼: 글에 </script>가 있어도 스크립트가 끊기지 않음
h = app.kakao_share_html("밥 </script><script>alert(1)</script>", "https://x.app/?party=ab12cd")
assert h.count("</script>") == 2 and r"<\/script>" in h and "integrity=" in h and "Kakao.Share.sendDefault" in h
# 메뉴 수정: 내 것만, 비우기도 됨, 마감돼도 됨
s10 = {"parties": []}
app.add_party(s10, "m", "k1", "x", "12:00", menu="A"); app.join_party(s10, "m", "k2", "B"); app.close_party(s10, "m", "k1")
assert app.set_menu(s10, "m", "k2", "C, D") is None and app.members_of(s10["parties"][0])[1]["menu"] == "C, D"
assert app.set_menu(s10, "m", "k2", "") is None and app.members_of(s10["parties"][0])[1]["menu"] == ""
assert app.set_menu(s10, "m", "k9", "X") == "참여 중인 모집이 아니에요" and app.set_menu(s10, "zz", "k1", "X") == "없어진 모집이에요"
# 참여 요청 → 방장 수락/거절
s11 = {"parties": []}
app.add_party(s11, "j", "k1", "x", "12:00", max_n=3)
assert app.request_join(s11, "j", "k1") == "이미 참여 중이에요"
assert app.request_join(s11, "j", "k2", "철수", "A") is None and app.request_join(s11, "j", "k2", "철수", "B") is None
pj = s11["parties"][0]
assert [x["menu"] for x in pj["pending"]] == ["B"] and len(app.members_of(pj)) == 1  # 수락 전엔 멤버 아님
assert app.answer_join(s11, "j", "k2", "k2", True) == "방장만 수락할 수 있어요"
assert app.answer_join(s11, "j", "k1", "k2", True) is None and [m["name"] for m in app.members_of(pj)] == ["k1", "k2"]
app.request_join(s11, "j", "k3"); assert app.answer_join(s11, "j", "k1", "k3", False) is None
assert pj["pending"] == [] and len(app.members_of(pj)) == 2
# 결제 담당자 넘기기
assert app.payer_of(pj) == "k1" and app.set_payer(s11, "j", "k2", "k2") == "방장만 바꿀 수 있어요"
app.set_pay(s11, "j", "k1", "hostid", "", "메뉴별")
assert app.set_payer(s11, "j", "k1", "k9") == "모집에 없는 사람이에요"
assert app.set_payer(s11, "j", "k1", "k2") is None and app.payer_of(pj) == "k2" and "pay" not in pj  # 예전 송금 정보 지움
assert app.set_pay(s11, "j", "k1", "x", "", "N빵") == "결제 담당자만 정산을 설정할 수 있어요"
assert app.set_pay(s11, "j", "k2", "payer2", "", "N빵") is None and app.mark_paid(s11, "j", "k2", "k1") is None
app.leave_party(s11, "j", "k2"); assert app.payer_of(pj) == "k1"  # 담당자가 나가면 방장에게
# 방장이 식당 바꾸면 메뉴 비움
app.join_party(s11, "j", "k4", "치킨")
assert app.edit_party(s11, "j", "k1", "12:00", "정문", 3, "", place="y") is None
assert pj["place"] == "y" and all(m["menu"] == "" for m in app.members_of(pj))
# 운영자: ADMIN_IDS에 있으면 남의 모집도 삭제
s12 = {"parties": []}
app.add_party(s12, "a1", "k1", "x", "12:00")
_os.environ["ADMIN_IDS"] = " k900 , k901"
assert not app.is_admin("") and not app.is_admin("k1") and app.is_admin("k901")
assert app.delete_party(s12, "a1", "k2") == "방장만 삭제할 수 있어요"
assert app.delete_party(s12, "a1", "k900") is None and s12["parties"] == []
_os.environ.pop("ADMIN_IDS")
# 배달 범위 수집: 45개 넘는 칸은 4등분, 반경 밖·중복은 빼고 가까운 순
_places = [(37.30 + dy * 0.002, 127.03 + dx * 0.002) for dy in range(-20, 21) for dx in range(-20, 21)]  # 41x41 격자


def _fake_fetch(url, headers=None):
    q = dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))
    x1, y1, x2, y2 = map(float, q["rect"].replace("%2C", ",").split(","))
    hit = [(la, ln) for la, ln in _places if x1 <= ln <= x2 and y1 <= la <= y2]
    page = int(q["page"])
    docs = [{"id": f"{la:.3f},{ln:.3f}", "place_name": f"식당{la:.3f}{ln:.3f}", "category_name": "음식점 > 한식 > 국밥",
             "x": str(ln), "y": str(la), "place_url": "u", "road_address_name": ""} for la, ln in hit[(page - 1) * 15: page * 15]]
    return json.dumps({"documents": docs, "meta": {"total_count": len(hit), "is_end": page * 15 >= min(len(hit), 45)}}).encode()


_real_fetch, crawl.fetch = crawl.fetch, _fake_fetch
dl = crawl.delivery_nearby("K", 37.30, 127.03, radius=3000)
crawl.fetch = _real_fetch
inside = [p for p in _places if crawl.distance_m(37.30, 127.03, *p) <= 3000]
assert len(dl) == len(inside) and len({r["name"] for r in dl}) == len(dl)  # 빠짐·중복 없음
assert [r["dist"] for r in dl] == sorted(r["dist"] for r in dl) and dl[0]["category"] == "한식" and dl[0]["sub"] == "국밥"
# 술집·빵집은 배달 점심 후보에서 뺌


def _two(url, headers=None):
    docs = [{"id": str(i), "place_name": n, "category_name": c, "x": "127.03", "y": "37.30", "place_url": "u"}
            for i, (n, c) in enumerate([("호프집", "음식점 > 술집 > 호프,요리주점"), ("빵집", "음식점 > 간식 > 제과,베이커리"),
                                        ("국밥집", "음식점 > 한식 > 국밥")])]
    return json.dumps({"documents": docs, "meta": {"total_count": 3, "is_end": True}}).encode()


crawl.fetch = _two
assert [r["name"] for r in crawl.delivery_nearby("K", 37.30, 127.03)] == ["국밥집"]
crawl.fetch = _real_fetch
# 배달 고르기: 종류·거리로 거르고 가까운 순 유지
_dl = [{"category": "한식", "dist": 300}, {"category": "중식", "dist": 900}, {"category": "한식", "dist": 2500}]
assert app.delivery_list(_dl, "한식", 1000) == [_dl[0]] and app.delivery_list(_dl) == _dl and app.delivery_list(_dl, "양식") == []
assert len(app.DELIVERY) > 1000 and all(r["dist"] <= 3000 for r in app.DELIVERY)
print("ok")
