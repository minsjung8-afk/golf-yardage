#!/usr/bin/env python3
"""
골프장 홈페이지 → 홀별 코스맵·파·티별 전장(m) 자동 수집기

사용:
  pip install playwright anthropic beautifulsoup4
  playwright install chromium
  export ANTHROPIC_API_KEY=sk-ant-...
  python crawl.py --course seowon-hills            # seeds.json에 등록된 골프장
  python crawl.py --name "김포씨사이드" --url https://...   # 즉석 등록
  python crawl.py --course seowon-hills --no-llm   # 수집만(LLM 없이 raw_pages.json 확인용)

출력:
  out/<course-id>/course.json   앱이 읽는 최종 데이터 (단위 m)
  out/<course-id>/images/*.jpg  홀별 코스맵 원본
  out/<course-id>/review.json   확정 못한 홀(검토 큐) — 정상 사이트면 비어 있음

동작:
  1) 시드 URL 접속 → 메뉴에서 코스소개/코스안내/course/hole 링크 수집(같은 도메인)
  2) 각 페이지: 텍스트, <table>→markdown, 이미지(src/alt/크기/주변텍스트)
     JS 탭 전환형 페이지는 'HOLE n / n홀 / ○○코스' 버튼을 전부 클릭하며 DOM을 펼침
  3) Claude에 텍스트+표+이미지 목록을 넘겨 홀 단위 JSON으로 구조화
     이미지가 홀과 매칭 안 되면 비전으로 이미지 자체를 분류해 채움
  4) 야드→m 환산, 누락·이상치는 review.json
"""
import argparse, json, os, re, sys, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

MODEL = os.environ.get("CRAWL_MODEL", "claude-sonnet-5-5")
YD = 0.9144
COURSE_LINK_RE = re.compile(r"(코스|course|hole|홀|야디지|yardage|제원|layout|^\s*\d{1,2}\s*(h|홀|번\s*홀)\s*$)", re.I)
SKIP_RE = re.compile(r"(login|member|reserv|예약|board|mypage|policy|logout|join|notice|event|gallery|galleryview|photo|갤러리|\.pdf$|\.hwp$)", re.I)
MIN_IMG = 250
HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
SEEDS = HERE / "seeds.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ---------- 1. 페이지 수집 ----------
def host(u):
    return urlparse(u).netloc.split(":")[0].lower().replace("www.", "")


# 통합 사이트(골프존카운티 등)는 여러 골프장이 한 도메인을 쓴다.
# 주소의 구장 식별 파라미터(golfclubSeq 등)를 '범위'로 잡아 그 구장 페이지만 따라가고, id도 그걸로 만든다.
SCOPE_KEY_RE = re.compile(r"(club|golf|cc|site|store|biz).*(seq|id|code|cd|no)$|^(gcd|ccd|clubcd)$", re.I)


def scope_params(url):
    from urllib.parse import parse_qs
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items() if SCOPE_KEY_RE.search(k)}


def in_scope(url, scope):
    if not scope:
        return True
    p = scope_params(url)
    return all(p.get(k) == v for k, v in scope.items())


def course_id_for(url):
    """도메인 + (구장 파라미터 | 첫 경로) → 'golfzoncounty-com-64', 'seowongolf-co-kr-hills'"""
    sc = scope_params(url)
    parts = [re.sub(r"^(m|mobile)\.", "", host(url))]          # m.golfzoncounty.com = golfzoncounty.com (중복 방지)
    if sc:
        parts += [v for _, v in sorted(sc.items())]
    else:
        segs = [s for s in urlparse(url).path.split("/") if s]
        if len(segs) > 1 and not re.search(r"\.(html?|php|aspx?|jsp)$", segs[0], re.I):
            parts.append(segs[0])
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")


