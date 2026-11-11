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
next(b for b in at.button if b.label == "저장").click()
at.run()
assert not at.exception, at.exception
st_ = json.load(open(os.environ["LUNCH_STATE"], encoding="utf-8"))
assert st_["profiles"]["k42"] == {"nick": "동근", "toss": "dong", "kakao": ""}, st_
md = " ".join(m.value for m in at.markdown)
assert "동근" in md, md[:300]
# 로그아웃 상태: 로그인 안내 카드
at2 = AppTest.from_file("app.py", default_timeout=60); at2.run()
assert not at2.exception and "로그인이 필요해요" in " ".join(m.value for m in at2.markdown)
print("apptest ok")
