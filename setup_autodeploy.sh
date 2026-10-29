#!/usr/bin/env bash
# 마지막 터미널 작업. 이후로는 GitHub에 코드가 올라가면 자동 배포된다.
#   ./setup_autodeploy.sh
# 하는 일: 최신 코드를 GitHub에 올리고, Cloudflare 토큰·계정 ID를 GitHub 비밀값으로 등록한 뒤 첫 자동 배포를 실행
set -euo pipefail
cd "$(dirname "$0")"
say() { printf "\n\033[1;32m▶ %s\033[0m\n" "$*"; }
ask_secret() { local v; read -r -s -p "$1: " v; echo >&2; printf "%s" "$v"; }
command -v gh >/dev/null || export PATH="$HOME/bin:$PATH"
REPO="$(gh api user --jq .login)/golf-yardage"

say "1/3 최신 코드 업로드 → $REPO"
git add -A
git commit -qm "update $(date +%Y-%m-%d\ %H:%M)" || echo "  변경 없음"
git push -q origin HEAD:main && echo "  완료"

say "2/3 Cloudflare 토큰을 GitHub 비밀값으로 등록 (화면에 안 보임)"
have=$(gh secret list -R "$REPO" | awk '{print $1}')
if grep -qx CLOUDFLARE_API_TOKEN <<<"$have" && [ -z "${RESET:-}" ]; then
  echo "  이미 등록됨 (바꾸려면 RESET=1 ./setup_autodeploy.sh)"
else
  cat <<EOT
  Cloudflare 대시보드 → My Profile → API Tokens → Create Token → "Edit Cloudflare Workers" 템플릿
  → Permissions에 Account · D1 · Edit, Account · Cloudflare Pages · Edit 추가 → Create → 토큰 복사
EOT
  open "https://dash.cloudflare.com/profile/api-tokens" 2>/dev/null || true
  T=$(ask_secret "Cloudflare 토큰 (cfut_… 또는 긴 문자열)")
  A=$(ask_secret "Cloudflare 계정 ID (32자리, /home 같은 경로 빼고)")
  [[ "$A" =~ ^[0-9a-f]{32}$ ]] || { echo "  계정 ID 형식 오류"; exit 1; }
  ok=$(curl -s -H "Authorization: Bearer $T" "https://api.cloudflare.com/client/v4/accounts/$A/pages/projects/golf-yardage" | python3 -c "import json,sys;print(json.load(sys.stdin).get('success'))")
  [ "$ok" = "True" ] || { echo "  토큰으로 Pages 프로젝트에 접근 실패 — 권한(Cloudflare Pages · Edit)과 계정 ID를 확인하세요"; exit 1; }
  printf "%s" "$T" | gh secret set CLOUDFLARE_API_TOKEN -R "$REPO"
  printf "%s" "$A" | gh secret set CLOUDFLARE_ACCOUNT_ID -R "$REPO"
  echo "  등록·확인 완료"
fi

say "3/3 첫 자동 배포"
gh workflow run deploy -R "$REPO" >/dev/null && echo "  시작됨 — 1~2분 뒤 https://github.com/$REPO/actions 에서 초록 체크 확인"
echo
echo "끝. 이제부터 GitHub에 코드가 올라가면 자동으로 배포됩니다."
echo "Claude가 직접 올리게 하려면: claude.ai → 설정 → 커넥터(Connectors) → GitHub 연결 → golf-yardage 저장소 접근 허용"
