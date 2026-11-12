"""화면 흐름 검사 (Streamlit AppTest): 로그인한 척 세션을 넣고 홈 프로필 저장까지. 실행: python test_ui.py"""
import json
import os
import sys
import tempfile

os.environ["LUNCH_STATE"] = os.path.join(tempfile.mkdtemp(), "state.json")
sys.path.insert(0, os.getcwd())
from streamlit.testing.v1 import AppTest
at = AppTest.from_file("app.py", default_timeout=60)
at.session_state["user"] = {"id": "k42", "nick": "카톡닉"}
at.run()
assert not at.exception, at.exception
md = " ".join(m.value for m in at.markdown)
assert "카톡닉" in md and "오늘 내 모집 0건" in md, md[:300]
f = next(x for x in at.text_input if x.label == "표시 이름")
f.input("동근")
next(x for x in at.text_input if x.label == "토스 아이디").input("dong")
next(x for x in at.selectbox if x.label == "기본 예산").select("8천원")
next(x for x in at.selectbox if x.label == "자주 가는 곳").select("정문")
next(x for x in at.radio if x.label == "기본 정산 방식").set_value("N빵")
next(x for x in at.text_input if x.label == "못 먹는 재료").input("해산물")
next(b for b in at.button if b.label == "저장").click()
at.run()
assert not at.exception, at.exception
st_ = json.load(open(os.environ["LUNCH_STATE"], encoding="utf-8"))
assert st_["profiles"]["k42"] == {"nick": "동근", "toss": "dong", "kakao": "", "split": "N빵",
                                 "budget": "8천원", "where": "정문", "avoid": "해산물"}, st_
# 저장한 취향이 추천·고르기 필터 기본값으로
assert at.session_state["rec_budget"] == "8천원" and at.session_state["pk_where"] == "정문"
assert at.session_state["rec_words"] == "해산물"
assert "토스 연결됨" in " ".join(m.value for m in at.markdown)
md = " ".join(m.value for m in at.markdown)
assert "동근" in md, md[:300]
# 로그아웃 상태: 로그인 안내 카드
at2 = AppTest.from_file("app.py", default_timeout=60); at2.run()
assert not at2.exception and "로그인이 필요해요" in " ".join(m.value for m in at2.markdown)
# 고르기에서 결정 → 홈 프로필에 식사 기록, 고르기에 '최근 간 곳 빼기'
at.radio(key="nav").set_value("고르기"); at.run()
first = next(b for b in at.button if b.key and b.key.startswith("pk|"))
place = first.key[3:]
first.click(); at.run()
next(b for b in at.button if b.key == f"pk_ok|{place}").click(); at.run()
assert not at.exception, at.exception
assert any(x.label.startswith("최근 3일 간 곳 빼기") for x in at.toggle)
at.radio(key="nav").set_value("홈"); at.run()
assert "이번 주 1번 결정" in " ".join(m.value for m in at.markdown)

# 같이 먹기: 내 모집에서 메뉴 수정 (메뉴판에서 고르기)
import app as _app
_app.STATE_FILE = __import__("pathlib").Path(os.environ["LUNCH_STATE"])
_app.update_state(lambda s: _app.add_party(s, "t1", "k42", "버거&타코", "23:59", menu="", nick="동근"))
at.radio(key="nav").set_value("같이 먹기"); at.run()
assert not at.exception, at.exception
pick = at.multiselect(key="mm_t1")
pick.select(pick.options[0]); at.run()
at.button(key="ms_t1").click(); at.run()
assert not at.exception, at.exception
saved = json.load(open(os.environ["LUNCH_STATE"], encoding="utf-8"))["parties"][0]["members"][0]["menu"]
assert saved and saved in pick.options[0], (saved, pick.options[0])
# 초대 링크 없이 온 사람의 참여 요청 → 방장 수락
_app.update_state(lambda s: _app.request_join(s, "t1", "k77", "민수", ""))
at.run()
at.button(key="ok_t1_k77").click(); at.run()
assert not at.exception, at.exception
party = json.load(open(os.environ["LUNCH_STATE"], encoding="utf-8"))["parties"][0]
assert [m["name"] for m in party["members"]] == ["k42", "k77"] and party["pending"] == []
# 방장: 결제 담당자 넘기기
sel = at.selectbox(key="payer_t1")
sel.select(next(o for o in sel.options if o.startswith("민수"))); at.run()
assert not at.exception, at.exception
assert json.load(open(os.environ["LUNCH_STATE"], encoding="utf-8"))["parties"][0]["payer"] == "k77"

at.radio(key="nav").set_value("홈"); at.run()
# 내 정보 삭제: 체크해야 버튼이 눌리고, 누르면 프로필 삭제 + 로그아웃
next(x for x in at.checkbox if x.key == "del_me_sure").check()
at.run()
next(b for b in at.button if b.label == "내 정보 삭제하고 로그아웃").click()
at.run()
assert not at.exception, at.exception
assert "k42" not in json.load(open(os.environ["LUNCH_STATE"], encoding="utf-8")).get("profiles", {})
assert "user" not in at.session_state and "로그인이 필요해요" in " ".join(m.value for m in at.markdown)

# 고르기: 20곳씩 보여주고 '더 보기'
at2.radio(key="nav").set_value("고르기")
at2.run()
assert not at2.exception, at2.exception
more = [b for b in at2.button if b.label.startswith("더 보기")]
assert more and sum(bool(b.key) and b.key.startswith("pk|") for b in at2.button) == 20
more[0].click()
at2.run()
assert sum(bool(b.key) and b.key.startswith("pk|") for b in at2.button) > 20
# 초대 링크로 비회원 입장: 로그인/비회원 선택 → 이름만 정하고 → 메뉴 골라 참여
_app.update_state(lambda s: _app.add_party(s, "g1", "k500", "버거&타코", "23:59", nick="방장"))
at3 = AppTest.from_file("app.py", default_timeout=60)
at3.query_params["party"] = "g1"
at3.run()
assert not at3.exception, at3.exception
md = " ".join(m.value for m in at3.markdown)
assert "님이 초대했어요" in md and "카카오로 로그인" in md and "비회원으로 참여" in md
next(x for x in at3.text_input if x.placeholder == "친구들이 알아볼 이름").input("손님")
next(b for b in at3.button if b.label == "이 이름으로 들어가기").click(); at3.run()
assert not at3.exception, at3.exception
assert at3.session_state["user"]["guest"] and at3.session_state["user"]["nick"] == "손님"
next(b for b in at3.button if b.label == "손님(으)로 참여하기").click(); at3.run()
party = next(p for p in json.load(open(os.environ["LUNCH_STATE"], encoding="utf-8"))["parties"] if p["id"] == "g1")
assert [m["nick"] for m in party["members"]] == ["방장", "손님"] and party["members"][1]["name"].startswith("g")
print("apptest ok")
