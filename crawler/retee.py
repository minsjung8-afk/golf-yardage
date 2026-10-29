#!/usr/bin/env python3
"""
이미 서버에 올라간 구장의 티 이름을 바꾼다. (다시 수집할 필요 없음)
홀마다 티 수가 다른 구장(예: 더크로스비 4티·5티 혼재)은 티 수별로 이름표를 따로 준다.

  python retee.py <course_id> "5=RED,GREEN,WHITE1,WHITE2,BLUE" "4=RED,GREEN,WHITE,BLUE"
  python retee.py golfzoncounty-com-68 "BLUE,WHITE,RED"          # 모든 홀 공통

이름은 거리가 긴 티부터 순서대로 붙는다. 환경변수: APP_URL, WORKER_KEY
"""
import os, sys, json
import requests

APP = os.environ.get("APP_URL", "https://golf-yardage.pages.dev").rstrip("/")
KEY = os.environ["WORKER_KEY"]


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cid, specs = sys.argv[1], sys.argv[2:]
    by_count, common = {}, None
    for s in specs:
        if "=" in s and s.split("=", 1)[0].strip().isdigit():
            n, names = s.split("=", 1)
            by_count[int(n)] = [x.strip().upper() for x in names.split(",") if x.strip()]
        else:
            common = [x.strip().upper() for x in s.split(",") if x.strip()]

    courses = requests.get(f"{APP}/api/courses", timeout=30).json()
    c = next((x for x in courses if x["course_id"] == cid), None)
    if not c:
        sys.exit(f"구장 없음: {cid}  (있는 구장: {', '.join(x['course_id'] for x in courses)})")

    order, changed, skipped = [], 0, []
    for k in c["courses"]:
        for h in k["holes"]:
            tees = sorted((h.get("tees_m") or {}).items(), key=lambda kv: -(kv[1] or 0))
            names = by_count.get(len(tees)) or common
            if not names or len(names) < len(tees):
                skipped.append(f"{k['name']} {h['hole']}홀({len(tees)}티)")
                continue
            h["tees_m"] = {names[i]: m for i, (_, m) in enumerate(tees)}
            for nm in names[:len(tees)]:
                if nm not in order:
                    order.append(nm)
            changed += 1
    # 선택 목록 순서: 긴 티부터 (이름이 여러 표에 걸쳐도 처음 나온 순서 유지)
    #   여러 이름표가 섞이면 각 이름의 상대 위치(순번/길이)로 정렬 → RED, GREEN, WHITE1, WHITE, WHITE2, BLUE
    pos = {}
    for lst in ([common] if common else []) + list(by_count.values()):
        for i, n in enumerate(lst):
            pos.setdefault(n, []).append(i / max(1, len(lst) - 1))
    tee_order = sorted([n for n in order], key=lambda n: sum(pos[n]) / len(pos[n]))

    body = {"course_id": cid, "club_name": c["club_name"], "tee_order": tee_order,
            "courses": [{"name": k["name"], "holes": k["holes"]} for k in c["courses"]]}
    r = requests.post(f"{APP}/api/courses", json=body, headers={"X-Worker-Key": KEY}, timeout=60)
    r.raise_for_status()
    print(f"{c['club_name']}: {changed}홀 티 이름 변경 → {tee_order}")
    if skipped:
        print("  이름표가 없어 그대로 둔 홀:", ", ".join(skipped))


if __name__ == "__main__":
    main()
