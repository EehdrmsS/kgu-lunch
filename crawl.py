"""data.json 갱신 크롤러.
  python crawl.py                      → 경기대 공식 홈페이지에서 교내 식당 운영정보 + 감성코어 주간메뉴(PDF)
  KAKAO_REST_KEY=... python crawl.py   → 위 + 카카오 로컬 API로 정문/후문 반경 500m 음식점 목록
                                         (환경변수 대신 같은 폴더의 .kakao_key 파일에 키만 적어둬도 됨)
"""
import datetime
import html
import io
import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data.json"
KST = datetime.timezone(datetime.timedelta(hours=9))  # 배포 서버(UTC)에서도 '이번 주'를 한국 날짜로
SITE = "https://www.kyonggi.ac.kr"
PAGE = SITE + "/www/selectTnRstrntMenuListU.do?key=7138&sc1=30"
# 카카오 분류(음식점 > X) → 앱 만다라트 8칸
KAKAO_CAT = {"한식": "한식", "분식": "분식", "중식": "중식", "일식": "일식·아시안", "아시아음식": "일식·아시안",
             "양식": "양식", "패스트푸드": "패스트푸드", "치킨": "패스트푸드"}


def fetch(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def text(s):
    s = re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", " / ", s))
    return re.sub(r"\s+", " ", html.unescape(s)).strip(" /")


def parse_cafeterias(page):
    """<h3>식당명</h3> 아래 '위치/운영시간/메뉴' 항목 → {식당명: {항목: 값}}"""
    out = {}
    parts = re.split(r"<h3>(.*?)</h3>", page)
    for name, body in zip(parts[1::2], parts[2::2]):
        pairs = re.findall(r'class="title">(.*?)</span>\s*<span class="text">(.*?)</span>', body, re.S)
        if pairs:
            out[text(name)] = {text(k): text(v) for k, v in pairs}  # 뒤쪽(상세) 값이 앞쪽 요약을 덮어씀
    return out


def week_pdfs(page):
    """달력에 걸린 주간메뉴 PDF 목록 [(pdf경로, 시작일, 종료일)]"""
    return sorted(set(re.findall(r'data-pdf="([^"]+\.pdf)" data-start="([\d-]+)" data-end="([\d-]+)"', page)),
                  key=lambda t: t[1])


def parse_week_pdf(pdf_bytes, start):
    """표 형태 주간메뉴 PDF → {날짜: [메뉴...]}.  글자 좌표로 요일 칸을 나눔."""
    import pypdf
    chars = []
    pypdf.PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text(
        visitor_text=lambda t, cm, tm, *_: t.strip() and chars.append((cm[4] + tm[4], cm[5] + tm[5], t.strip())))
    # 날짜 헤더 줄 = '/'가 가장 많은 줄 (11/09일(월) ...)
    ys = [y for x, y, c in chars if c == "/"]
    hy = max(set(ys), key=ys.count)
    tokens = []  # 헤더 줄 글자를 간격 100 이상에서 끊어 칸 단위로 묶음
    for x, c in sorted((x, c) for x, y, c in chars if abs(y - hy) < 5):
        if tokens and x - tokens[-1][-1][0] < 100:
            tokens[-1].append((x, c))
        else:
            tokens.append([(x, c)])
    cols = [sum(x for x, _ in t) / len(t) for t in tokens if any(c == "/" for _, c in t)]  # '일자' 칸 제외
    half = (cols[1] - cols[0]) / 2
    days = {i: [] for i in range(len(cols))}
    for x, y, c in chars:
        if y > hy + 30 and cols[0] - half <= x <= cols[-1] + half:
            days[min(range(len(cols)), key=lambda i: abs(cols[i] - x))].append((y, x, c))
    d0 = datetime.date.fromisoformat(start)
    out = {}
    for i, cs in days.items():
        lines = []  # [(y, 글자들)]
        for y, x, c in sorted(cs):
            if lines and abs(lines[-1][0] - y) < 10:
                lines[-1][1].append((x, c))
            else:
                lines.append((y, [(x, c)]))
        items, prev_y = [], None
        for y, cs2 in lines:
            s = "".join(c for _, c in sorted(cs2))
            if items and (s.startswith("(") or y - prev_y < 80):  # 줄바꿈된 메뉴명 이어붙이기
                items[-1] += s
            else:
                items.append(s)
            prev_y = y
        out[(d0 + datetime.timedelta(days=i)).isoformat()] = items
    return out


def kakao_nearby(key, lat, lng, radius=500):
    docs = []
    for page in (1, 2, 3):  # 카카오 API는 최대 45개
        q = urllib.parse.urlencode({"category_group_code": "FD6", "x": lng, "y": lat,
                                    "radius": radius, "sort": "distance", "page": page})
        r = json.loads(fetch("https://dapi.kakao.com/v2/local/search/category.json?" + q,
                             {"Authorization": "KakaoAK " + key}))
        docs += r["documents"]
        if r["meta"]["is_end"]:
            break
    return docs


CAMPUS_ADDR = "광교산로 154-42"  # 교내 가게는 data.json 교내 식당으로 직접 관리


def kakao_to_restaurant(d, gate, prices=None):
    cat = (d["category_name"].split(" > ") + ["", ""])[1]
    r = {"name": d["place_name"], "where": "주변", "gate": gate, "category": KAKAO_CAT.get(cat, "기타"),
         "lat": float(d["y"]), "lng": float(d["x"]), "address": d.get("road_address_name", ""),
         "url": d["place_url"], "source": "kakao",
         "menus": [{"name": f"{cat or '메뉴'} (메뉴·가격은 카카오맵에서 확인)", "price": None}]}
    p = (prices or {}).get(d["place_name"], {})  # prices.json에 조사해둔 메뉴·카테고리 보정이 있으면 붙임
    if p.get("menus"):
        r["menus"] = [{"name": n, "price": v} for n, v in p["menus"]]
        r["price_source"] = p["source"]
    if p.get("category"):
        r["category"] = p["category"]
    return r


DORM_PAGE = "https://dorm.kyonggi.ac.kr:446/Khostel/mall_main.php?viewform=B0001_foodboard_list&board_no=1"


def parse_dorm(page):
    """경기드림타워(기숙사) 금주의 식단 표 → {날짜: {"아침": [...], "점심": [...], "저녁": [...]}} (빈 날은 제외)"""
    out = {}
    for date, cells in re.findall(r"(\d{4}-\d{2}-\d{2}) \(.\)</a>\s*</th>(.*?)</tr>", page, re.S):
        tds = [[text(x) for x in re.split(r"<br\s*/?>", td) if text(x)]
               for td in re.findall(r"<td[^>]*>(.*?)</td>", cells, re.S)]
        if any(tds):
            out[date] = dict(zip(["아침", "점심", "저녁"], tds))
    return out


def main():
    data = json.loads(DATA_FILE.read_text("utf-8"))
    page = fetch(PAGE).decode("utf-8")

    info = parse_cafeterias(page)
    for r in data["restaurants"]:
        if r.get("official") in info:
            r["info"] = info[r["official"]]
    print("교내 식당 정보:", ", ".join(info))

    pdfs = week_pdfs(page)
    if pdfs:
        today = datetime.datetime.now(KST).date().isoformat()
        pdf, start, end = next((p for p in pdfs if p[1] <= today <= p[2]), pdfs[-1])
        days = parse_week_pdf(fetch(SITE + pdf), start)
        data["weekly"] = {"restaurant": "감성코어", "start": start, "end": end,
                          "pdf": SITE + pdf, "days": days, "fetched": today}
        print(f"감성코어 주간메뉴 {start}~{end}: {len(days)}일")

    dorm = parse_dorm(fetch(DORM_PAGE).decode("cp949", "replace"))
    if dorm:
        data["dorm_weekly"] = {"url": DORM_PAGE, "days": dorm, "fetched": datetime.datetime.now(KST).date().isoformat()}
        print(f"기숙사 식단: {len(dorm)}일")

    key_file = DATA_FILE.parent / ".kakao_key"  # 키 파일은 공유/업로드 금지
    key = os.environ.get("KAKAO_REST_KEY") or (key_file.read_text("utf-8").strip() if key_file.exists() else "")
    if key:
        manual = {r["name"] for r in data["restaurants"] if r.get("source") != "kakao"}
        price_file = DATA_FILE.parent / "prices.json"
        prices = json.loads(price_file.read_text("utf-8"))["restaurants"] if price_file.exists() else {}
        found = {}
        for gate in data["campus"]["gates"]:
            for d in kakao_nearby(key, gate["lat"], gate["lng"]):
                if d["place_name"] not in manual and CAMPUS_ADDR not in d.get("road_address_name", ""):
                    found.setdefault(d["id"], kakao_to_restaurant(d, gate["name"], prices))
        data["restaurants"] = [r for r in data["restaurants"] if r.get("source") != "kakao"] + list(found.values())
        print("카카오 주변 음식점:", len(found))
    else:
        print("카카오 키 없음(KAKAO_REST_KEY 또는 .kakao_key) → 주변 음식점 자동 수집 건너뜀")

    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
