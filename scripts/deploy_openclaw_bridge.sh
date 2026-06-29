#!/usr/bin/env bash
# 部署 openclaw-bridge 插件：以 git 仓库为单一真源，拷贝到运行中的 OpenClaw 扩展安装目录并重启 gateway。
#
# 背景：运行中的插件是安装副本（~/.openclaw/extensions/ai4all-openclaw-bridge/），不是仓库本身；
# git pull 不会更新它。历史上靠手动在 dist 上打补丁，导致补丁与源码分叉、OpenClaw 升级一覆盖就静默退化。
# 本脚本让「仓库 -> 安装副本」可复现：每次改完 openclaw-bridge/ 跑一次即可，升级被覆盖后重跑即恢复。
#
# 用法：scripts/deploy_openclaw_bridge.sh [--no-restart] [--dry-run]
#   --no-restart  只同步文件，不重启 gateway（需自行重启才生效）
#   --dry-run     只打印将要做的动作，不改动任何文件
#
# 退出码：0 成功；非 0 表示中止（安装目录不存在/源缺文件/校验失败）。
set -euo pipefail

NO_RESTART=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --no-restart) NO_RESTART=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

# 仓库根 = 本脚本上级目录的上级
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$REPO_DIR/openclaw-bridge"
DST_DIR="${OPENCLAW_BRIDGE_INSTALL_DIR:-$HOME/.openclaw/extensions/ai4all-openclaw-bridge}"
GATEWAY_SERVICE="${OPENCLAW_GATEWAY_SERVICE:-openclaw-gateway.service}"

# 要同步的文件集（与 openclaw-bridge/ 内容保持一致）
FILES=(index.js openclaw.plugin.json package.json)

echo "[deploy] repo   = $REPO_DIR"
echo "[deploy] source = $SRC_DIR"
echo "[deploy] target = $DST_DIR"

# 安全闸：安装目录必须已存在（避免在错误机器上凭空创建假插件）
if [[ ! -d "$DST_DIR" ]]; then
  echo "[deploy] ERROR: 安装目录不存在: $DST_DIR （本机可能没装该扩展）" >&2
  exit 3
fi
for f in "${FILES[@]}"; do
  if [[ ! -f "$SRC_DIR/$f" ]]; then
    echo "[deploy] ERROR: 源文件缺失: $SRC_DIR/$f" >&2
    exit 4
  fi
done

# 备份当前安装副本（仅备份将被覆盖的文件），便于回滚
STAMP="$(date +%Y%m%d-%H%M%S)"
BAK_DIR="$DST_DIR/.backup-$STAMP"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[deploy] (dry-run) 将备份到 $BAK_DIR 并拷贝: ${FILES[*]}"
else
  mkdir -p "$BAK_DIR"
  for f in "${FILES[@]}"; do
    [[ -f "$DST_DIR/$f" ]] && cp -a "$DST_DIR/$f" "$BAK_DIR/$f"
  done
  echo "[deploy] 已备份原文件 -> $BAK_DIR"
  for f in "${FILES[@]}"; do
    cp -a "$SRC_DIR/$f" "$DST_DIR/$f"
    echo "[deploy] 已更新 $f"
  done
fi

# 重启 gateway 让新插件生效
if [[ "$NO_RESTART" -eq 1 ]]; then
  echo "[deploy] --no-restart：跳过 gateway 重启（改动尚未生效，需手动 systemctl --user restart $GATEWAY_SERVICE）"
elif [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[deploy] (dry-run) 将执行: systemctl --user restart $GATEWAY_SERVICE"
else
  echo "[deploy] 重启 $GATEWAY_SERVICE ..."
  systemctl --user restart "$GATEWAY_SERVICE"
  sleep 3
  STATE="$(systemctl --user show "$GATEWAY_SERVICE" -p ActiveState --value 2>/dev/null || echo unknown)"
  echo "[deploy] gateway ActiveState=$STATE"
  [[ "$STATE" == "active" ]] || { echo "[deploy] ERROR: gateway 未 active，请检查日志" >&2; exit 5; }
fi

# 落地校验：确认安装副本已是仓库版（以 requestDump 标记 + 行数为证）
echo "[deploy] 校验安装副本:"
echo "  index.js 行数 = $(wc -l < "$DST_DIR/index.js")（repo = $(wc -l < "$SRC_DIR/index.js")）"
echo "  requestDump 出现次数 = $(grep -c 'requestDump' "$DST_DIR/index.js" 2>/dev/null || echo 0)"
echo "[deploy] 完成。"
