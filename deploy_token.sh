#!/usr/bin/env bash
# Cloudflare 배포·업데이트 (여러 번 실행해도 안전)
#   ./deploy_token.sh                  # 토큰 없이: 처음 한 번 브라우저 로그인 창이 뜸 (권장)
#   또는 토큰으로:
#   export CLOUDFLARE_API_TOKEN=...   # 권한: Workers Scripts·Pages·D1·R2 편집
#   export CLOUDFLARE_ACCOUNT_ID=...  # 32자리 (주소창의 /home 같은 경로는 빼고)
# 하는 일: D1·R2·Pages 없으면 생성 → DB 스키마/업그레이드 → (처음만) 워커 키 등록 → 앱 배포
set -euo pipefail
cd "$(dirname "$0")"
P=golf-yardage
W="npx wrangler"
npm i --silent
if [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  [[ "$CLOUDFLARE_ACCOUNT_ID" =~ ^[0-9a-f]{32}$ ]] || { echo "계정 ID 형식 오류: '$CLOUDFLARE_ACCOUNT_ID' (32자리 영숫자만)"; exit 1; }
fi
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
  if ! $W whoami 2>/dev/null | grep -qi "associated with the email\|You are logged in"; then
    echo "[0] Cloudflare 로그인 — 브라우저 창에서 Allow 를 누르세요"; $W login
  fi
fi

d1id() { $W d1 list --json 2>/dev/null | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{const x=JSON.parse(s).find(d=>d.name==="'$P'");console.log(x?x.uuid:"")}catch{console.log("")}})'; }

echo "[1/5] D1"
DBID=$(d1id)
if [ -z "$DBID" ]; then $W d1 create $P >/dev/null 2>&1 || true; DBID=$(d1id); fi
[[ "$DBID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || { echo "D1 id를 못 찾음: '$DBID'  → npx wrangler d1 list 로 확인"; exit 1; }
sed -i.bak "s/database_id = \".*\"/database_id = \"$DBID\"/" wrangler.toml && rm -f wrangler.toml.bak
echo "  $DBID"

echo "[2/5] DB 스키마·업그레이드"
$W d1 execute $P --remote --file=cf/schema.sql >/dev/null
grep -E '^(ALTER|CREATE)' cf/migrate_002.sql | while read -r s; do
  if $W d1 execute $P --remote --command "$s" >/dev/null 2>&1; then echo "  적용: ${s:0:60}"; else echo "  이미 적용됨: ${s:0:50}"; fi
done

echo "[3/5] R2"
$W r2 bucket create golf-coursemaps >/dev/null 2>&1 && echo "  생성" || echo "  이미 있음"

echo "[4/5] Pages 프로젝트·워커 키"
$W pages project create $P --production-branch main >/dev/null 2>&1 && echo "  프로젝트 생성" || echo "  프로젝트 있음"
NEWKEY=""
if [ -n "${WORKER_KEY:-}" ]; then
  echo "$WORKER_KEY" | $W pages secret put WORKER_KEY --project-name $P >/dev/null; echo "  지정한 WORKER_KEY로 교체"
elif ! $W pages secret list --project-name $P 2>/dev/null | grep -q WORKER_KEY; then
  NEWKEY=$(openssl rand -hex 24)
  echo "$NEWKEY" | $W pages secret put WORKER_KEY --project-name $P >/dev/null; echo "  새 워커 키 등록"
else
  echo "  기존 워커 키 유지"
fi

echo "[5/5] 배포"
$W pages deploy app --project-name $P --branch main --commit-dirty=true
echo
echo "URL : https://$P.pages.dev"
[ -n "$NEWKEY" ] && echo "WORKER_KEY : $NEWKEY   ← worker.py 실행에 필요, 따로 보관"
echo "끝. 휴대폰에서 앱을 완전히 닫았다 다시 열면 새 버전이 뜹니다."
