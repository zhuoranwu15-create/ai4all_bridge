# 生产稳定性 Runbook

更新时间：2026-06-02

适用范围：阿里云单机或少量 ECS 外测环境，FastAPI Backend、SQLite、OpenClaw Gateway、独立 proactive scheduler。

## 基础信息

- Backend service：`ai4all-weixin-backend.service`
- Proactive scheduler：`ai4all-weixin-proactive-scheduler.service`
- Health monitor timer：`ai4all-monitor-health.timer`
- Backend 本机地址：`http://127.0.0.1:8180`
- 标准数据库：`data/ai4all.sqlite3`
- Admin 状态页：`/ui/ops.html`

不要在日志、文档或飞书群中粘贴用户聊天正文、完整 webhook、API key、验证码或 Authorization header。

## 规模化运维红线（必读）

> 背景：当前形态是「N 个登录态个人微信号挂在单台机器的单个 OpenClaw daemon、单一出口 IP 上」。账号数越多，以下三条违规的代价越大——可能直接触发微信侧风控（同 IP 账号聚集异常、集中上线）。详见 [`docs/tech_design/single-host-multi-openclaw-scale.md`](../tech_design/single-host-multi-openclaw-scale.md)。

1. **禁止把全局 `openclaw gateway restart` 当日常操作。**
   - 一次全局重启 = 该 daemon 上**全部**微信账号在同一秒、从同一 IP 重新拉起 iLink 长轮询，是教科书级风控触发点。
   - 必须重启时：**避开活跃时段、分批错峰**；能用单账号粒度（`openclaw channels logout` / 单账号重登）就**不要**全局重启。
   - `scripts/restart_runtime.sh` **默认不重启 OpenClaw**；只有显式 `--restart-openclaw` 才会，且会二次确认（见下）。

2. **出口 IP 稳定绑定。** 同一账号长期固定从同一出口 IP 出去，**不要频繁换 IP**——频繁换 IP 本身是风控信号。

3. **登录/重登错峰。** 批量上号或批量重登时分批、错峰，不要同一 IP 在短时间集中上线大量账号（onboarding 高峰、故障恢复重连都适用）。

> 补充：单 OpenClaw daemon 是单点，宁可多实例、每实例少挂账号，以控制爆炸半径（重构方向见上述设计文档，此处仅运维纪律）。

## Backend 不可用

确认：

```bash
systemctl status ai4all-weixin-backend --no-pager -n 50
curl -s http://127.0.0.1:8180/health
curl -s http://127.0.0.1:8180/health/ready
journalctl -u ai4all-weixin-backend -n 100 --no-pager
```

处理：

- 如果进程退出：`sudo systemctl restart ai4all-weixin-backend`。
- 如果 `/health/live` 正常但 `/health/ready` 失败：按返回的 `checks` 修复 DB、目录权限或 `.env`。
- 如果 nginx 公网不可达但本机 health 正常：检查 nginx、证书、DNS 和安全组。

## OpenClaw Disconnected

确认：

```bash
openclaw channels status --probe
openclaw channels list
openclaw gateway status
```

处理：

- Gateway 不可达：检查 OpenClaw gateway 进程或用户级 systemd 服务。
- `openclaw-weixin` 未启用：检查 OpenClaw 配置和 channel 安装状态。
- 微信账号掉线：重新走 Web onboarding QR 绑定或 OpenClaw login 流程。
- Backend 正常但消息不到达：检查 OpenClaw bridge webhook URL、`AI4ALL_BRIDGE_SECRET` 和 `/openclaw/turn` 日志。

## LLM 大面积失败

确认：

```bash
journalctl -u ai4all-weixin-backend -n 200 --no-pager
curl -s http://127.0.0.1:8180/health/ready
```

处理：

- 检查 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 是否正确。
- 检查供应商状态、额度和网络连通性。
- 如果失败由最近发布引入，先回滚代码，再重启 backend。

## 微信收不到回复

确认链路：

```bash
journalctl -u ai4all-weixin-backend -n 200 --no-pager
openclaw channels status --probe
```

处理：

- 入站没有日志：优先查 OpenClaw bridge 到 backend 的 POST。
- 入站有日志但 outbound 失败：查 `openclaw.send_failed` 或 outbound message 错误。
- 只有单账号异常：在 Admin 状态页查账号状态、绑定状态、限流和最近错误；所有查询必须带 `account_id` 维度。

## Proactive Scheduler 停止

确认：

```bash
systemctl status ai4all-weixin-proactive-scheduler --no-pager -n 50
MONITOR_SCHEDULERS=proactive_scheduler:90 .venv/bin/python scripts/monitor_health.py --dry-run
```

处理：

