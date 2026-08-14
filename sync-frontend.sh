#!/usr/bin/env bash
# 两个前端站：
#   客户端  server/static/index.html （唯一来源）→ frontend/index.html （Vercel）
#   管理站  admin/index.html         （唯一来源）→ server/static/console.html （后端 /console，同源）
#                                    admin/ 那份留着，将来想独立托管随时可以

# 两边的后端地址都由 <meta name="hivora-api"> 决定，本脚本负责写进去。
#
#   ./sync-frontend.sh                 同步
#   ./sync-frontend.sh --check         校验是否同步（不一致则退出码 1，CI/hook 用）
#   ./sync-frontend.sh --install-hooks 给三个 repo 装 pre-push 钩子，不同步就拒绝 push
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 真身在 server/ 里（这样才进版本控制），工作区根目录是软链。
# 直接跑 server/xxx.sh 会算错 ROOT —— 与其默默做错事，不如当场停下。
[ -d "$ROOT/server" ] && [ -d "$ROOT/frontend" ] || {
  echo "❌ 请从工作区根目录运行（./sync-frontend.sh），不要直接跑 server/ 里的那份。" >&2
  exit 2
}
SRC="$ROOT/server/static/index.html"
DEST="$ROOT/frontend/index.html"
ADMIN="$ROOT/admin/index.html"
CONSOLE="$ROOT/server/static/console.html"
API_BASE="${HIVORA_API_BASE:-https://hivora-agent-stage.onrender.com}"

stamp() {   # 写入后端地址 + 内容指纹
  python3 "$ROOT/stamp.py" "$1" "$API_BASE"
}

build_id() {   # 某个源文件应有的指纹（部署后拿它跟线上页面比对）
  python3 "$ROOT/stamp.py" "$1" --build-id
}

# 客户端那份必须一行管理代码都没有——管理功能在独立的 admin 站
assert_no_admin() {
  if grep -q "api/admin" "$DEST" 2>/dev/null; then
    echo "❌ frontend/index.html 里出现了管理接口调用。管理功能只能在 admin/ 站。" >&2
    exit 1
  fi
}

case "${1:-}" in
  --check)
    assert_no_admin
    if ! diff -q <(stamp "$ADMIN") "$ADMIN" >/dev/null 2>&1; then
      echo "❌ admin/index.html 的后端地址不是 $API_BASE，跑 ./sync-frontend.sh" >&2
      exit 1
    fi
    if ! diff -q <(python3 "$ROOT/stamp.py" "$ADMIN" "") "$CONSOLE" >/dev/null 2>&1; then
      echo "❌ server/static/console.html 与 admin/index.html 不同步，跑 ./sync-frontend.sh" >&2
      exit 1
    fi
    if diff -q <(stamp "$SRC") "$DEST" >/dev/null 2>&1; then
      echo "✅ frontend/index.html 已同步 · admin/index.html 后端地址正确"
    else
      echo "❌ frontend/index.html 与 server/static/index.html 不同步。" >&2
      echo "   跑一下：./sync-frontend.sh" >&2
      diff <(stamp "$SRC") "$DEST" | head -20 >&2 || true
      exit 1
    fi
    ;;
  --install-hooks)
    for repo in server frontend admin; do
      hook="$ROOT/$repo/.git/hooks/pre-push"
      [ -d "$ROOT/$repo/.git" ] || { echo "跳过 $repo（不是 git repo）"; continue; }
      cat > "$hook" <<EOF
#!/usr/bin/env bash
# 由 sync-frontend.sh --install-hooks 生成
exec "$ROOT/sync-frontend.sh" --check
EOF
      chmod +x "$hook"
      echo "✅ 已装 $repo/.git/hooks/pre-push"
    done
    ;;
  *)
    stamp "$SRC" > "$DEST"
    tmp="$(mktemp)"; stamp "$ADMIN" > "$tmp" && mv "$tmp" "$ADMIN"
    # 后端自带的那份把后端地址留空 → 前端相对路径调用 → 永远是同源，不吃 CORS
    python3 "$ROOT/stamp.py" "$ADMIN" "" > "$CONSOLE"
    assert_no_admin
    echo "✅ server/static → frontend/index.html"
    echo "✅ admin/index.html 后端地址已写入"
    echo "✅ admin → server/static/console.html（同源，后端 /console）"
    echo "   API_BASE=${API_BASE}"
    ;;
esac
