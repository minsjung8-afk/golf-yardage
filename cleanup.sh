#!/usr/bin/env bash
# 수집되지 않은 채 남은 '수집중/실패' 빈 구장과 v1 시절 구장추가 요청을 정리한다.
# 실행: ./cleanup.sh   (Cloudflare 로그인 창이 뜨면 Allow)
set -euo pipefail
cd "$(dirname "$0")"
W="npx wrangler"; P=golf-yardage
npm i --silent
$W whoami 2>/dev/null | grep -qi "associated with the email\|You are logged in" || $W login
ID=$($W d1 list --json 2>/dev/null | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const x=JSON.parse(s).find(d=>d.name==="'$P'");console.log(x?x.uuid:"")})')
sed -i.bak "s/database_id = \".*\"/database_id = \"$ID\"/" wrangler.toml && rm -f wrangler.toml.bak

echo "== 지울 대상 (홀 데이터 없는 수집중·실패 구장)"
$W d1 execute $P --remote --command "select id, club_name, status from courses where status!='ready' and id not in (select distinct course_id from holes)"
read -p "위 구장을 지울까요? (y/N) " yn
[ "$yn" = "y" ] || exit 0
$W d1 execute $P --remote --command "delete from courses where status!='ready' and id not in (select distinct course_id from holes)"
$W d1 execute $P --remote --command "delete from crawl_requests where user_id is null"
echo "정리 끝. 앱에서 [구장]을 다시 열면 사라져 있습니다."
