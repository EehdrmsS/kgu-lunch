"""앱 상태(모집·프로필·이용객 수) 저장소.

- 배포 서버: Supabase(무료 Postgres)의 app_state 테이블 한 줄(id=1)에 JSON으로 저장 → 서버가 재시작·잠들어도 남음.
  SUPABASE_URL, SUPABASE_KEY(서버 전용 secret 키) 환경변수(Streamlit Secrets)가 있으면 이 방식.
- 내 컴퓨터: 지금처럼 state.json 파일.

Streamlit은 화면을 누를 때마다 app.py를 처음부터 다시 실행하지만, import한 이 모듈은 한 번만 실행된다.
그래서 잠금(동시에 눌러도 데이터가 꼬이지 않게)과 Supabase 내용 캐시를 여기 둔다.
"""
import copy
import json
import os
import threading
import urllib.request

_lock = threading.Lock()
_cache = None  # Supabase 쓸 때만: 서버 메모리의 최신본 (이 프로세스가 유일한 작성자라 매번 DB를 읽지 않음)
EMPTY = {"visits": {}, "parties": []}


def _supabase():
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_KEY", "")
    return (url, key) if url and key else None


def _req(method, url, key, body=None, prefer=None):
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):  # 예전 형식(JWT) service_role 키는 Authorization도 필요
        headers["Authorization"] = "Bearer " + key
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=10) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _db_read(sb, seed_file):
    url, key = sb
    rows = _req("GET", f"{url}/rest/v1/app_state?id=eq.1&select=data", key)
    if rows:
        return rows[0]["data"]
    # 처음 연결: 서버에 남아 있던 state.json이 있으면 그걸로 시작 (없으면 빈 상태)
    seed = json.loads(seed_file.read_text("utf-8")) if seed_file.exists() else copy.deepcopy(EMPTY)
    _db_write(sb, seed)
    return seed


def _db_write(sb, s):
    url, key = sb
    _req("POST", f"{url}/rest/v1/app_state", key, {"id": 1, "data": s}, prefer="resolution=merge-duplicates")


def load(file):
    """읽기 실패는 그대로 에러로 올림: 빈 상태로 계속하다 나중에 저장하면 DB를 덮어써 데이터가 날아가기 때문."""
    global _cache
    sb = _supabase()
    if not sb:
        return json.loads(file.read_text("utf-8")) if file.exists() else copy.deepcopy(EMPTY)
    with _lock:
        if _cache is None:
            _cache = _db_read(sb, file)
        return copy.deepcopy(_cache)


def update(file, fn):
    """잠근 채로 최신본을 읽고 fn(s)로 고친 뒤 저장. 저장에 실패하면 캐시도 그대로 둠."""
    global _cache
    sb = _supabase()
    with _lock:
        if not sb:
            s = json.loads(file.read_text("utf-8")) if file.exists() else copy.deepcopy(EMPTY)
            fn(s)
            file.write_text(json.dumps(s, ensure_ascii=False, indent=1), "utf-8")
            return s
        s = copy.deepcopy(_cache if _cache is not None else _db_read(sb, file))
        fn(s)
        _db_write(sb, s)
        _cache = s
        return copy.deepcopy(s)
