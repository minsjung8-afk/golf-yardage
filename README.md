# golf-yardage — 골프장 코스맵 야디지 + 스코어 PWA (Cloudflare)

스택: Cloudflare Pages(앱) + Pages Functions(API) + D1(DB) + R2(코스맵) — 전부 무료 티어. 크롤링만 PC/VPS의 `worker.py`.

```
app/            모바일 PWA (index.html, sw.js, manifest, courses/seowon-hills.json 번들)
functions/api/  /api/* — courses, images(R2), rounds, hole_scores, stats, crawl-requests
cf/schema.sql   D1 스키마 (drive_m = length_m − remain_m 는 DB 생성 컬럼)
crawler/        crawl.py(수집) · worker.py(구장추가 큐 처리 + 업로드) · seeds.json
courses/        수집 완료 데이터 (서원힐스 27홀)
deploy_token.sh 배포·업데이트 스크립트 (여러 번 실행해도 안전)
```

## 배포·업데이트
```
export CLOUDFLARE_API_TOKEN=...  CLOUDFLARE_ACCOUNT_ID=...(32자리)
./deploy_token.sh
```
D1·R2·Pages가 없으면 만들고, DB를 최신 스키마로 올리고, 앱을 배포한다. 워커 키는 처음 한 번만 만들고 이후엔 유지한다.

## v2 변경점
- 닉네임+PIN 로그인. PIN 5회 틀리면 10분 잠김.
- 서버가 원본: 입력할 때마다 해당 홀만 즉시 저장. 통신이 끊기면 기기에 대기열로 두었다가 연결되면 자동 전송(상단 ⏳/⚠ 표시).
- 다른 휴대폰에서 같은 계정으로 들어오면 진행 중 라운드를 이어서 입력.
- 홈 화면에 서버 기준 통계(평균 스코어·베스트·티샷 평균/최장·퍼트·페어웨이)와 기록, 삭제.
- 구장추가: 이름만 넣으면 워커가 공식 홈페이지를 찾아 '이 주소가 맞나요?' 확인 → 수집.
- 코스 데이터는 워커 키 또는 본인이 등록한 구장만 수정 가능.

## 구장추가 자동 수집 (GitHub Actions) — 권장
```
./setup_github.sh
```
한 번 실행하면 끝. GitHub 로그인 → 비공개 저장소 생성·업로드 → 비밀값(ANTHROPIC_API_KEY·WORKER_KEY·APP_URL) 등록 →
GitHub 토큰(golf-yardage 저장소, Contents: Read and write)을 Cloudflare에 등록 → 재배포 → 시험 실행.
이후 앱에서 구장추가를 누르면 Cloudflare가 GitHub Actions를 깨워 1~3분 안에 처리한다. 신호가 누락돼도 매시 7분에 한 번 확인.
대기열이 비면 몇 초 만에 끝나 무료 한도(비공개 저장소 월 2,000분) 안에서 충분하다.
코드를 고친 뒤에는 `git add -A && git commit -m 수정 && git push` 로 올리면 다음 실행부터 반영된다.

## 워커 직접 실행 (자동 수집을 안 쓸 때)
```
cd crawler
pip install playwright anthropic beautifulsoup4 requests && playwright install chromium
export APP_URL=https://golf-yardage.pages.dev WORKER_KEY=<위에서 넣은 값> ANTHROPIC_API_KEY=sk-ant-...
python worker.py --upload seowon-hills   # 번들 데이터를 D1에 적재(한 번)
python worker.py                          # 상시 대기: 앱의 구장추가 요청 처리
```
앱 [구장] → [구장추가] → 골프장명(주소는 선택) → 워커가 홈페이지 검색 → 앱에서 주소 확인 → 크롤링(2~5분) → 목록에 등장.
홈페이지 검색만 시험: `python worker.py --find "이글몬트CC"`
워커 없이도 `python crawl.py --url ...` 결과(`out/<id>/course.json`)를 앱 [JSON 가져오기]로 바로 쓸 수 있음.

## 거리 규칙
- 티샷 거리 = 선택 티 전장(m) − 세컨샷 지점 잔여거리(m). 파3는 입력 없음.
- 야드 표기 구장은 수집 시 ×0.9144, 앱은 m만.
- 입력은 즉시 서버 저장, 오프라인일 때만 기기 대기열.

## 알아둘 것
- 코스맵은 기본적으로 골프장 원본 URL 참조. 원본이 깨지면 worker.py가 R2로 옮겨 `/api/images/...`로 제공.
- 로컬 테스트: `npm i && npx wrangler d1 execute golf-yardage --local --file=cf/schema.sql && echo WORKER_KEY=dev-key-change-me > .dev.vars && npx wrangler pages dev app --port 8788`
