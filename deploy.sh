#!/usr/bin/env bash
# Cloudflare 배포 — 처음 한 번: 1~3단계, 이후 업데이트는 4단계만
set -e
cd "$(dirname "$0")"
npm i >/dev/null

echo "[1] 로그인"; npx wrangler login
echo "[2] D1·R2 생성"
npx wrangler d1 create golf-yardage || true          # 출력의 database_id 를 wrangler.toml 에 넣을 것
npx wrangler r2 bucket create golf-coursemaps || true
read -p "wrangler.toml 의 database_id 를 교체했으면 Enter: "
npx wrangler d1 execute golf-yardage --remote --file=cf/schema.sql

echo "[3] Pages 프로젝트 + 시크릿"
npx wrangler pages project create golf-yardage --production-branch main || true
echo "워커 키(아무 긴 문자열)를 입력:"; npx wrangler pages secret put WORKER_KEY --project-name golf-yardage

echo "[4] 배포"
npx wrangler pages deploy app --project-name golf-yardage
echo "→ https://golf-yardage.pages.dev  휴대폰에서 열고 '홈 화면에 추가'"