def harvest_page(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(0.8)

    # 탭/버튼형 홀 전환 → 전부 클릭해서 숨은 DOM/이미지 로드
    #   단, 다른 페이지로 이동하는 진짜 링크(a href="a2.html")는 누르지 않는다 — 누르면 페이지가 바뀌어
    #   모든 홀이 같은 내용으로 수집된다(더크로스비에서 1번 홀만 남던 원인). 그런 링크는 아래에서 따로 따라간다.
    clicked = 0
    for el in page.query_selector_all("a, button, li, span, div[onclick]")[:600]:
        try:
            t = (el.inner_text() or "").strip()
        except Exception:
            continue
        if re.fullmatch(r"(HOLE\s*\d{1,2}|\d{1,2}\s*번?\s*홀|\d{1,2}H|[A-Za-z가-힣]{2,10}\s*(코스|COURSE))", t, re.I):
            try:
                # 자신이 링크이거나 링크 안에 있거나, 안에 링크를 품은 칸(<li><a href="a1.html">1H</a></li>)이면 누르지 않는다
                hrefs = el.evaluate("""e => { const out = []; const up = e.closest('a'); if (up) out.push(up.getAttribute('href') || '');
                                         e.querySelectorAll('a[href]').forEach(a => out.push(a.getAttribute('href') || '')); return out }""")
                if any(h and not re.match(r"^(#|javascript:|void)", h.strip(), re.I) for h in hrefs):
                    continue
                el.click(timeout=1200)
                clicked += 1
                time.sleep(0.25)
            except Exception:
                pass
        if clicked >= 60:
            break
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    # 안전장치: 클릭 때문에 다른 페이지로 넘어갔으면 원래 페이지를 다시 연다 (주소가 다르면 내용도 다르다)
    def norm(u):
        p = urlparse(u)
        return (p.netloc.replace("www.", ""), p.path.rstrip("/"), p.query)
    if clicked and norm(page.url) != norm(url):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    for s in soup(["script", "style", "noscript", "iframe"]):
        s.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()

    tables = []
    for t in soup.find_all("table"):
        rows = []
        for tr in t.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        if len(rows) >= 2:
            tables.append("\n".join(rows))

    imgs = []
    for im in soup.find_all("img"):
        src = im.get("src") or im.get("data-src") or im.get("data-original") or ""
        if not src or src.startswith("data:"):
            continue
        ctx = im.parent.get_text(" ", strip=True)[:200] if im.parent else ""
        imgs.append({"url": urljoin(url, src), "alt": im.get("alt", ""), "ctx": ctx})
    for m in re.finditer(r"url\(['\"]?([^'\")]+\.(?:jpg|jpeg|png|webp))", html, re.I):
        imgs.append({"url": urljoin(url, m.group(1)), "alt": "", "ctx": "css-background"})

    sizes = page.evaluate("() => Array.from(document.images).map(i => [i.currentSrc||i.src, i.naturalWidth, i.naturalHeight])")
    size_map = {s[0]: (s[1], s[2]) for s in sizes}
    keep, seen = [], set()
    for im in imgs:
        if im["url"] in seen:
            continue
        seen.add(im["url"])
        w, h = size_map.get(im["url"], (MIN_IMG, MIN_IMG))
        if max(w, h) >= MIN_IMG and min(w, h) >= 120 and not re.search(r"(logo|icon|banner|bn_|btn|weather|partner|special|visual|main_|tile|kakaocdn|daumcdn|staticmap|maps\.)", im["url"], re.I):
            im["w"], im["h"] = w, h
            keep.append(im)

    links, other = [], []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        label = a.get_text(" ", strip=True)
        if href.startswith("http") and host(href) == host(url) and not SKIP_RE.search(href):
            if COURSE_LINK_RE.search(label) or COURSE_LINK_RE.search(href):
                links.append(href)
            elif href.rstrip("/") != url.rstrip("/"):
                other.append(href)              # 입구 페이지 대비(예: 베르힐 영종 'VISIT' → index.asp)
    # onclick·data 속성·스크립트 안에만 있는 이동 주소 (JS로 화면 전환하는 사이트)
    for m in re.finditer(r"['\"]((?:https?://[^'\"\s]+)?/[^'\"\s<>]*(?:course|hole|코스)[^'\"\s<>]*)['\"]", html, re.I):
        href = urljoin(url, m.group(1).replace("&amp;", "&")).split("#")[0]
        if host(href) == host(url) and not SKIP_RE.search(href) and not re.search(r"\.(js|css|png|jpe?g|gif|svg|webp)(\?|$)", href, re.I):
            links.append(href)

    # iframe/frame 안의 페이지 (예: 더크로스비 — 코스공략이 /Course/iframe/a1.html 에 들어 있음)
    #   위에서 BeautifulSoup가 iframe 태그를 지우므로, 렌더된 DOM에서 직접 주소를 받는다. 코스 관련 여부와 무관하게 따라간다.
    try:
        for src in page.evaluate("() => [...document.querySelectorAll('iframe,frame')].map(f => f.src).filter(Boolean)"):
            href = urljoin(url, src).split("#")[0]
            if href.startswith("http") and host(href) == host(url) and not SKIP_RE.search(href) \
                    and not re.search(r"(youtube|map|kakao|naver|google|facebook|instagram|player|\.pdf)", href, re.I):
                links.append(href)
    except Exception:
        pass

    return {"url": url, "text": text[:20000], "tables": tables, "images": keep, "links": sorted(set(links)),
            "other_links": list(dict.fromkeys(other))[:10]}


def crawl_site(seed_url, max_pages=25):
    pages, queue, seen = [], [seed_url], set()
    scope = scope_params(seed_url)
    if scope:
        print(f"  범위: {scope} 인 페이지만 수집")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 1000}, locale="ko-KR")
        page = ctx.new_page()
        while queue and len(pages) < max_pages:
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            try:
                d = harvest_page(page, u)
            except Exception as e:
                print(f"  ! {u}: {str(e)[:120]}", file=sys.stderr)
                continue
            pages.append(d)
            print(f"  · {u}  (img {len(d['images'])}, table {len(d['tables'])}, links {len(d['links'])})")
            nxt = list(d["links"])
            # 입구(인트로) 페이지: 첫 페이지에 코스 링크가 거의 없으면 같은 사이트 링크를 따라 한 단계 더 들어간다
            if len(pages) == 1 and len(nxt) < 3 and d.get("other_links"):
                print(f"  입구 페이지로 보고 안쪽으로 들어감: {', '.join(d['other_links'][:4])}")
                nxt += d["other_links"]
            for l in nxt:
                if l not in seen and l not in queue and in_scope(l, scope):
                    queue.append(l)
        browser.close()
    return pages


