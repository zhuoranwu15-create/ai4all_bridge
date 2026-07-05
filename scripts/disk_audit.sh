#!/usr/bin/env bash
# aliyun1 磁盘占用审计脚本
# 用法：
#   bash disk_audit.sh          # 跑所有「无需 sudo」的检查
#   sudo bash disk_audit.sh     # 额外跑「需要 sudo」的深挖（root-owned 目录/全盘大文件）
# 只读，不删除任何东西。清理动作单独给建议，不在此脚本执行。
set -u

hr(){ printf '\n==== %s ====\n' "$1"; }
have_sudo(){ [ "$(id -u)" = "0" ]; }

echo "# 磁盘审计  host=$(hostname)  time=$(date '+%F %T')"

hr "1. 文件系统总览"
df -hT -x tmpfs -x devtmpfs 2>/dev/null | grep -vE 'overlay|shm'

hr "2. 根文件系统顶层目录占用 (-x 不跨挂载点)"
for d in /home /var /usr /opt /root /tmp /srv /data /app; do
  [ -d "$d" ] && du -xsh "$d" 2>/dev/null
done | sort -rh

hr "3. /var/log 明细 (日志是常见吃盘点)"
du -xh --max-depth=1 /var/log 2>/dev/null | sort -rh | head -12
echo "-- systemd journal 实际占用 --"
journalctl --disk-usage 2>/dev/null || true
journalctl --user --disk-usage 2>/dev/null || true

hr "4. /home 各用户 & /home/jack 明细"
du -xh --max-depth=1 /home 2>/dev/null | sort -rh | head
echo "-- /home/jack --"
du -xh --max-depth=1 /home/jack 2>/dev/null | sort -rh | head -15
echo "-- workspace 明细 (业务代码/依赖) --"
du -xh --max-depth=1 /home/jack/workspace 2>/dev/null | sort -rh | head
echo "-- 各 node_modules 体积 --"
find /home/jack/workspace -maxdepth 3 -type d -name node_modules 2>/dev/null \
  | xargs -I{} du -xsh {} 2>/dev/null | sort -rh | head

hr "5. 可回收的开发/工具缓存 (删了不影响业务)"
for c in ~/.npm ~/.npm-global ~/.cache ~/.codex ~/.claude ~/.local/share/uv ~/.cache/pip; do
  [ -e "$c" ] && du -xsh "$c" 2>/dev/null
done | sort -rh
echo "-- npm cache 可清理量 --"
npm cache verify 2>/dev/null | grep -iE 'content|size' || echo "(npm 不可用或无缓存)"

hr "6. Docker 占用 (镜像/容器/卷)"
if command -v docker >/dev/null 2>&1; then
  docker system df 2>/dev/null || echo "(docker 需权限, 见 sudo 部分)"
else
  echo "(无 docker)"
fi

hr "7. PG 数据目录 (业务真实数据, 作对比基准)"
for p in /var/lib/pgsql/data /var/lib/postgresql; do
  [ -d "$p" ] && du -xsh "$p" 2>/dev/null || true
done
echo "(若上面为空=需 sudo, 见下)"

hr "8. /home/jack/backup 里都是什么 (同盘备份, 建议迁走)"
ls -lah /home/jack/backup 2>/dev/null | head -20
du -xsh /home/jack/backup 2>/dev/null

if have_sudo; then
  hr "S1. [sudo] 全盘 Top-20 大目录"
  du -xh / 2>/dev/null | sort -rh | head -20
  hr "S2. [sudo] 全盘 Top-30 大文件 (>50M)"
  find / -xdev -type f -size +50M 2>/dev/null -printf '%s\t%p\n' \
    | sort -rn | head -30 | awk '{printf "%.0fM\t%s\n",$1/1048576,$2}'
  hr "S3. [sudo] Docker 数据根实际占用"
  du -xsh /var/lib/docker 2>/dev/null
  docker system df -v 2>/dev/null | head -30
  hr "S4. [sudo] PG 数据目录明细"
  du -xsh /var/lib/pgsql/data 2>/dev/null
  du -xh --max-depth=1 /var/lib/pgsql/data 2>/dev/null | sort -rh | head
  hr "S5. [sudo] 系统 journal 目录"
  du -xsh /var/log/journal 2>/dev/null
else
  hr "!! 需要 sudo 的深挖 (请用 sudo 重跑本脚本获取以下信息) !!"
  cat <<'EOF'
以下必须 sudo 才能看全, 请执行:  sudo bash disk_audit.sh
  S1 全盘 Top-20 大目录 (root-owned 的 /usr /var/lib/docker 等)
  S2 全盘 Top-30 大文件 (>50M) —— 定位异常大文件(core dump / 旧日志 / 误留 tar)
  S3 /var/lib/docker 真实占用
  S4 /var/lib/pgsql/data 明细
  S5 /var/log/journal 系统级 journal 占用
EOF
fi

echo
echo "# 审计完成。清理动作请人工确认后再执行(本脚本只读不删)。"
