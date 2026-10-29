#!/usr/bin/env python3
"""
앱 [구장추가] 처리 워커 (v2)

흐름
  1) searching : 이름만 들어온 요청 → Claude 웹 검색으로 공식 홈페이지 찾기 → status=confirm (앱에서 사용자 확인)
  2) queued    : 주소 확정된 요청 → crawl.py 실행 → /api/courses 업로드(+R2 이미지) → done

실행(PC 또는 작은 VPS):
  pip install playwright anthropic beautifulsoup4 requests && python -m playwright install chromium
  export APP_URL=https://golf-yardage.pages.dev
  export WORKER_KEY=...            # 배포 때 나온 값
  export ANTHROPIC_API_KEY=sk-ant-...
  python worker.py                 # 15초마다 큐 확인
  python worker.py --once
  python worker.py --upload seowon-hills    # out/<id>/course.json 또는 ../courses/<id>.json 업로드만
  python worker.py --find "이글몬트CC"       # 홈페이지 검색만 테스트
"""
import os, sys, json, time, re, subprocess, argparse, mimetypes
from pathlib import Path
from urllib.parse import urlparse
import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
APP = os.environ.get("APP_URL", "http://localhost:8788").rstrip("/")
H = {"X-Worker-Key": os.environ.get("WORKER_KEY", "dev-key-change-me")}
MODEL = os.environ.get("CRAWL_MODEL", "claude-sonnet-5-5")
# 예약·중개·리뷰 사이트는 공식 홈페이지가 아님
NOT_OFFICIAL = re.compile(r"(kimcaddie|dbegl|teescanner|tigerbooking|xgolf|golfzon\.com/booking|smartscore|naver\.com|daum\.net|kakao|"
                          r"tistory|blog|cafe|instagram|facebook|youtube|wikipedia|namu\.wiki|ktriptips|golfpass|teetimegolfpass)", re.I)


def api(method, path, **kw):
    r = requests.request(method, APP + "/api" + path, headers={**H, **kw.pop("headers", {})}, timeout=120, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} → {r.status_code} {r.text[:200]}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text


# ---------- 1. 이름 → 공식 홈페이지 ----------
def reachable(url):
    """실제로 열리는 주소인지 (없어진 도메인·접속 불가 걸러내기)"""
    try:
        r = requests.get(url, timeout=12, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code < 500, r.url
    except Exception:
        return False, url


def find_homepage(club_name: str):
    """웹 검색으로 공식 홈페이지를 찾고, 접속되는지 확인한다. 안 되면 그 주소를 빼고 한 번 더 찾는다."""
    dead = []
    for _ in range(2):
        url, note = _search_homepage(club_name, dead)
        if not url:
            return None, note
        ok, final = reachable(url)
        if ok:
            return final, note
        dead.append(url)
    return None, f"찾은 주소가 접속되지 않습니다({', '.join(dead)}). 주소를 직접 입력해 주세요"


def _search_homepage(club_name: str, dead=()):
    import anthropic
    client = anthropic.Anthropic()
    avoid = f"\n- 다음 주소는 접속이 안 되는 옛 주소이니 제외하라: {', '.join(dead)}" if dead else ""
    prompt = (
        f"대한민국 골프장 '{club_name}'의 **공식 홈페이지** 주소를 웹 검색으로 찾아라.\n"
        "- 예약·중개·리뷰·블로그·지도·위키 사이트는 공식 홈페이지가 아니다.\n"
        "- 운영사 통합 사이트(예: 골프존카운티)라면 해당 골프장 전용 페이지 주소를 택한다.\n"
        "- 같은 이름의 해외 골프장과 혼동하지 마라. 주소(시·군)로 국내 골프장인지 확인하라.\n"
        "- 인수·리뉴얼로 도메인이 바뀌었을 수 있으니 최신 정보를 우선하라.\n"
        "- 가능하면 '코스소개/코스안내' 페이지 주소를, 못 찾으면 메인 주소를 답한다." + avoid + "\n"
        '마지막 줄에 JSON만: {"url": "https://..." 또는 null, "location": "시·군", "confidence": 0~1, "reason": "한 줄"}'
    )
    r = client.messages.create(
        model=MODEL, max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4, "user_location": {"type": "approximate", "country": "KR"}}],
        messages=[{"role": "user", "content": prompt}],
    )
    seen = set()
    for b in r.content:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            for x in b.content:
                seen.add(urlparse(x.url).netloc.lower().replace("www.", ""))
    text = "".join(b.text for b in r.content if b.type == "text")
    m = re.findall(r"\{[^{}]*\"url\"[^{}]*\}", text, re.S)
    if not m:
        return None, "검색 결과를 해석하지 못했습니다"
    j = json.loads(m[-1])
    url = j.get("url")
    if not url or not url.startswith("http"):
        return None, j.get("reason") or "공식 홈페이지를 찾지 못했습니다"
    host = urlparse(url).netloc.lower().replace("www.", "")
    if NOT_OFFICIAL.search(host):
        return None, f"공식 홈페이지가 아닌 사이트만 찾았습니다({host}). 주소를 직접 입력해 주세요"
    note = f"{j.get('location') or ''} · 신뢰도 {j.get('confidence', '?')} · {j.get('reason') or ''}".strip(" ·")
    if host not in seen:
        note += " · ⚠ 검색 결과에 직접 나온 주소가 아님, 꼭 확인"
    return url, note


