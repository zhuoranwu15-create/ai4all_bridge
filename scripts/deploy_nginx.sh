#!/usr/bin/env bash
# 部署 nginx 反代配置：以 git 仓库 deploy/nginx/ 为单一真源，安装到 /etc/nginx/conf.d/ 并 reload。
#
# 背景：nginx vhost 此前手工维护、不在 git，改动易丢失/漂移（与 openclaw-bridge 同类隐患）。
# 本脚本让「仓库 -> /etc/nginx/conf.d」可复现：改完 deploy/nginx/*.conf 跑一次即可。
#
# 需要 root 权限写 /etc/nginx 并 reload：脚本内部用 sudo，运行时可能提示输入密码。
#
# 用法：scripts/deploy_nginx.sh [--dry-run]
#   --dry-run  只 diff 与预检，不写入、不 reload
#
# 注意：basic auth 的 .ai4all_ops.htpasswd 是机密，不在 git；首次部署见 deploy/nginx/README.md。
set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_DIR/deploy/nginx"
DST_DIR="/etc/nginx/conf.d"

# 要同步的配置文件（仅 ai4all 自有 vhost，不碰 governor-game 等他人配置）
FILES=(ai4company.top.conf ai4all-node.conf)

echo "[nginx-deploy] source = $SRC_DIR"
echo "[nginx-deploy] target = $DST_DIR"

for f in "${FILES[@]}"; do
  [[ -f "$SRC_DIR/$f" ]] || { echo "[nginx-deploy] ERROR: 源缺失 $SRC_DIR/$f" >&2; exit 4; }
done

# 先展示与线上的差异，便于核对
echo "[nginx-deploy] 与线上差异："
for f in "${FILES[@]}"; do
  if [[ -f "$DST_DIR/$f" ]]; then
    if diff -q "$DST_DIR/$f" "$SRC_DIR/$f" >/dev/null; then
      echo "  $f: 无差异"
    else
      echo "  $f: 有差异 ↓"
      diff "$DST_DIR/$f" "$SRC_DIR/$f" | sed 's/^/    /' || true
    fi
  else
    echo "  $f: 线上不存在（将新增）"
  fi
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[nginx-deploy] (dry-run) 预检线上当前配置：sudo nginx -t"
  sudo nginx -t || true
  echo "[nginx-deploy] (dry-run) 结束，未写入。"
  exit 0
fi

# 备份当前线上配置（带时间戳）
STAMP="$(date +%Y%m%d-%H%M%S)"
for f in "${FILES[@]}"; do
  [[ -f "$DST_DIR/$f" ]] && sudo cp -a "$DST_DIR/$f" "$DST_DIR/$f.bak.$STAMP"
done
echo "[nginx-deploy] 已备份线上原文件 -> $DST_DIR/*.bak.$STAMP"

# 写入
for f in "${FILES[@]}"; do
  sudo cp "$SRC_DIR/$f" "$DST_DIR/$f"
  echo "[nginx-deploy] 已更新 $f"
done

# 语法预检：失败则回滚
if ! sudo nginx -t; then
  echo "[nginx-deploy] ERROR: nginx -t 失败，回滚到备份" >&2
  for f in "${FILES[@]}"; do
    [[ -f "$DST_DIR/$f.bak.$STAMP" ]] && sudo cp -a "$DST_DIR/$f.bak.$STAMP" "$DST_DIR/$f"
  done
  exit 5
fi

sudo systemctl reload nginx
echo "[nginx-deploy] nginx 已 reload，完成。"
