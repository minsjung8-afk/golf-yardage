#!/usr/bin/env python3
"""
검수용: 서버의 구장 데이터와 홀별 코스맵 그림을 내려받아 inspect/ 폴더에 저장한다.
  python dump.py "골프클럽Q"        # 이름 일부 또는 course_id
환경변수: APP_URL
"""
import os, sys, json, re
from pathlib import Path
import requests

APP = os.environ.get("APP_URL", "https://golf-yardage.pages.dev").rstrip("/")
q = sys.argv[1].strip().lower().replace(" ", "")
courses = requests.get(f"{APP}/api/courses", timeout=30).json()
hits = [c for c in courses if q in c["course_id"].lower() or q in c["club_name"].lower().replace(" ", "")]
print("구장 목록:", ", ".join(f"{c['club_name']}({c['course_id']})" for c in courses))
if not hits:
    sys.exit(f"'{sys.argv[1]}' 에 맞는 구장 없음")
out = Path("inspect"); out.mkdir(exist_ok=True)
for c in hits:
    d = out / c["course_id"]; d.mkdir(exist_ok=True)
    (d / "course.json").write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    n = 0
    for k in c["courses"]:
        for h in k["holes"]:
            u = h.get("image_url")
            if not u:
                continue
            if u.startswith("/"):
                u = APP + u
            try:
                r = requests.get(u, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                ext = os.path.splitext(u.split("?")[0])[1] or ".jpg"
                (d / f"{k['name']}_{int(h['hole']):02d}{ext}").write_bytes(r.content)
                n += 1
            except Exception as e:
                print("  그림 실패", k["name"], h["hole"], e)
    print(f"{c['club_name']} ({c['course_id']}): 그림 {n}장 저장")
