#!/usr/bin/env bash
# 从中心 PG(aliyun1) 拉取自定义格式逻辑备份到本机(aliyun2)。
# 异地副本 + 可 pg_restore 恢复。仅读，不改主库。
#
# 用法:  bash scripts/pg_backup_pull.sh
# 依赖:  postgresql 客户端(pg_dump/pg_restore) 与主库大版本一致(当前 13)
# 连接:  从仓库 .env 的 DATABASE_URL 解析(host/port/db/user/pass)
# 输出:  $BACKUP_DIR/ai4all_<YYYYmmdd_HHMM>.dump  (默认 ~/pgbackups)
# 轮转:  保留最近 $KEEP 份(默认 14)
# 退出码: 0 成功; 非0 失败(供 cron/监控捕获)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/pgbackups}"
KEEP="${KEEP:-14}"
LOG="$BACKUP_DIR/backup.log"
PY="$REPO_DIR/.venv/bin/python"

mkdir -p "$BACKUP_DIR"
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG" >&2; }

# 从 DATABASE_URL 解析各字段(处理 URL 编码), 不落盘不进 ps
eval "$("$PY" - "$ENV_FILE" <<'PYEOF'
import sys
from urllib.parse import urlparse, unquote
url=None
for line in open(sys.argv[1]):
    if line.startswith('DATABASE_URL='):
        url=line.split('=',1)[1].strip()
if not url:
    print("echo NO_DATABASE_URL; exit 3"); sys.exit()
p=urlparse(url)
def q(s): return "'"+(s or '').replace("'","'\\''")+"'"
print(f"DB_HOST={q(p.hostname)}")
print(f"DB_PORT={q(str(p.port or 5432))}")
print(f"DB_NAME={q(p.path.lstrip('/'))}")
print(f"DB_USER={q(unquote(p.username or ''))}")
print(f"DB_PASS={q(unquote(p.password or ''))}")
PYEOF
)"

if [ -z "${DB_HOST:-}" ]; then log "解析 PostgreSQL DATABASE_URL 失败"; exit 3; fi

STAMP="$(date '+%Y%m%d_%H%M')"
OUT="$BACKUP_DIR/ai4all_${STAMP}.dump"

log "开始 pg_dump host=$DB_HOST db=$DB_NAME user=$DB_USER -> $OUT"
if ! PGPASSWORD="$DB_PASS" pg_dump -Fc -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
        -d "$DB_NAME" --no-owner --no-privileges -f "$OUT" 2>>"$LOG"; then
    log "ERROR pg_dump 失败"; rm -f "$OUT"; exit 1
fi

# 校验: pg_restore --list 能读且对象数>0
OBJS="$(PGPASSWORD="$DB_PASS" pg_restore --list "$OUT" 2>>"$LOG" | grep -cvE '^;|^$' || true)"
SIZE="$(du -h "$OUT" | cut -f1)"
if [ "${OBJS:-0}" -lt 1 ]; then log "ERROR 校验失败: 备份对象数=0"; exit 2; fi
log "OK 备份完成 size=$SIZE objects=$OBJS"

# 轮转: 保留最近 KEEP 份
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/ai4all_*.dump 2>/dev/null | tail -n +$((KEEP+1)))
if [ "${#OLD[@]}" -gt 0 ]; then
    log "轮转: 删除 ${#OLD[@]} 份旧备份(保留最近 $KEEP)"
    rm -f "${OLD[@]}"
fi

log "DONE"
