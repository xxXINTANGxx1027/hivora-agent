#!/usr/bin/env bash
# 一条命令走完：跑测试 → 确认要推什么 → 推 → 等部署 → 验证线上真的换版本了。
#
#   ./push.sh              全流程
#   ./push.sh --status     只看状态，什么都不推
#   ./push.sh -y           不问直接推
#   ./push.sh --skip-tests 跳过测试（不建议）
#
# 存在的理由：三个 repo 各自独立，项目根目录**不是** git repo。
# 在根目录敲 git push 只会得到 "not a git repository"，很容易以为推过了。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 真身在 server/ 里（这样才进版本控制），工作区根目录是软链。
# 直接跑 server/xxx.sh 会算错 ROOT —— 与其默默做错事，不如当场停下。
[ -d "$ROOT/server" ] && [ -d "$ROOT/frontend" ] || {
  echo "❌ 请从工作区根目录运行（./push.sh），不要直接跑 server/ 里的那份。" >&2
  exit 2
}
API="${HIVORA_API_BASE:-https://hivora-agent-stage.onrender.com}"
WEB="${HIVORA_WEB_URL:-https://hivora-insurance.vercel.app}"
ADMIN_WEB="${HIVORA_ADMIN_URL:-$API/console}"   # 管理站跟后端同源；独立托管的话改这里
DEPLOY_WAIT="${DEPLOY_WAIT:-420}"          # 等部署的秒数上限

STATUS_ONLY=0; ASSUME_YES=0; SKIP_TESTS=0
for a in "$@"; do
  case "$a" in
    --status) STATUS_ONLY=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    *) echo "不认识的参数：$a" >&2; exit 2 ;;
  esac
done

c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say() { printf '%s\n' "$*"; }
head2() { printf '\n%s──%s %s\n' "$c_dim" "$c_off" "$*"; }

pending() {   # 某个 repo 有几个提交没推
  git -C "$ROOT/$1" rev-list --count origin/main..HEAD 2>/dev/null || echo "?"
}
has_remote() { git -C "$ROOT/$1" remote get-url origin >/dev/null 2>&1; }

# ── 状态 ──────────────────────────────────────────────────────
head2 "本地状态"
ANY=0
for r in server frontend admin; do
  if ! has_remote "$r"; then
    printf '  %-9s %s还没建远端仓库%s\n' "$r" "$c_warn" "$c_off"
    continue
  fi
  git -C "$ROOT/$r" fetch -q origin 2>/dev/null || true
  n="$(pending "$r")"
  dirty="$(git -C "$ROOT/$r" status --porcelain | wc -l | tr -d ' ')"
  [ "$n" != "0" ] && ANY=1
  printf '  %-9s 待推 %-3s 未提交 %-3s %s\n' "$r" "$n" "$dirty" \
    "$(git -C "$ROOT/$r" log --oneline -1 | cut -c1-46)"
done

head2 "线上现状"
printf '  后端  %s\n' "$(curl -s --max-time 90 "$API/healthz" || echo '(连不上)')"
printf '  前端  %s\n' "$(curl -s --max-time 60 "$WEB" | grep -o 'content="[a-f0-9]\{12\}"' | head -1 || echo '(没有构建标记 = 旧版)')"

if [ "$STATUS_ONLY" = 1 ]; then exit 0; fi
if [ "$ANY" = 0 ]; then say "${c_ok}✅ 没有待推的提交${c_off}"; exit 0; fi

# ── 推之前的检查 ──────────────────────────────────────────────
if [ "$SKIP_TESTS" = 0 ]; then
  head2 "测试"
  ( cd "$ROOT/server" && .venv/bin/python -m pytest tests -q 2>&1 | tail -1 ) \
    || { say "${c_err}❌ 测试没过，不推。${c_off}"; exit 1; }
fi
head2 "前端同步检查"
"$ROOT/sync-frontend.sh" --check

head2 "即将推送"
for r in server frontend admin; do
  has_remote "$r" || continue
  n="$(pending "$r")"; [ "$n" = "0" ] && continue
  printf '  %s（%s 个）\n' "$r" "$n"
  git -C "$ROOT/$r" log --oneline origin/main..HEAD | sed 's/^/      /'
done
say ""
say "${c_warn}这会直接部署到生产（Render + Vercel）。${c_off}"
if [ "$ASSUME_YES" = 0 ]; then
  read -r -p "继续？[y/N] " ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { say "取消。"; exit 0; }
