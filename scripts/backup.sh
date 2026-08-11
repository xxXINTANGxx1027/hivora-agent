#!/usr/bin/env bash
# 备份生产数据库到本地文件。
#
#   DATABASE_URL=postgresql://... ./scripts/backup.sh [输出目录]
#
# 默认写到 ./backups/hivora-YYYY-MM-DD-HHMM.sql.gz，并保留最近 14 份。
# 恢复方法见 RUNBOOK.md。
set -euo pipefail

OUT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/backups}"
KEEP="${BACKUP_KEEP:-14}"

if [ -z "${DATABASE_URL:-}" ]; then
  echo "❌ 没有 DATABASE_URL。备份的是生产库，必须显式给。" >&2
  exit 1
fi
if ! command -v pg_dump >/dev/null; then
  echo "❌ 找不到 pg_dump。macOS: brew install libpq && brew link --force libpq" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
STAMP="$(date +%Y-%m-%d-%H%M)"
FILE="$OUT_DIR/hivora-$STAMP.sql.gz"

echo "→ 正在备份到 $FILE"
pg_dump --no-owner --no-privileges "$DATABASE_URL" | gzip -9 > "$FILE"

SIZE=$(wc -c < "$FILE" | tr -d ' ')
if [ "$SIZE" -lt 1024 ]; then
  echo "❌ 备份文件只有 ${SIZE} 字节，几乎肯定是失败了。" >&2
  rm -f "$FILE"; exit 1
fi

# 验证：能否解压并看到关键表。备份不验证等于没备份。
if ! gzip -dc "$FILE" | grep -qE 'CREATE TABLE (public\.)?agents'; then
  echo "❌ 备份里找不到 agents 表，内容不对。" >&2
  exit 1
fi

echo "✅ 完成：$FILE（$(du -h "$FILE" | cut -f1)）"

# 清理旧备份
ls -1t "$OUT_DIR"/hivora-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  echo "  清理旧备份 $(basename "$old")"
  rm -f "$old"
done