- 进程退出：`sudo systemctl restart ai4all-weixin-proactive-scheduler`。
- heartbeat stale：确认 scheduler 没有卡在 LLM、OpenClaw 或 DB 调用。
- 不要同时开启多个 proactive scheduler，也不要在多 worker FastAPI 内启用 `PROACTIVE_SCHEDULER_ENABLED=true`。

## 磁盘或 SQLite 风险

确认：

```bash
df -h
df -i
du -sh data data/backups logs 2>/dev/null
sqlite3 data/ai4all.sqlite3 "PRAGMA integrity_check;"
```

处理：

- 磁盘超过 80%：清理旧日志、旧备份或扩容。
- 磁盘超过 90%：先停止大写入任务，释放空间后再恢复服务。
- SQLite integrity check 非 `ok`：停止写入，保留现场，使用最近备份恢复到临时库验证。

## 备份与恢复

备份：

```bash
mkdir -p data/backups
sqlite3 data/ai4all.sqlite3 ".backup 'data/backups/ai4all_$(date +%Y%m%d_%H%M%S).sqlite3'"
tar -czf "data/backups/user_profiles_$(date +%Y%m%d_%H%M%S).tar.gz" data/user_profiles data/system
```

恢复前先停服务：

```bash
sudo systemctl stop ai4all-weixin-proactive-scheduler
sudo systemctl stop ai4all-weixin-backend
```

恢复后启动：

```bash
sudo systemctl start ai4all-weixin-backend
sudo systemctl start ai4all-weixin-proactive-scheduler
curl -s http://127.0.0.1:8180/health/ready
```

## 临时停用账号

在 Admin 页面或 Admin API 将账号状态设为 disabled。停用后验证该账号入站消息会被丢弃或收到预期提示，不影响其他 `account_id`。

## 拉取代码后重启运行态

拉取最新代码后，用脚本统一重启 backend、独立 proactive scheduler、reload nginx，并验证 health 和 scheduler heartbeat：

```bash
scripts/restart_runtime.sh
```

如果 `requirements.txt` 有变更，先让脚本安装依赖：

```bash
scripts/restart_runtime.sh --install-deps
```

如果本次也改了 OpenClaw bridge 插件，先按部署文档重新安装插件，再追加重启 gateway：

```bash
scripts/restart_runtime.sh --restart-openclaw
```

> ⚠️ `--restart-openclaw` 会重启整个 gateway = 该机所有微信账号同时重连，属「规模化运维红线」第 1 条的高危操作。脚本会要求二次确认；务必避开活跃时段，能单账号重登就不要全局重启。

脚本会使用 `sudo` 执行 `nginx -t`、`systemctl restart/reload` 和 monitor timer 启用；需要当前用户具备 sudo 权限。

## OpenClaw / openclaw-weixin 插件升级后（必查补丁）

`@tencent-weixin/openclaw-weixin` 升级或重装会覆盖 `node_modules`，**两个本地热补丁会静默丢失**：

- QR 登录补丁（`gatewayMethods`）丢失 → Web 扫码绑定报 `web login provider is not available`。
- 解绑登出补丁（`logoutAccount`）丢失 → **解绑只清 AI4ALL 侧，微信账号文件 + 索引残留，孤儿 bot 仍在线收消息**。

升级后立即校验并按需重打：

```bash
PLUGIN=~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin
grep -c gatewayMethods $PLUGIN/dist/src/channel.js   # 0 → 重打 QR 补丁
grep -c logoutAccount  $PLUGIN/dist/src/channel.js   # 0 → 重打解绑登出补丁
# 整库备份 → 打补丁 → node --check → 重启（完整步骤见部署文档对应节）
tar czf ~/.openclaw/_plugin_bak_$(date +%s).tgz -C ~/.openclaw openclaw-weixin
( cd $PLUGIN && patch -p1 < /opt/ai4all-weixin-bot/patches/openclaw-weixin-logout-account-runtime.patch )
node --check $PLUGIN/dist/src/channel.js && systemctl --user restart openclaw-gateway.service
```

> ⚠️ **切勿用 workspace `openclaw-weixin`（v2.4.3）build 覆盖线上 dist**：腾讯只发布 2.4.4 npm 产物、未推源码，dist 比 v2.4.3 源码多 20 个模块，覆盖会大规模回退。
> 完整原理与回滚：[解绑登出补丁](../tech_design/openclaw_weixin_gateway_logout_patch.md)、[补丁维护总表](../tech_design/openclaw_patches_maintenance.md)。

## 回滚最近版本

```bash
git log --oneline -n 5
git checkout <known-good-commit>
scripts/restart_runtime.sh
```

如需回滚数据库，必须先备份当前现场，再按“备份与恢复”执行。