# ---------- 2. LLM 구조화 ----------
SCHEMA_HINT = """
반드시 아래 JSON만 출력(설명·코드펜스 금지):
{
 "club_name": "골프장명",
 "courses": [
  {"name": "코스명(예: 웨스트)", "holes": [
    {"hole": 1, "par": 4, "hdcp": 7,
     "tees": {"BLACK": 420, "BLUE": 400, "WHITE": 380, "RED": 330},
     "greens": null,
     "unit": "yd",
     "image_url": "홀 코스맵 이미지 URL 또는 null",
     "image_confidence": 0.9,
     "note": "홀 공략 설명(있으면, 없으면 null)"}
  ]}
 ],
 "issues": ["확정 못한 항목 설명"]
}
규칙:
- 티 이름은 원문 표기(BLACK/BLUE/WHITE/GOLD/RED/챔피언/레귤러/레이디 등)를 그대로.
  티 이름이 페이지에 없으면 거리가 긴 순서대로 "T1","T2","T3"... 를 키로 쓰고 issues에 적는다.
- 티 이름이 글자 없이 색 점·아이콘(●)으로만 표시되면 색으로 이름을 붙인다:
  검정=BLACK, 파랑=BLUE, 흰색/빈 원=WHITE, 빨강=RED, 노랑·금색=GOLD, 초록=GREEN, 주황=ORANGE, 은색·회색=SILVER.
- 그린이 두 개인 홀(우그린/좌그린, A/B그린, 또는 표에 '좌·우'로 두 줄)은 "greens"에 그린별 전장을 모두 넣는다:
  "greens": {"우": {"BLACK": 404, "BLUE": 375, ...}, "좌": {"BLACK": 435, ...}}. 이때 "tees"에는 첫 번째 그린(보통 우) 값을 넣는다.
  그린이 하나면 "greens": null.
- 전장은 표에 있는 숫자를 우선. 표가 없으면 본문. 추정 금지, 없으면 tees를 {}로.
- 단위: 미터(m)와 야드(yd)가 함께 적혀 있으면 **미터 값을 쓰고 unit="m"**. 야드만 있으면 unit="yd".
- VISION 블록은 그 페이지 그림에서 읽은 값이다. 표·본문에 숫자가 없으면 VISION의 hole·par·hdcp·tees·unit을 쓰고,
  VISION의 map_image가 있으면 그 홀의 image_url로 쓴다. 페이지 주소의 코스 기호(예: /iframe/a3.html → a코스 3홀)와 탭 이름으로 코스를 정한다.
- image_url은 파일명/alt/주변 텍스트로 홀 번호가 식별될 때만 채운다(예: w3.jpg → 웨스트 3홀, hole_05.png → 5홀).
  같은 홀에 '코스맵(조감도)'과 '사진'이 둘 다 있으면 코스맵(일러스트/조감도) 쪽을 택한다(보통 파일명이 짧거나 img_ 접두가 없는 쪽).
  18홀/9홀이 한 장에 그려진 전체도는 image_url에 넣지 말고 issues에 URL을 적는다.
- 클럽하우스·풍경·배너·파트너 로고는 제외.
"""


