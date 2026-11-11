"""store.py 검사: Supabase 경로를 가짜 서버로 (네트워크 없이). 실행: python test_store.py"""
import json
import os
import tempfile
import threading
from pathlib import Path

import store

db = {}  # 가짜 app_state 테이블
calls = []


def fake_req(method, url, key, body=None, prefer=None):
    calls.append(method)
    if method == "GET":
        return [{"data": db[1]}] if 1 in db else []
    assert prefer == "resolution=merge-duplicates" and body["id"] == 1
    db[1] = json.loads(json.dumps(body["data"]))
    return None


store._req = fake_req
os.environ.update(SUPABASE_URL="https://x.supabase.co/", SUPABASE_KEY="sb_secret_test")
seed = Path(tempfile.mkdtemp()) / "state.json"
seed.write_text(json.dumps({"visits": {"A": 3}, "parties": []}), "utf-8")

# 처음 연결: 서버에 남은 state.json으로 DB를 채움
assert store.load(seed)["visits"] == {"A": 3} and db[1]["visits"] == {"A": 3}
# 고치면 DB에 저장되고, 다음 읽기는 DB를 다시 부르지 않음(캐시)
store.update(seed, lambda s: s["visits"].__setitem__("B", 1))
n = calls.count("GET")
assert store.load(seed)["visits"] == {"A": 3, "B": 1} == db[1]["visits"] and calls.count("GET") == n
# 받은 사본을 고쳐도 캐시는 안 바뀜
store.load(seed)["visits"]["C"] = 9
assert "C" not in store.load(seed)["visits"]


# 저장 실패하면 캐시도 그대로 (DB와 어긋나지 않게)
def broken(*a, **k):
    raise OSError("down")


store._req = broken
try:
    store.update(seed, lambda s: s["visits"].__setitem__("D", 1))
    raise AssertionError("저장 실패가 숨겨지면 안 됨")
except OSError:
    pass
assert "D" not in store.load(seed)["visits"]
store._req = fake_req

# 동시에 50번 눌러도 하나도 안 빠짐 (잠금)
threads = [threading.Thread(target=store.update, args=(seed, lambda s: s["visits"].__setitem__("B", s["visits"]["B"] + 1)))
           for _ in range(50)]
[t.start() for t in threads]
[t.join() for t in threads]
assert db[1]["visits"]["B"] == 51

# 처음 읽기가 실패하면 빈 상태로 덮어쓰지 않고 에러
store._cache, store._req = None, broken
try:
    store.load(seed)
    raise AssertionError("읽기 실패가 숨겨지면 안 됨")
except OSError:
    pass
assert db[1]["visits"]["A"] == 3
print("store ok")
