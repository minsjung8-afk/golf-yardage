#!/usr/bin/env python3
"""
crawler/fixes/<course_id>.json 의 홀별 티 거리를 서버 구장 데이터에 덮어쓴다(검수로 확인한 값 반영용).
  python apply_fix.py golfclubq-com
fixes 형식: {"tee_order": [...], "holes": {"코스명": {"홀번호": [티 순서대로 m], ...}}}  — 없는 홀은 그대로 둔다.
"""
import os, sys, json
from pathlib import Path
import requests
APP = os.environ.get("APP_URL", "https://golf-yardage.pages.dev").rstrip("/")
KEY = os.environ["WORKER_KEY"]
cid = sys.argv[1]
fix = json.loads((Path(__file__).parent / "fixes" / f"{cid}.json").read_text(encoding="utf-8"))
c = next(x for x in requests.get(f"{APP}/api/courses", timeout=30).json() if x["course_id"] == cid)
order, n, kept = fix["tee_order"], 0, []
for k in c["courses"]:
    for h in k["holes"]:
        v = fix["holes"].get(k["name"], {}).get(str(h["hole"]))
        if v:
            h["tees_m"] = {order[i]: m for i, m in enumerate(v) if m}
            n += 1
        else:
            kept.append(f"{k['name']} {h['hole']}")
body = {"course_id": cid, "club_name": c["club_name"], "tee_order": order,
        "courses": [{"name": k["name"], "holes": k["holes"]} for k in c["courses"]]}
r = requests.post(f"{APP}/api/courses", json=body, headers={"X-Worker-Key": KEY}, timeout=60); r.raise_for_status()
print(f"{c['club_name']}: {n}홀 반영 · 그대로 둔 홀: {', '.join(kept) or '없음'}")
