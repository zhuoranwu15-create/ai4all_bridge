# Runtime Health Checklist

更新时间：2026-06-04

用途：在开发机或线上机快速确认 Backend、Scheduler、OpenClaw 和基础配置是否处于可工作状态。不要在输出、日志或飞书群里粘贴用户正文、API key、Authorization header 或 webhook。

## 1. 本地开发机

### 1.1 确认配置

```bash
.venv/bin/python -c "from app.config import settings; print('APP_ENV=', settings.app_env); print('DATABASE_URL_CONFIGURED=', bool(settings.database_url)); print('PROACTIVE_SCHEDULER_ENABLED=', settings.proactive_scheduler_enabled); print('DREAMING_SCHEDULER_ENABLED=', settings.dreaming_scheduler_enabled)"
```

当前默认：

- `PROACTIVE_SCHEDULER_ENABLED=false`
- `DREAMING_SCHEDULER_ENABLED=false`

这表示 FastAPI 启动时不会自动跑 scheduler。

### 1.2 启动 Backend

```bash
.venv/bin/uvicorn app.main:app --reload --port 8180
```

### 1.3 检查 Backend readiness

```bash
curl -s http://127.0.0.1:8180/health
curl -s http://127.0.0.1:8180/health/ready
```

预期：

- `/health` 返回 `status=ok`
- `/health/ready` 返回 `status=ok`
- `checks.db`、`checks.user_profiles_dir`、`checks.system_dir`、`checks.runtime_config` 都是 `ok`

### 1.4 检查 scheduler 状态

```bash
curl -s -H "Authorization: Bearer dev-admin-token" \
  http://127.0.0.1:8180/admin/proactive/scheduler

curl -s -H "Authorization: Bearer dev-admin-token" \
  http://127.0.0.1:8180/admin/dreaming/scheduler
```

如果配置里 scheduler 没启用，返回里的 `enabled=false` 是正常的。

手动补跑一次 Dreaming scan：

```bash
curl -s -X POST -H "Authorization: Bearer dev-admin-token" \
  "http://127.0.0.1:8180/admin/dreaming/scheduler/run-once?limit=100"
```

手动跑一次 proactive scheduler：

```bash
curl -s -X POST -H "Authorization: Bearer dev-admin-token" \
  "http://127.0.0.1:8180/admin/proactive/scheduler/run-once?limit=20"
```

### 1.5 使用统一 monitor 脚本

只检查 Backend ready：

```bash
.venv/bin/python scripts/monitor_health.py --dry-run
```

如果某个 scheduler 已启用并且应该持续运行，再加 heartbeat 检查：

```bash
MONITOR_SCHEDULERS=proactive_scheduler:90,dreaming_scheduler:900 \
  .venv/bin/python scripts/monitor_health.py --dry-run
```

如果 scheduler 暂未启用，不要把它放进 `MONITOR_SCHEDULERS`，否则会因为 heartbeat missing 报错。

## 2. 线上机

### 2.1 Backend

```bash
systemctl status ai4all-weixin-backend --no-pager -n 50
curl -s http://127.0.0.1:8180/health
curl -s http://127.0.0.1:8180/health/ready
journalctl -u ai4all-weixin-backend -n 100 --no-pager
```

线上 `/health/ready` 会额外检查关键配置：

- `LLM_API_KEY`
- `AI4ALL_BRIDGE_SECRET` 不能是默认值
- `ADMIN_TOKEN` 不能是默认值

### 2.2 Scheduler

Proactive scheduler 当前推荐独立 systemd 进程：

```bash
systemctl status ai4all-weixin-proactive-scheduler --no-pager -n 50
MONITOR_SCHEDULERS=proactive_scheduler:90 \
  .venv/bin/python scripts/monitor_health.py --dry-run
```

Dreaming scheduler 目前是 FastAPI in-process scheduler。只有在单 worker 部署并设置 `DREAMING_SCHEDULER_ENABLED=true` 时才应启用；启用后可监控：

```bash
MONITOR_SCHEDULERS=dreaming_scheduler:900 \
  .venv/bin/python scripts/monitor_health.py --dry-run
```

不要同时运行多个 scheduler 实例；当前没有跨进程 leader election。

### 2.3 OpenClaw

```bash
openclaw channels status --probe
openclaw channels list
openclaw gateway status
```

也可以让 monitor 脚本检查 OpenClaw：

```bash
MONITOR_CHECK_OPENCLAW=true \
MONITOR_OPENCLAW_CHANNEL=openclaw-weixin \
  .venv/bin/python scripts/monitor_health.py --dry-run
```

### 2.4 定时监控

线上推荐用 systemd timer 或 cron 每分钟运行：

```bash
MONITOR_SCHEDULERS=proactive_scheduler:90 \
  .venv/bin/python scripts/monitor_health.py
```

如果确认 Dreaming scheduler 已启用，再改为：

```bash
MONITOR_SCHEDULERS=proactive_scheduler:90,dreaming_scheduler:900 \
  .venv/bin/python scripts/monitor_health.py
```

配置了 `FEISHU_ALERT_WEBHOOK_URL` 后，连续失败会发飞书告警。

## 3. 快速判定

- Backend 不通：先看 `/health/ready` 和 backend journal。
- Ready 失败：优先修 DB、目录权限、`.env` 关键配置。
- Scheduler heartbeat missing：确认该 scheduler 是否本来就启用；未启用则不要监控它。
- Scheduler heartbeat stale：检查进程是否退出、是否卡在 LLM/OpenClaw/DB 调用。
- OpenClaw 异常：先查 channel/gateway，再查 bridge webhook 和 `/openclaw/turn` 日志。
