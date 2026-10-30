#!/usr/bin/env python3
"""
지역 일괄 수집 — regions/<key>.json 의 골프장을 차례로 (홈페이지 찾기 → 수집 → 업로드).
진행 상황은 bulk_state/<key>.json 에 남겨 다음 실행이 이어서 한다(이미 끝난 곳·서버에 있는 곳은 건너뜀).

  python bulk.py gg_incheon --limit 10          # 시범 10곳
  python bulk.py gg_incheon --limit 999 --budget-min 300
  python bulk.py gg_incheon --retry-failed
환경변수: APP_URL, WORKER_KEY, ANTHROPIC_API_KEY
"""
import os, sys, json, re, time, argparse, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import worker
from crawl import course_id_for

# 대략적 단가(USD/백만 토큰) — 실제 청구액은 Anthropic 콘솔 기준
PRICE_IN, PRICE_OUT, PRICE_SEARCH = 3.0, 15.0, 10.0 / 1000
KRW = 1400


def norm(n):
    return re.sub(r"\s+|컨트리클럽|골프클럽|골프앤리조트|골프&리조트|골프리조트|골프장|리조트|cc|gc|c\.c|g\.c|\(.*?\)|[·\-_.]", "", (n or "").lower())


def cost_usd(u):
    return u.get("in", 0) / 1e6 * PRICE_IN + u.get("out", 0) / 1e6 * PRICE_OUT + u.get("search", 0) * PRICE_SEARCH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--budget-min", type=int, default=300, help="이 시간(분)이 지나면 다음 곳을 시작하지 않음")
    ap.add_argument("--retry-failed", action="store_true")
    a = ap.parse_args()

    targets = json.loads((HERE / "regions" / f"{a.key}.json").read_text(encoding="utf-8"))
    sf = HERE / "bulk_state" / f"{a.key}.json"
    sf.parent.mkdir(exist_ok=True)
    state = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {}

    def save():
        sf.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    server = worker.api("GET", "/courses")
    have_names = {norm(c["club_name"]) for c in server if c.get("status", "ready") == "ready"}
    have_ids = {c["course_id"] for c in server if c.get("status", "ready") == "ready"}

    started, done_now = time.time(), 0
    for t in targets:
        name = t["name"]; k = norm(name)
        if re.search(r"체력단련장|river bend|camp |미군|공군|육군|해군", name, re.I):
            continue
        st = state.get(name, {})
        if st.get("status") == "done" or (st.get("status") in ("failed", "no_homepage") and not a.retry_failed):
            continue
        if k in have_names:
            state[name] = {**st, "status": "done", "message": "이미 서버에 있음"}; save(); continue
        if done_now >= a.limit:
            break
        if (time.time() - started) / 60 > a.budget_min:
            print("시간 한도 — 다음 실행에서 이어감"); break
        done_now += 1
        usage = {"in": 0, "out": 0, "search": 0}
        t0 = time.time()
        print(f"\n=== [{done_now}] {name} ({t.get('province')} {t.get('area')})")

        # 1) 홈페이지
        url = None
        if t.get("homepage"):
            ok, final = worker.reachable(t["homepage"])
            if ok and not worker.NOT_OFFICIAL.search(final):
                url = final
        if not url:
            try:
                url, note = worker.find_homepage(name)
                usage["search"] += 4; usage["in"] += 20000; usage["out"] += 800   # 검색 1회 대략치
            except Exception as e:
                url, note = None, f"검색 실패: {e}"
            if not url:
                state[name] = {"status": "no_homepage", "message": note[:300], "cost_usd": round(cost_usd(usage), 3)}
                save(); print("  홈페이지 없음:", note); continue
        cid = course_id_for(url)
        if cid in have_ids:
            state[name] = {"status": "done", "url": url, "course_id": cid, "message": "이미 서버에 있음(주소 일치)"}; save(); continue
        print("  주소:", url, "→", cid)

        # 2) 수집
        try:
            p = subprocess.run([sys.executable, str(HERE / "crawl.py"), "--name", name, "--url", url, "--id", cid],
                               capture_output=True, text=True, timeout=1200)
            log = (p.stdout or "") + (p.stderr or "")
            print(log[-1200:])
            uf = worker.OUT / cid / "usage.json"
            if uf.exists():
                u = json.loads(uf.read_text()); usage["in"] += u["in"]; usage["out"] += u["out"]
            if p.returncode != 0:
                raise RuntimeError(log.strip().splitlines()[-1][:300] if log.strip() else "crawl.py 실패")
            cj = json.loads((worker.OUT / cid / "course.json").read_text(encoding="utf-8"))
            n = sum(len(c.get("holes") or []) for c in cj.get("courses", []))
            if n == 0:
                raise RuntimeError("홀 정보를 못 찾음: " + "; ".join(cj.get("issues", [])[:2])[:250])
            imgs = sum(1 for c in cj["courses"] for h in c["holes"] if h.get("image"))
            worker.upload_course(cid)
            review = json.loads((worker.OUT / cid / "review.json").read_text(encoding="utf-8"))
            state[name] = {"status": "done", "url": url, "course_id": cid, "holes": n, "maps": imgs,
                           "review": review[:5], "cost_usd": round(cost_usd(usage), 3), "sec": int(time.time() - t0)}
            have_ids.add(cid); have_names.add(k)
            print(f"  ✔ {n}홀 · 맵 {imgs}장 · ${state[name]['cost_usd']}")
        except subprocess.TimeoutExpired:
            state[name] = {"status": "failed", "url": url, "course_id": cid, "message": "20분 시간 초과(해외 접속 차단 가능)",
                           "cost_usd": round(cost_usd(usage), 3)}
        except Exception as e:
            state[name] = {"status": "failed", "url": url, "course_id": cid, "message": str(e)[:300],
                           "cost_usd": round(cost_usd(usage), 3), "sec": int(time.time() - t0)}
            print("  ✘", e)
        save()

    # 요약
    vals = list(state.values())
    cnt = lambda s: sum(1 for v in vals if v.get("status") == s)
    spent = sum(v.get("cost_usd", 0) for v in vals)
    tried = [v for v in vals if "cost_usd" in v]
    left = sum(1 for t in targets if state.get(t["name"], {}).get("status") not in ("done", "failed", "no_homepage"))
    avg = spent / max(1, len(tried))
    summary = (f"대상 {len(targets)} · 완료 {cnt('done')} · 실패 {cnt('failed')} · 홈페이지없음 {cnt('no_homepage')} · 남음 {left}\n"
               f"지금까지 약 ${spent:.2f} (₩{spent*KRW:,.0f}) · 곳당 평균 ${avg:.2f} → 남은 곳 예상 ${avg*left:.0f} (₩{avg*left*KRW:,.0f})")
    print("\n" + summary)
    (sf.parent / f"{a.key}.summary.txt").write_text(summary, encoding="utf-8")


if __name__ == "__main__":
    main()