USAGE = {"in": 0, "out": 0, "calls": 0}


def llm_json(client, content, max_tokens=16000):
    r = client.messages.create(model=MODEL, max_tokens=max_tokens, messages=[{"role": "user", "content": content}])
    USAGE["in"] += r.usage.input_tokens; USAGE["out"] += r.usage.output_tokens; USAGE["calls"] += 1
    # 모델이 답 앞에 thinking 블록을 붙일 수 있으므로 text 블록만 모은다
    txt = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
    if r.stop_reason == "max_tokens":
        raise ValueError("응답이 길이 제한에 걸려 잘림 — 페이지 수를 줄여 다시 시도(--max-pages)")
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError("LLM이 JSON을 반환하지 않음: " + txt[:200])
    return json.loads(m.group(0))


DIST_RE = re.compile(r"(\d{2,3}\s*(m|M|미터|yd|Yd|YD|야드)\b|par\s*\d|파\s*[345]\b|HDCP|핸디)", re.I)


def needs_vision(d):
    """파·거리가 글자로 없고 큰 그림이 1~4장인 페이지 = 숫자가 그림 안에 있을 가능성 (홀별 안내 페이지)"""
    if d["tables"] or len(DIST_RE.findall(d["text"])) >= 3:
        return False
    big = [i for i in d["images"] if max(i.get("w") or 0, i.get("h") or 0) >= 300]
    return 1 <= len(big) <= 4