def run_search(req):
    rid = req["id"]
    try:
        url, note = find_homepage(req["club_name"])
    except Exception as e:
        url, note = None, f"검색 실패: {str(e)[:200]}"
    api("PATCH", f"/crawl-requests/{rid}", json={"status": "confirm", "candidate_url": url, "message": note})
    print(f"  검색: {req['club_name']} → {url}  ({note})")


# ---------- 2. 수집·업로드 ----------
sys.path.insert(0, str(HERE))
from crawl import course_id_for as course_id_of   # 크롤러와 같은 구장 id 규칙(통합 사이트는 구장 번호까지)


def upload_course(course_id: str, course_dir: Path = None):
    f = (course_dir or OUT / course_id) / "course.json"
    if not f.exists():
        f = HERE.parent / "courses" / f"{course_id}.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    data["course_id"] = course_id
    base = f.parent
    for c in data["courses"]:
        for h in c["holes"]:
            local = h.get("image")
            if local and (base / local).exists():
                key = f"{course_id}/{Path(local).name}"
                mime = mimetypes.guess_type(local)[0] or "image/jpeg"
                api("PUT", f"/images/{key}", data=(base / local).read_bytes(), headers={"content-type": mime})
                h["image_url"] = f"/api/images/{key}"
    r = api("POST", "/courses", json=data)
    print(f"  업로드: {course_id} {r.get('holes')}홀")


def run_crawl(req):
    rid, url = req["id"], req["url"]
    course_id = course_id_of(url)
    api("PATCH", f"/crawl-requests/{rid}", json={"status": "running", "course_id": course_id})
    try:
        p = subprocess.run([sys.executable, str(HERE / "crawl.py"), "--name", req["club_name"], "--url", url, "--id", course_id],
                           capture_output=True, text=True, timeout=1800)
        print(p.stdout[-1500:])
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout)[-800:] or "crawl.py 실패")
        upload_course(course_id)
        review = json.loads((OUT / course_id / "review.json").read_text(encoding="utf-8"))
        msg = ("완료 · 확인 필요: " + "; ".join(review[:4])) if review else "완료"
        api("PATCH", f"/crawl-requests/{rid}", json={"status": "done", "message": msg[:500]})
        print("  완료:", req["club_name"], msg)
    except Exception as e:
        api("PATCH", f"/crawl-requests/{rid}", json={"status": "failed", "course_id": course_id, "message": str(e)[:500]})
        print("  실패:", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--drain", action="store_true", help="대기열이 빌 때까지 처리하고 종료 (GitHub Actions용)")
    ap.add_argument("--upload")
    ap.add_argument("--find")
    a = ap.parse_args()
    if a.upload:
        upload_course(a.upload); return
    if a.find:
        print(find_homepage(a.find)); return
    print("워커 시작 →", APP, "(drain)" if a.drain else "")
    started = time.time()
    while True:
        try:
            api("POST", "/worker/ping")
        except Exception:
            pass
        did = False
        for status, fn in (("searching", run_search), ("queued", run_crawl)):
            try:
                q = api("GET", f"/crawl-requests?status={status}")
            except Exception as e:
                print("API 오류:", e); q = []
            for req in q:
                print(f"[{status}] {req['club_name']} {req.get('url') or ''}")
                fn(req); did = True
        if (a.once or a.drain) and not did:
            return
        if a.drain and time.time() - started > 35 * 60:    # Actions 제한시간 전에 정리
            print("시간 한도 — 나머지는 다음 실행에서"); return
        time.sleep(3 if did else 15)


if __name__ == "__main__":
    main()
