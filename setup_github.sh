#!/usr/bin/env bash
# 구장추가 수집기를 GitHub Actions로 자동 실행하도록 한 번에 설정한다. (맥)
#   ./setup_github.sh
# 하는 일
#   1) GitHub CLI(gh) 설치·로그인        — 브라우저 로그인 창
#   2) 비공개 저장소 golf-yardage 생성·업로드
#   3) 저장소 비밀값 등록: ANTHROPIC_API_KEY, WORKER_KEY, APP_URL   — 입력값은 화면에 안 보임
#   4) 앱이 GitHub를 깨울 수 있게 토큰을 Cloudflare에 등록           — GitHub 토큰 1개 생성 필요
#   5) 앱 재배포 + 수집기 시험 실행
# 여러 번 실행해도 안전하다(이미 된 단계는 건너뜀).
set -euo pipefail
cd "$(dirname "$0")"
REPO_NAME=golf-yardage
APP_URL_DEFAULT=https://golf-yardage.pages.dev
say() { printf "\n\033[1;32m▶ %s\033[0m\n" "$*"; }
ask_secret() { local v; read -r -s -p "$1: " v; echo >&2; printf "%s" "$v"; }

# ---------- 1. gh ----------
say "1/5 GitHub CLI"
if ! command -v gh >/dev/null; then
  if command -v brew >/dev/null; then brew install gh
  else echo "gh가 없습니다. https://cli.github.com 에서 macOS 설치 파일을 받아 설치한 뒤 다시 실행하세요."; open https://cli.github.com; exit 1; fi
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "브라우저가 열리면 화면의 코드를 입력하고 승인하세요."
  gh auth login --hostname github.com --git-protocol https --web --scopes workflow
fi
gh auth status 2>&1 | grep -q workflow || gh auth refresh -h github.com -s workflow
OWNER=$(gh api user --jq .login)
REPO="$OWNER/$REPO_NAME"
gh auth setup-git >/dev/null 2>&1 || true
echo "  계정: $OWNER"

# ---------- 2. 저장소 ----------
say "2/5 저장소 $REPO (비공개)"
[ -d .git ] || git init -q -b main
git config user.name  >/dev/null || git config user.name  "$OWNER"
git config user.email >/dev/null || git config user.email "$OWNER@users.noreply.github.com"
git add -A
git commit -qm "golf-yardage $(date +%Y-%m-%d\ %H:%M)" || echo "  변경 없음"
if gh repo view "$REPO" >/dev/null 2>&1; then
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$REPO.git"
  git push -q -u origin HEAD:main
  echo "  기존 저장소에 업로드"
else
  gh repo create "$REPO" --private --source=. --remote=origin --push >/dev/null
  echo "  새 저장소 생성·업로드"
fi

# ---------- 3. 저장소 비밀값 ----------
say "3/5 저장소 비밀값"
have=$(gh secret list -R "$REPO" 2>/dev/null | awk '{print $1}')
if ! grep -qx APP_URL <<<"$have"; then printf "%s" "${APP_URL:-$APP_URL_DEFAULT}" | gh secret set APP_URL -R "$REPO"; echo "  APP_URL 등록"; else echo "  APP_URL 있음"; fi
if ! grep -qx WORKER_KEY <<<"$have"; then
  K="${WORKER_KEY:-}"; [ -n "$K" ] || K=$(ask_secret "워커 키 (배포 때 나온 48자리)")
  printf "%s" "$K" | gh secret set WORKER_KEY -R "$REPO"; echo "  WORKER_KEY 등록"
else echo "  WORKER_KEY 있음"; fi
if ! grep -qx ANTHROPIC_API_KEY <<<"$have"; then
  K="${ANTHROPIC_API_KEY:-}"; [ -n "$K" ] || K=$(ask_secret "Anthropic API 키 (sk-ant-api03-…)")
  [[ "$K" == sk-ant-* ]] || { echo "  sk-ant- 로 시작하는 키가 아닙니다. 다시 실행하세요."; exit 1; }
  printf "%s" "$K" | gh secret set ANTHROPIC_API_KEY -R "$REPO"; echo "  ANTHROPIC_API_KEY 등록"
else echo "  ANTHROPIC_API_KEY 있음"; fi

# ---------- 4. Cloudflare → GitHub 깨우기 토큰 ----------
say "4/5 앱이 수집기를 깨울 토큰"
npm i --silent
W="npx wrangler"
$W whoami 2>/dev/null | grep -qi "associated with the email\|You are logged in" || { echo "Cloudflare 로그인 창에서 Allow 를 누르세요."; $W login; }
if $W pages secret list --project-name golf-yardage 2>/dev/null | grep -q GH_TOKEN && [ -z "${RESET_GH_TOKEN:-}" ]; then
  echo "  이미 등록됨 (다시 만들려면 RESET_GH_TOKEN=1 ./setup_github.sh)"
else
  cat <<EOT
  브라우저에서 GitHub 토큰 만들기 화면이 열립니다. 아래대로 입력하세요.
    · Token name: golf-yardage-wake      · Expiration: 1 year (또는 No expiration)
    · Repository access: Only select repositories → $REPO_NAME
    · Permissions → Repository permissions → Contents: Read and write
    · 맨 아래 Generate token → 나온 github_pat_… 복사
EOT
  open "https://github.com/settings/personal-access-tokens/new" 2>/dev/null || true
  T=$(ask_secret "github_pat_ 토큰 붙여넣기")
  code=$(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: Bearer $T" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/dispatches" -d '{"event_type":"crawl"}')
  [ "$code" = "204" ] || { echo "  토큰 확인 실패(HTTP $code). 저장소 선택과 Contents: Read and write 권한을 확인하고 다시 실행하세요."; exit 1; }
  printf "%s" "$T" | $W pages secret put GH_TOKEN --project-name golf-yardage >/dev/null
  printf "%s" "$REPO" | $W pages secret put GH_REPO --project-name golf-yardage >/dev/null
  echo "  토큰 확인·등록 완료"
fi

# ---------- 4b. 홈페이지 즉시 검색용 Anthropic 키 (Cloudflare) ----------
say "4b 홈페이지 찾기를 앱에서 바로 하도록 Anthropic 키 등록"
if $W pages secret list --project-name golf-yardage 2>/dev/null | grep -q ANTHROPIC_API_KEY; then
  echo "  이미 등록됨"
else
  K="${ANTHROPIC_API_KEY:-}"; [ -n "$K" ] || K=$(ask_secret "Anthropic API 키 (sk-ant-api03-…, 3단계와 같은 키)")
  [[ "$K" == sk-ant-* ]] || { echo "  sk-ant- 로 시작하는 키가 아닙니다. 다시 실행하세요."; exit 1; }
  printf "%s" "$K" | $W pages secret put ANTHROPIC_API_KEY --project-name golf-yardage >/dev/null
  echo "  등록 완료"
fi

# ---------- 5. 재배포 + 시험 ----------
say "5/5 앱 재배포 (새 비밀값 적용)"
unset CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID || true
./deploy_token.sh
gh workflow run crawl -R "$REPO" >/dev/null 2>&1 || true
echo
echo "완료. 앱 [구장] → [구장추가] 화면에 '🟢 자동 수집 켜짐'이 보이면 됩니다."
echo "실행 기록: https://github.com/$REPO/actions"