def vision_page(client, d):
    """홀 안내 페이지의 그림들을 보고 홀 번호·파·핸디캡·티별 거리·코스맵 그림을 읽는다."""
    import base64
    content, urls = [], []
    for im in [i for i in d["images"] if max(i.get("w") or 0, i.get("h") or 0) >= 200][:4]:
        try:
            data = fetch_bytes(im["url"], referer=d["url"])
        except Exception:
            continue
        if len(data) > 4_500_000:
            continue
        mime = "image/png" if data[:4] == b"\x89PNG" else ("image/webp" if data[8:12] == b"WEBP" else ("image/gif" if data[:3] == b"GIF" else "image/jpeg"))
        content.append({"type": "text", "text": f"그림 {len(urls) + 1}: {im['url']}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}})
        urls.append(im["url"])
    if not urls:
        return None
    content.append({"type": "text", "text":
        f"골프장 홈페이지의 한 페이지({d['url']})에 있는 그림들이다. 페이지 글: {d['text'][:400]!r}\n"
        "그림 안에 적힌 숫자를 읽어라. 티 이름이 글자 없이 색 점으로만 있으면 색으로 이름을 붙여라"
        "(검정 BLACK, 파랑 BLUE, 흰색 WHITE, 빨강 RED, 노랑 GOLD, 초록 GREEN). 그린이 둘(우/좌)이면 \"greens\": {\"우\": {티: 거리}, \"좌\": {...}} 도 넣어라. "
        "한 홀 안내 페이지라면 "
        '{"hole": 번호, "par": n, "hdcp": n 또는 null, "tees": {"티 이름(그림에 적힌 그대로, 없으면 T1·T2… 긴 순서)": 거리숫자}, '
        '"unit": "m" 또는 "yd", "map_image": "홀 코스맵(조감도·공략도) 그림의 URL 또는 null"} '
        '여러 홀 정보가 한 그림에 있으면 {"holes": [위 형식의 배열]}. 홀 안내가 아니면 {"none": true}. '
        "m와 yd가 함께 있으면 m. 읽을 수 없는 값은 null. JSON만."})
    try:
        return llm_json(client, content, max_tokens=4000)
    except Exception as e:
        return {"error": str(e)[:120]}


def add_vision(client, pages, limit=45):
    """숫자가 그림에만 있는 페이지들을 병렬로 판독해 d['vision']에 붙인다."""
    from concurrent.futures import ThreadPoolExecutor
    targets = [d for d in pages if needs_vision(d)][:limit]
    if not targets:
        return
    print(f"  그림 판독: {len(targets)}페이지 (파·거리가 그림 안에 있는 페이지)")
    with ThreadPoolExecutor(max_workers=4) as ex:
        for d, v in zip(targets, ex.map(lambda x: vision_page(client, x), targets)):
            if v:
                d["vision"] = v


def structure(client, pages, club_hint):
    add_vision(client, pages)
    chunks = []
    per_page = max(1200, min(6000, 110000 // max(1, len(pages))))   # 페이지가 많으면 본문을 줄여 한도 안에
    for d in pages:
        chunks.append(f"### PAGE {d['url']}")
        if d["tables"]:
            chunks.append("TABLES:\n" + "\n\n".join(d["tables"]))
        if d.get("vision"):
            chunks.append("VISION(그림에서 읽은 값 — 표가 없으면 이 값을 사용):\n" + json.dumps(d["vision"], ensure_ascii=False))
        if d["images"]:
            chunks.append("IMAGES:\n" + "\n".join(
                f"- {i['url']} | alt={i['alt']} | {i.get('w')}x{i.get('h')} | ctx={i['ctx'][:120]}" for i in d["images"]))
        chunks.append("TEXT:\n" + d["text"][:per_page])
    prompt = f"골프장 '{club_hint}' 홈페이지에서 수집한 자료다. 홀별 코스 데이터로 구조화하라.\n{SCHEMA_HINT}\n\n" + "\n".join(chunks)
    return llm_json(client, prompt)


def classify_image(client, img_bytes, mime, course_names):
    import base64
    b64 = base64.b64encode(img_bytes).decode()
    q = ("이 이미지가 골프 '한 홀'의 코스맵(조감도/일러스트)이면 "
         '{"is_hole_map":true,"course":"코스명 또는 null","hole":번호,"par":n,"distances":{"티명":숫자},"unit":"m|yd","multi_hole":false}, '
         "여러 홀이 그려진 전체 코스도면 multi_hole:true, 코스맵이 아니면(사진·풍경·클럽하우스) is_hole_map:false. "
         f"코스 후보: {course_names}. 못 읽는 값은 null. JSON만.")
    return llm_json(client, [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
        {"type": "text", "text": q}], max_tokens=4000)


# ---------- 3. 후처리 ----------
def fetch_bytes(url, referer=None):
    """그림 받기. 오래된 골프장 서버는 SSL 연결이 가끔 끊기므로 재시도하고, 그래도 안 되면 http로 받는다."""
    import urllib.request
    last = None
    tries = [url] * 3 + ([url.replace("https://", "http://", 1)] if url.startswith("https://") else [])
    for i, u in enumerate(tries):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Referer": referer or u})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def download(url, dest: Path):
    data = fetch_bytes(url)
    dest.write_bytes(data)
    return data


def finalize(client, course_id, struct, pages):
    out = OUT / course_id
    (out / "images").mkdir(parents=True, exist_ok=True)
    review = list(struct.get("issues", []))
    all_imgs = {i["url"]: i for d in pages for i in d["images"]}
    course_names = [c["name"] for c in struct["courses"]]

    for c in struct["courses"]:
        for h in c["holes"]:
            u = h.get("image_url")
            h["image"] = None
            if u:
                ext = os.path.splitext(urlparse(u).path)[1] or ".jpg"
                fn = f"{c['name']}_{int(h['hole']):02d}{ext}"
                try:
                    download(u, out / "images" / fn)
                    h["image"] = f"images/{fn}"
                    all_imgs.pop(u, None)
                except Exception as e:
                    review.append(f"{c['name']} {h['hole']}홀 이미지 다운로드 실패 {u}: {e}")

    # 3-2a 순서 매칭: 같은 폴더에 홀 수와 정확히 같은 장수의 이미지가 있으면 페이지 순서대로 홀에 붙인다
    #      (파일명이 해시라 홀 번호를 알 수 없는 사이트 — 예: 골프존카운티 TU_GOLFCLUB_HOLE/*.png 27장)
    missing = [(c, h) for c in struct["courses"] for h in c["holes"] if not h["image"]]
    if missing:
        groups = {}
        for u, meta in all_imgs.items():                     # dict는 처음 나온 순서를 유지
            groups.setdefault(u.rsplit("/", 1)[0], []).append(u)
        cand = [g for g in groups.values() if len(g) == len(missing)]
        if len(cand) == 1:
            for (c, h), u in zip(missing, cand[0]):
                ext = os.path.splitext(urlparse(u).path)[1] or ".jpg"
                fn = f"{c['name']}_{int(h['hole']):02d}{ext}"
                try:
                    download(u, out / "images" / fn)
                    h["image"], h["image_url"], h["image_confidence"] = f"images/{fn}", u, 0.7
                    all_imgs.pop(u, None)
                except Exception as e:
                    review.append(f"{c['name']} {h['hole']}홀 이미지 다운로드 실패 {u}: {e}")
            review.append(f"코스맵 {len(cand[0])}장을 페이지 순서대로 홀에 연결함(파일명에 홀 번호 없음) — 앱에서 몇 홀 열어 맞는지 확인")
        elif len(cand) > 1:
            review.append(f"홀 수와 장수가 같은 이미지 묶음이 {len(cand)}개라 순서 매칭 보류")

    # 3-2b 번호 추론: 같은 코스에 그림이 있는 홀이 있고 파일명에 그 홀 번호가 들어 있으면(s_01.png, hole1.jpg)
    #      번호만 바꿔 빠진 홀 그림을 찾아본다 (예: 베르힐 영종 — 페이지는 9홀 모두 s_01.png를 걸고 JS로 바꿔 끼움)
    for c in struct["courses"]:
        known = [h for h in c["holes"] if h.get("image") and h.get("image_url")]
        need = [h for h in c["holes"] if not h.get("image")]
        if not known or not need:
            continue
        tmpl = None
        for h in known:
            base = h["image_url"].rsplit("/", 1)
            n = int(h["hole"])
            m = list(re.finditer(rf"(?<!\d)0*{n}(?!\d)", base[-1]))
            if len(m) == 1:
                width = len(m[0].group(0))
                tmpl = (base[0] + "/" + base[-1][:m[0].start()], base[-1][m[0].end():], width)
                break
        if not tmpl:
            continue
        found = 0
        for h in need:
            u = f"{tmpl[0]}{str(int(h['hole'])).zfill(tmpl[2])}{tmpl[1]}"
            fn = f"{c['name']}_{int(h['hole']):02d}{os.path.splitext(urlparse(u).path)[1] or '.jpg'}"
            try:
                data = fetch_bytes(u)
                if len(data) < 2000 or not (data[:4] == b"\x89PNG" or data[:3] == b"\xff\xd8\xff" or data[8:12] == b"WEBP" or data[:3] == b"GIF"):
                    continue
                (out / "images" / fn).write_bytes(data)
                h["image"], h["image_url"], h["image_confidence"] = f"images/{fn}", u, 0.75
                found += 1
            except Exception:
                continue
        if found:
            review.append(f"{c['name']}: 1번 홀 그림 이름의 번호를 바꿔 {found}개 홀 코스맵을 찾음 — 몇 홀 확인 권장")

    missing = [(c, h) for c in struct["courses"] for h in c["holes"] if not h["image"]]
    if missing and client:
        for u in list(all_imgs)[:60]:
            tmp = out / "images" / ("_tmp" + hashlib.md5(u.encode()).hexdigest()[:8])
            try:
                data = download(u, tmp)
            except Exception:
                continue
            mime = "image/png" if data[:4] == b"\x89PNG" else ("image/webp" if data[8:12] == b"WEBP" else "image/jpeg")
            try:
                res = classify_image(client, data, mime, course_names)
            except Exception as e:
                review.append(f"vision 실패 {u}: {str(e)[:80]}")
                continue
            if res.get("multi_hole"):
                review.append(f"전체 코스도(홀 분리 필요): {u}")
                continue
            if res.get("is_hole_map") and res.get("hole"):
                for c, h in missing:
                    if h["image"] or int(h["hole"]) != int(res["hole"]):
                        continue
                    if res.get("course") and res["course"] not in c["name"] and c["name"] not in res["course"]:
                        continue
                    fn = f"{c['name']}_{int(h['hole']):02d}.jpg"
                    (out / "images" / fn).write_bytes(data)
                    h["image"], h["image_confidence"] = f"images/{fn}", 0.6
                    if not h.get("tees") and res.get("distances"):
                        h["tees"], h["unit"] = res["distances"], res.get("unit", "m")
                        review.append(f"{c['name']} {h['hole']}홀 전장을 이미지에서 읽음(확인 필요)")
                    break
        for f in (out / "images").glob("_tmp*"):
            f.unlink()

    for c in struct["courses"]:
        for h in c["holes"]:
            unit = (h.get("unit") or "m").lower()
            tees = h.get("tees") or {}
            conv = lambda d: {k: (round(v * YD) if unit.startswith("y") else int(round(v)))
                              for k, v in (d or {}).items() if isinstance(v, (int, float))}
            h["tees_m"] = conv(tees)
            greens = h.get("greens") if isinstance(h.get("greens"), dict) else None
            h["greens_m"] = {g: conv(t) for g, t in greens.items() if conv(t)} if greens and len(greens) >= 2 else None
            if h["greens_m"] and not h["tees_m"]:
                h["tees_m"] = next(iter(h["greens_m"].values()))
            if not h["tees_m"]:
                review.append(f"{c['name']} {h['hole']}홀 전장 없음")
            if not h["image"]:
                review.append(f"{c['name']} {h['hole']}홀 코스맵 없음")
            if h.get("par") not in (3, 4, 5):
                review.append(f"{c['name']} {h['hole']}홀 파 이상: {h.get('par')}")

    struct.update({"course_id": course_id, "crawled_at": time.strftime("%Y-%m-%d %H:%M"), "unit": "m"})
    (out / "course.json").write_text(json.dumps(struct, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return review


def apply_tee_names(struct, names):
    """T1,T2,... (또는 이름 없는 티)를 거리 긴 순서대로 지정한 이름으로 바꾼다."""
    for c in struct["courses"]:
        for h in c["holes"]:
            tees = h.get("tees") or {}
            if not tees or not all(re.fullmatch(r"(T|TEE)\d+", k, re.I) for k in tees):
                continue
            order = sorted(tees.items(), key=lambda kv: -(kv[1] or 0))
            h["tees"] = {(names[i] if i < len(names) else k): v for i, (k, v) in enumerate(order)}
    struct["tee_order"] = names
    struct["issues"] = [s for s in struct.get("issues", []) if "티 이름" not in s] + [f"티 이름은 --tees 지정값({','.join(names)})을 긴 순서대로 붙임"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", help="seeds.json의 id")
    ap.add_argument("--name"); ap.add_argument("--url")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--no-llm", action="store_true", help="수집만 하고 raw_pages.json 저장")
    ap.add_argument("--id", help="결과 폴더/구장 id (기본: 주소에서 자동)")
    ap.add_argument("--tees", help="티 이름이 사이트에 없을 때 긴 순서대로 붙일 이름. 예: BLUE,WHITE,RED")
    ap.add_argument("--reuse", action="store_true", help="다시 방문하지 않고 직전 raw_pages.json으로 구조화만 다시")
    ap.add_argument("--course-names", help="코스 이름을 등장 순서대로 지정. 예: 이글,몬트,히든")
    a = ap.parse_args()

    seeds = json.loads(SEEDS.read_text(encoding="utf-8")) if SEEDS.exists() else {}
    if a.course:
        s = seeds[a.course]; course_id, name, url = course_id_for(s["url"]), s["name"], s["url"]
        a.tees = a.tees or s.get("tees")
        a.course_names = a.course_names or s.get("course_names")
    elif a.url:
        name, url = a.name or a.url, a.url
        course_id = course_id_for(url)
    else:
        ap.error("--course 또는 --url 필요")
    course_id = a.id or course_id

    raw = OUT / course_id / "raw_pages.json"
    if a.reuse and raw.exists():
        print(f"[1/3] 직전 수집본 재사용: {raw}")
        pages = json.loads(raw.read_text(encoding="utf-8"))
    else:
        print(f"[1/3] 수집: {name} {url}  → out/{course_id}/")
        pages = crawl_site(url, a.max_pages)
    if not pages:
        sys.exit("수집된 페이지가 없습니다. 주소가 맞는지, 브라우저로 열리는지 확인하세요.")
    (OUT / course_id).mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.no_llm:
        print("수집만 완료 → raw_pages.json"); return

    import anthropic
    client = anthropic.Anthropic()
    print(f"[2/3] 구조화 ({MODEL})")
    struct = structure(client, pages, name)
    if a.tees:
        apply_tee_names(struct, [t.strip().upper() for t in a.tees.split(",") if t.strip()])
    if a.course_names:
        names = [n.strip() for n in a.course_names.split(",") if n.strip()]
        if len(names) != len(struct["courses"]):
            print(f"  ! 코스 이름 {len(names)}개 ≠ 수집된 코스 {len(struct['courses'])}개 — 이름 지정 건너뜀")
        else:
            for c, n in zip(struct["courses"], names):
                c["name"] = n
            struct["issues"] = [s for s in struct.get("issues", []) if "코스명" not in s and "어느 9홀" not in s] + [f"코스 이름은 --course-names 지정값({','.join(names)}) 순서로 붙임"]
    print("[3/3] 이미지 다운로드·검증")
    review = finalize(client, course_id, struct, pages)
    n = sum(len(c["holes"]) for c in struct["courses"])
    print(f"완료: {n}홀, 검토 필요 {len(review)}건 → out/{course_id}/")
    (OUT / course_id / "usage.json").write_text(json.dumps(USAGE), encoding="utf-8")
    print(f"  토큰: in {USAGE['in']:,} / out {USAGE['out']:,} ({USAGE['calls']}회)")
    for r in review:
        print("  -", r)


if __name__ == "__main__":
    main()
