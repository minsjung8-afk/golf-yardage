#!/usr/bin/env python3
"""
지역 골프장 목록 만들기 — 시·군·구마다 Claude 웹 검색으로 골프장 이름을 모아 regions/<name>.json 에 저장.
  python region_list.py gg_incheon
9홀 이상 골프장(회원제·대중제)만, 파3·스크린·연습장 제외.
"""
import os, sys, json, re, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import anthropic

MODEL = os.environ.get("CRAWL_MODEL", "claude-sonnet-5-5")
HERE = Path(__file__).resolve().parent
REGIONS = {
    "gg_incheon": {
        "경기도": ["가평군", "고양시", "광주시", "김포시", "남양주시", "동두천시", "부천시", "성남시", "수원시", "시흥시", "안산시",
                 "안성시", "안양시", "양주시", "양평군", "여주시", "연천군", "용인시", "이천시", "파주시", "평택시", "포천시",
                 "하남시", "화성시", "군포시", "의왕시", "과천시", "광명시", "구리시", "오산시", "의정부시"],
        "인천광역시": ["강화군", "옹진군", "연수구", "서구·청라", "영종도(중구)", "검단", "남동구", "계양구", "부평구", "미추홀구"],
    }
}
usage = {"in": 0, "out": 0, "search": 0}


def ask(area):
    client = anthropic.Anthropic()
    prompt = (f"대한민국 {area}에 있는 골프장(9홀 이상 정규 코스, 회원제·대중제 모두)을 웹 검색으로 빠짐없이 찾아라. "
              "파3 전용·스크린골프·연습장·파크골프는 제외. 폐장한 곳 제외. 이름은 공식 명칭(예: OO컨트리클럽, OO CC, OO골프클럽).\n"
              '마지막 줄에 JSON 배열만: [{"name": "공식명", "holes": 18, "homepage": "https://... 또는 null"}]')
    for attempt in range(3):
        try:
            r = client.messages.create(model=MODEL, max_tokens=8000,
                                       tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8,
                                               "user_location": {"type": "approximate", "country": "KR"}}],
                                       messages=[{"role": "user", "content": prompt}])
            usage["in"] += r.usage.input_tokens; usage["out"] += r.usage.output_tokens
            usage["search"] += getattr(getattr(r.usage, "server_tool_use", None), "web_search_requests", 0) or 0
            text = "".join(b.text for b in r.content if b.type == "text")
            m = re.findall(r"\[\s*\{.*?\}\s*\]", text, re.S)
            if m:
                return json.loads(m[-1])
            if re.search(r"\[\s*\]\s*`*\s*$", text):
                return []
            print("  목록 해석 실패, 재시도:", area, r.stop_reason, text[-200:].replace("\n", " "))
        except Exception as e:
            print("  재시도", area, e); time.sleep(5 * (attempt + 1))
    return []


def norm(n):
    return re.sub(r"\s+|컨트리클럽|골프클럽|골프앤리조트|골프&리조트|골프리조트|골프장|cc|gc|c\.c|g\.c|\(.*?\)", "", n.lower())


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "gg_incheon"
    only = [x.strip() for x in sys.argv[2].split(",")] if len(sys.argv) > 2 and sys.argv[2].strip() else None
    areas = [(p, a) for p, lst in REGIONS[key].items() for a in lst if not only or a in only]
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda pa: (pa, ask(f"{pa[0]} {pa[1]}")), areas))
    f = HERE / "regions" / f"{key}.json"
    out = json.loads(f.read_text(encoding="utf-8")) if (only and f.exists()) else []   # 일부 지역만 다시 → 기존 목록에 합침
    seen = {norm(g["name"]): True for g in out}
    before = len(out)
    for (prov, area), lst in results:
        print(f"{prov} {area}: {len(lst)}곳")
        for g in lst:
            n = (g.get("name") or "").strip()
            if not n or (g.get("holes") and int(g["holes"]) < 9):
                continue
            k = norm(n)
            if k in seen:
                continue
            seen[k] = True
            out.append({"name": n, "province": prov, "area": area, "holes": g.get("holes"), "homepage": g.get("homepage")})
    d = HERE / "regions"; d.mkdir(exist_ok=True)
    (d / f"{key}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if only:
        print(f"추가 {len(out) - before}곳")
    print(f"총 {len(out)}곳 → regions/{key}.json · 토큰 in {usage['in']:,} out {usage['out']:,} 검색 {usage['search']}회")


if __name__ == "__main__":
    main()