fi

# ── 推 ────────────────────────────────────────────────────────
head2 "推送"
SERVER_SHA=""
for r in server frontend admin; do
  has_remote "$r" || { printf '  %-9s 跳过（没有远端）\n' "$r"; continue; }
  n="$(pending "$r")"; [ "$n" = "0" ] && { printf '  %-9s 已是最新\n' "$r"; continue; }
  git -C "$ROOT/$r" push --quiet
  git -C "$ROOT/$r" push --quiet --tags 2>/dev/null || true
  printf '  %-9s %s✅ 推了 %s 个%s\n' "$r" "$c_ok" "$n" "$c_off"
  [ "$r" = "server" ] && SERVER_SHA="$(git -C "$ROOT/server" rev-parse HEAD | cut -c1-12)"
done

# ── 等部署生效 ────────────────────────────────────────────────
WANT_WEB="$("$ROOT/sync-frontend.sh" >/dev/null 2>&1; python3 "$ROOT/stamp.py" "$ROOT/server/static/index.html" --build-id)"

wait_for() {  # 名字 / 期望值 / 取当前值的命令
  local what="$1" want="$2" cmd="$3" got="" i=0
  printf '  %-6s 等 %s …' "$what" "${want:0:12}"
  while [ "$i" -lt "$DEPLOY_WAIT" ]; do
    got="$(eval "$cmd" 2>/dev/null || true)"
    if [ -n "$want" ] && [ "$got" = "$want" ]; then
      printf ' %s✅ %ss%s\n' "$c_ok" "$i" "$c_off"; return 0
    fi
    # 20 秒一次，不是 10 —— 太频繁会被 Vercel 的机器人防护拦成 403 challenge，
    # 表现是「一直拿到空」，看着像没部署，其实页面根本没发给我们。
    sleep 20; i=$((i + 20)); printf '.'
  done
  printf ' %s⏳ 超时，最后拿到 %s%s\n' "$c_warn" "${got:-空}" "$c_off"
  return 1
}

head2 "等部署（最多 $((DEPLOY_WAIT / 60)) 分钟）"
RC=0
if [ -n "$SERVER_SHA" ]; then
  wait_for "后端" "$SERVER_SHA" \
    "curl -s --max-time 90 '$API/healthz' | sed -n 's/.*\"build\":\"\([^\"]*\)\".*/\1/p'" || RC=1
fi
if ! wait_for "前端" "$WANT_WEB" \
  "curl -s --max-time 60 '$WEB' | sed -n 's/.*hivora-build\" content=\"\([^\"]*\)\".*/\1/p' | head -1"; then
  RC=1
  # 区分「没部署」和「被防护拦了」——两者都表现为拿不到指纹，处理方式完全不同
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$WEB")" = "403" ]; then
    printf '  %s↳ 拿到 403：Vercel 把这次校验当机器人拦了，不代表没部署。\n' "$c_warn"
    printf '    用浏览器打开确认，或过几分钟跑 ./push.sh --status 复查。%s\n' "$c_off"
  fi
fi

if [ -n "$ADMIN_WEB" ]; then
  WANT_ADMIN="$(python3 "$ROOT/stamp.py" "$ROOT/admin/index.html" --build-id)"
  wait_for "管理站" "$WANT_ADMIN" \
    "curl -s --max-time 60 '$ADMIN_WEB' | sed -n 's/.*hivora-build\" content=\"\([^\"]*\)\".*/\1/p' | head -1" || RC=1
fi

# ── 验收 ──────────────────────────────────────────────────────
head2 "验收"
printf '  readyz    %s\n' "$(curl -s --max-time 90 "$API/readyz" || echo '(连不上)')"
LEAK="$(curl -s --max-time 60 "$WEB" | grep -c 'api/admin' || true)"
if [ "$LEAK" = "0" ]; then
  printf '  %s✅ 客户端不含管理代码%s\n' "$c_ok" "$c_off"
else
  printf '  %s❌ 客户端里出现了 %s 处管理接口调用%s\n' "$c_err" "$LEAK" "$c_off"; RC=1
fi

say ""
[ "$RC" = 0 ] && say "${c_ok}✅ 全部就绪${c_off}" \
              || say "${c_warn}⚠️  有项目没验过，翻上面看哪一条${c_off}"
exit "$RC"
