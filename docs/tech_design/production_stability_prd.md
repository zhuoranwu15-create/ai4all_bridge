# 线上稳定性建设技术 PRD

更新时间：2026-06-02

阶段：阿里云小规模外测前

目标环境：单台或少量 ECS、SQLite、FastAPI、OpenClaw Gateway、独立 proactive / dreaming scheduler 进程

## 1. 背景

AI4ALL 微信 Bot 当前已具备基础日志、`/health`、部署检查和部分 Admin/debug 能力，但线上报警、指标、链路观测仍基本空白。

本阶段不是大规模商业化发布，而是从个人测试进入少量外部用户测试。系统需要具备最小但有效的稳定性能力：

- 服务挂了能知道。
- 微信消息收不到或发不出去能知道。
- LLM 大面积失败能知道。
- 主动消息调度器停了能知道。
- 关键指标能看趋势，而不是只靠用户反馈。
- 出问题后能通过 trace 快速定位，但默认不暴露用户聊天正文。

## 2. 目标

### 2.1 产品目标

保障小规模外测期间的基础服务连续性，让团队能在用户明显受影响前发现核心异常，并能基于指标判断测试质量。

### 2.2 技术目标

- 建立轻量级健康检查、关键指标、错误日志和报警机制。
- 覆盖微信入站、后端处理、LLM 调用、OpenClaw 出站、主动调度、数据库和磁盘资源。
- 支持按 `account_id` 维度定位问题，但日志和指标默认不记录聊天正文。
- 不引入重型 Prometheus / Grafana 集群；优先使用阿里云云监控、systemd、结构化日志、轻量脚本和一个 Admin 状态页。

## 3. 非目标

本阶段不做：

- 全链路 APM 深度接入。
- 分布式 tracing 平台。
- 大规模用户增长监控体系。
- 复杂 BI 仪表盘。
- 多地域容灾。
- 高可用 SQLite 架构改造。
- 默认开放明文聊天日志查询。

## 4. 当前现状

代码层面已有：

- `/health`：只返回 `{"status": "ok", "env": ...}`，偏浅层。
- `logging.basicConfig(...)`：普通文本日志，无统一结构化字段。
- 多处 logger：`turn_service`、`llm`、`dreaming`、`scheduler`、`openclaw_gateway` 等。
- Admin/debug API：可查部分会话、消息、主动任务和隐私访问。
- 部署文档中有手动检查项：pytest、health、OpenClaw status、真实冒烟。
- SQLite 标准库路径：`data/ai4all.sqlite3`。
- proactive scheduler / dreaming scheduler 是独立进程，存在进程级可用性风险。

主要缺口：

- 没有 ready/deep health。
- 没有统一 trace id / request id。
- 没有稳定性指标出口。
- 没有自动报警。
- 没有错误聚合或告警阈值。
- 没有 scheduler 心跳。
- 没有关键链路成功率统计。
- 没有线上 runbook。

## 5. 核心原则

1. 轻量优先：先用 `/healthz`、日志、脚本、阿里云云监控和 Admin 状态页解决 80% 问题。
2. 用户隐私优先：指标、报警、日志默认不包含用户消息正文、模型回复正文、raw payload 正文、密钥或验证码。
3. 账号隔离优先：所有可定位指标允许带 `account_id` 或脱敏后的 account key，但查询和聚合必须遵守账号隔离。
4. 可操作优先：每个报警必须对应明确处理动作，例如重启服务、检查 OpenClaw、查看 LLM provider、检查磁盘。

## 6. 指标体系

### 6.1 服务可用性指标

| 指标 | 含义 | 阈值建议 |
|---|---|---|
| `backend_process_up` | FastAPI 进程是否存活 | 0 立即报警 |
| `backend_health_ok` | `/health` 是否成功 | 连续 2 次失败报警 |
| `backend_ready_ok` | DB、配置、关键目录是否可用 | 连续 2 次失败报警 |
| `openclaw_gateway_up` | OpenClaw Gateway 是否可用 | 连续 2 次失败报警 |
| `scheduler_up` | proactive scheduler 进程是否存活 | 1 个周期未心跳报警 |
| `dreaming_scheduler_up` | dreaming scheduler 是否存活 | 2 个周期未心跳报警 |

### 6.2 用户消息链路指标

核心链路：

```text
微信消息
-> OpenClaw bridge
-> POST /openclaw/turn
-> account/session/message 写入
-> prompt build
-> LLM call
-> memory / reminder / tools
-> reply
-> OpenClaw outbound
-> 微信用户收到
```

建议指标：

| 指标 | 含义 |
|---|---|
| `inbound_turn_total` | 入站消息总数 |
| `inbound_turn_error_total` | 入站处理失败数 |
| `turn_success_rate_5m` | 5 分钟消息处理成功率 |
| `turn_latency_p50` / `turn_latency_p95` | 后端单轮处理耗时 |
| `llm_call_total` | LLM 调用次数 |
| `llm_error_total` | LLM 失败次数 |
| `llm_latency_p95` | LLM 耗时 |
| `outbound_send_total` | 出站发送次数 |
| `outbound_send_error_total` | 出站失败次数 |
| `rate_limited_total` | 限流次数 |
| `account_disabled_drop_total` | 禁用账号丢弃次数 |

### 6.3 微信 / OpenClaw 指标

| 指标 | 含义 |
|---|---|
| `bridge_post_error_total` | bridge 调后端失败次数 |
| `openclaw_channel_connected` | 微信 channel 是否连接 |
| `openclaw_send_error_total` | 发送失败次数 |
| `qr_login_start_error_total` | 二维码登录启动失败 |
| `qr_login_wait_error_total` | 二维码等待/绑定失败 |
| `binding_success_total` | 绑定成功数 |
| `binding_failed_total` | 绑定失败数 |

### 6.4 主动消息指标

| 指标 | 含义 |
|---|---|
| `proactive_due_total` | 到期主动任务数 |
| `proactive_sent_total` | 成功发送数 |
| `proactive_error_total` | 失败数 |
| `proactive_skipped_quiet_hours_total` | 静默时间跳过数 |
| `proactive_daily_limit_hit_total` | 每日限制命中数 |
| `scheduler_last_heartbeat_at` | 调度器最后心跳时间 |

### 6.5 资源指标

由阿里云云监控覆盖：

- CPU 使用率。
- 内存使用率。
- 磁盘使用率。
- 磁盘 inode。
- ECS 网络流量。
- 进程存活。
- 端口存活。
- SQLite 文件大小。
- `data/backups` 是否持续增长。

## 7. 健康检查设计

### 7.1 保留 `/health`

用于负载探测和简单存活检查。

返回：

```json
{
  "status": "ok",
  "env": "production"
}
```

### 7.2 新增 `/health/live`

只检查进程活着，不访问 DB 或外部依赖。

用途：

- systemd / nginx / 云监控探活。
- 避免因为 DB 短暂抖动导致进程被误判死亡。

### 7.3 新增 `/health/ready`

检查服务是否能处理真实请求。

建议检查：

- SQLite 可连接。
- `data/user_profiles` 可读写。
- `data/system` 可读。
- 关键配置存在：`LLM_API_KEY`、`AI4ALL_BRIDGE_SECRET`、`ADMIN_TOKEN`。
- OpenClaw Gateway 基础状态可选检查，不建议每次 ready 都执行重命令，可通过缓存结果避免拖慢。

示例返回：

```json
{
  "status": "ok",
  "checks": {
    "db": "ok",
    "user_profiles_dir": "ok",
    "system_dir": "ok",
    "llm_config": "ok",
    "openclaw_cached": "ok"
  },
  "checked_at": "2026-06-02T10:00:00+08:00"
}
```

## 8. 报警设计

### 8.1 报警通道

本阶段建议：

1. 阿里云云监控：ECS、端口、CPU、内存、磁盘。
2. 轻量脚本：定时检查 `/health/ready`、OpenClaw、scheduler 心跳。
3. 飞书报警群机器人：接收 P0/P1/P2 告警。
4. 可选：阿里云日志服务 SLS，用于日志检索，不作为第一阶段强依赖。

### 8.2 是否需要新建飞书报警群

建议新建一个专门的飞书报警接收群。

原因：

- 报警和日常沟通分离，避免重要告警被聊天淹没。
- 后续可以把机器人 webhook、值班人、故障处理记录固定在一个群里。
- 方便按报警等级设置通知策略，例如 P0/P1 强提醒，P2 只聚合推送。
- 外测阶段问题会比较杂，专群能沉淀排障上下文和复盘材料。

群建议命名：`AI4ALL 外测报警与运维`

建议成员：

- 后端研发。
- 产品/运营负责人。
- 有服务器权限的人。
- 需要接收用户反馈并参与判断优先级的人。

群机器人建议配置：

- 使用飞书自定义机器人 webhook。
- webhook 地址写入 `.env` 或服务器环境变量，变量名建议为 `FEISHU_ALERT_WEBHOOK_URL`，不提交仓库。
- 当前外测报警群机器人已创建，hook id 脱敏记录为 `b69bc052-b2a7-4071-a767-************`。
- 报警消息必须包含：级别、服务、环境、摘要、发生时间、检测项、建议动作、相关 trace 或日志定位信息。

### 8.3 报警分级

| 等级 | 定义 | 示例 |
|---|---|---|
| P0 | 核心服务不可用 | backend down、OpenClaw 全断、磁盘满 |
| P1 | 核心链路严重退化 | LLM 连续失败、出站发送失败率高 |
| P2 | 可用但需要处理 | 绑定失败率高、scheduler 延迟、磁盘接近阈值 |
| P3 | 观察项 | 某账号频繁限流、单用户异常失败 |

### 8.4 建议阈值

| 报警 | 阈值 |
|---|---|
| 后端端口不可访问 | 连续 2 次，间隔 1 分钟 |
| `/health/ready` 失败 | 连续 2 次 |
| OpenClaw channel disconnected | 连续 2 次 |
| LLM error rate | 5 分钟内失败率 > 30%，且请求数 >= 5 |
| 出站发送失败率 | 5 分钟内失败率 > 20%，且发送数 >= 5 |
| proactive scheduler 心跳丢失 | 超过 `interval * 3` 未更新 |
| CPU | 5 分钟平均 > 85% |
| 内存 | 5 分钟平均 > 85% |
| 磁盘 | > 80% P2，> 90% P1 |
| SQLite 备份失败 | 当日未生成备份 P1 |

## 9. 日志设计

### 9.1 结构化日志字段

建议统一输出 JSON log 或至少统一 key-value 文本。

必备字段：

- `ts`
- `level`
- `service`
- `event`
- `trace_id`
- `account_id`
- `session_id`
- `message_id`
- `route`
- `duration_ms`
- `status`
- `error_code`
- `error_class`

禁止字段：

- 用户消息正文。
- 模型回复正文。
- OTP。
- API key。
- Authorization header。
- raw payload 正文。
- 微信敏感身份字段，除非已脱敏或是排障必需字段。

### 9.2 核心事件

建议补齐以下事件：

- `turn.received`
- `turn.routed`
- `turn.llm_started`
- `turn.llm_completed`
- `turn.reply_sent`
- `turn.failed`
- `openclaw.send_failed`
- `binding.qr_start_failed`
- `binding.qr_wait_failed`
- `scheduler.heartbeat`
- `scheduler.job_failed`
- `proactive.sent`
- `proactive.failed`
- `memory.write_failed`
- `admin.plaintext_access`

## 10. 指标落地方案

本阶段推荐两层实现。

### 10.1 v0.1：轻量实现

新增一个本地状态文件或 SQLite 表记录运行状态：

- `runtime_health`
- `runtime_metric_events`
- `scheduler_heartbeats`

通过脚本定时聚合近 5 分钟、1 小时、24 小时指标，并输出到：

- Admin 状态页。
- 日志。
- 报警脚本。

优点：

- 不引入新服务。
- 和当前 SQLite 架构兼容。
- 适合小规模外测。

### 10.2 v0.2：可选增强

当外测人数上升后再考虑：

- 阿里云 SLS 日志采集。
- Prometheus exporter。
- Grafana dashboard。
- Sentry 或等价错误聚合。

## 11. Admin 状态页需求

新增或扩展 Admin 页面，提供“线上状态”视图。

最小展示：

- 后端状态。
- OpenClaw 状态。
- scheduler 状态。
- 最近 5 分钟 / 1 小时入站消息数。
- 成功率。
- LLM 失败数。
- 出站失败数。
- 主动消息成功/失败数。
- 最近 20 条错误事件。
- 最近一次备份时间。
- 磁盘使用率。

页面默认只展示元数据，不展示聊天正文。

## 12. Runbook 需求

新增 `docs/ops/production_runbook.md`。

至少覆盖：

- 后端不可用怎么办。
- OpenClaw disconnected 怎么办。
- LLM 大面积失败怎么办。
- 微信收不到回复怎么办。
- proactive scheduler 停止怎么办。
- SQLite 备份与恢复。
- 磁盘满处理。
- 如何临时停用某个账号。
- 如何回滚最近版本。

## 13. 建议实施范围

### 13.1 第一批必须做

- `/health/live`、`/health/ready`。
- scheduler 心跳。
- 关键链路结构化日志。
- 轻量报警脚本。
- 阿里云云监控 ECS / 端口 / 磁盘报警。
- Admin 状态页最小版。
- 线上 runbook。

### 13.2 第二批再做

- `/metrics` 或 metrics snapshot API。
- SLS 日志采集。
- 错误聚合。
- 更完整 dashboard。
- 按账号维度的异常趋势分析。

## 14. 预计修改文件

后续实现时预计影响：

- `app/main.py`：新增 health endpoints、Admin 状态接口。
- `app/turn_service.py`：补关键链路事件和耗时统计。
- `app/llm.py`：补 LLM 指标和错误分类。
- `app/openclaw_gateway.py`：补 OpenClaw 调用指标。
- `app/proactive/scheduler.py` / `app/dreaming_scheduler.py`：写 scheduler heartbeat。
- `app/db.py`：新增 runtime health / metric event 相关表和查询。
- `app/config.py`：新增报警、监控开关配置。
- `.env.example`：补充监控相关配置。
- `scripts/monitor_health.py`：轻量健康检查和报警脚本。
- `docs/ops/production_runbook.md`：线上排障手册。
- `docs/deploy_aliyun.md`：补充上线前监控 checklist。

## 15. 验收标准

- 停掉 backend，1-2 分钟内收到报警。
- 停掉 proactive scheduler，超过 3 个周期收到报警。
- OpenClaw channel 断开后收到报警。
- 模拟 LLM 失败，Admin 状态页能看到失败计数和最近错误。
- `/health/live` 在 DB 异常时仍能返回进程存活状态。
- `/health/ready` 能正确暴露 DB / 目录 / 配置异常。
- 线上日志中不出现用户消息正文、模型回复正文、OTP、密钥。
- 能从一条失败消息定位到 `trace_id`、`account_id`、失败阶段和错误类型。
- 发布前 checklist 包含监控、报警、备份、OpenClaw、真实冒烟。

## 16. 推荐结论

这个阶段不建议直接上完整 Prometheus + Grafana + Sentry + APM。更合适的版本是：

```text
阿里云云监控
+ systemd 进程管理
+ /health/live / /health/ready
+ scheduler heartbeat
+ 结构化日志
+ 轻量报警脚本
+ Admin 状态页
+ production runbook
```

这套能力足够支撑少量外部用户测试，也不会让项目在上线前被运维体系拖重。后续用户量和事故复杂度上来后，再把日志接 SLS、指标接 Prometheus / Grafana。

## 17. 临时章节：阿里云服务器现场执行清单

本章节记录必须登录阿里云服务器后才能完成或验收的工作。当前代码侧已具备 `/health/live`、`/health/ready`、scheduler heartbeat、`scripts/monitor_health.py`、飞书 webhook 配置项和 Admin 运行状态页；以下事项待服务器部署现场继续。

### 17.1 环境变量确认

在服务器 `.env` 或 systemd 环境中确认：

- `APP_ENV=production` 或明确的线上环境名。
- `FEISHU_ALERT_WEBHOOK_URL` 已配置为外测报警群机器人 webhook。
- `AI4ALL_BRIDGE_SECRET` 不是默认 `dev-secret`。
- `ADMIN_TOKEN` 不是默认 `dev-admin-token`。
- `LLM_API_KEY` 已配置。
- `DATABASE_PATH=data/ai4all.sqlite3` 指向标准数据库。

不得把完整 webhook、token、API key 写入文档或提交仓库。

### 17.2 服务健康检查验收

服务启动后执行：

```bash
curl -s http://127.0.0.1:8180/health
curl -s http://127.0.0.1:8180/health/live
curl -s http://127.0.0.1:8180/health/ready
```

预期：

- `/health` 返回 `status=ok`。
- `/health/live` 返回 `status=ok`。
- `/health/ready` 返回 `status=ok`，且 `db`、`user_profiles_dir`、`system_dir`、`runtime_config` 均为 `ok`。

如 `/health/ready` 失败，先处理配置、目录权限或 DB 连接问题，再继续后续步骤。

### 17.3 Admin 运行状态页验收

浏览器访问：

```text
http://<server-host>:8180/ui/ops.html
```

使用 Admin token 登录后检查：

- Ready 状态展示正常。
- Ready checks 能看到 DB、目录、配置状态。
- Scheduler 区域能看到 proactive / dreaming 配置。
- Scheduler 启动后能看到 heartbeat。
- 最近 1 小时指标能展示账号、入站、错误、出站和绑定摘要。
- 最近错误区不展示用户聊天正文。

### 17.4 监控脚本手动验收

先手动执行 dry-run：

```bash
cd /opt/ai4all-weixin-bot
MONITOR_SCHEDULERS=proactive_scheduler:90,dreaming_scheduler:900 \
.venv/bin/python scripts/monitor_health.py --dry-run
```

预期正常时输出：

```text
ok
```

如果 scheduler 尚未启用，可先只检查 ready：

```bash
.venv/bin/python scripts/monitor_health.py --dry-run
```

### 17.5 飞书报警真实验收

在确认 webhook 可用后，做一次可控失败测试。

方式一：使用错误 ready URL：

```bash
.venv/bin/python scripts/monitor_health.py \
  --url http://127.0.0.1:8180/not-exists \
  --consecutive-failures 1
```

预期：

- 飞书报警群收到 `[AI4ALL][P1] health monitor failed`。
- 报警内容包含环境、检查时间、失败项。

随后恢复检查：

```bash
.venv/bin/python scripts/monitor_health.py \
  --url http://127.0.0.1:8180/health/ready \
  --consecutive-failures 1
```

预期：

- 正常输出 `ok`。
- 如前一次已触发报警，飞书群收到恢复通知。

测试完成后不要保留错误 URL。

### 17.6 配置定时运行

建议用 systemd timer 或 cron 二选一。

cron 示例：

```cron
* * * * * cd /opt/ai4all-weixin-bot && MONITOR_SCHEDULERS=proactive_scheduler:90,dreaming_scheduler:900 .venv/bin/python scripts/monitor_health.py >> logs/monitor_health.log 2>&1
```

注意：

- 如果暂未启用 `dreaming_scheduler`，先不要把 `dreaming_scheduler:900` 放入 `MONITOR_SCHEDULERS`。
- 如果 proactive scheduler 使用独立进程运行，心跳阈值建议为 `interval * 3`，例如 interval 30 秒则阈值 90 秒。
- `logs/monitor_health.log` 需要确保目录存在，并定期轮转或清理。

### 17.7 OpenClaw 状态检查补充

`scripts/monitor_health.py` 已支持可选 OpenClaw 检查。服务器现场先手动验证：

```bash
openclaw channels status --probe
openclaw channels list
```

预期：

- `openclaw-weixin` channel 存在。
- 微信账号连接状态正常。
- bridge 能正常向后端 `POST /openclaw/turn`。

接入监控脚本：

```bash
MONITOR_CHECK_OPENCLAW=true \
MONITOR_OPENCLAW_CHANNEL=openclaw-weixin \
.venv/bin/python scripts/monitor_health.py --dry-run
```

预期正常输出 `ok`。连续 2 次 probe 失败、gateway 不可达、或 `openclaw-weixin`
未配置启用时发飞书报警。

### 17.8 阿里云云监控配置

在阿里云控制台配置基础报警：

- ECS CPU 5 分钟平均 > 85%。
- ECS 内存 5 分钟平均 > 85%。
- 磁盘使用率 > 80% P2，> 90% P1。
- 磁盘 inode 接近耗尽。
- 端口 `8180` 不可访问。
- 可选：公网入口 / nginx 端口不可访问。

报警接收人优先配置到外测报警群或同一批运维成员。

### 17.9 备份检查

确认发布、重启或迁移前备份：

```bash
cd /opt/ai4all-weixin-bot
mkdir -p data/backups
sqlite3 data/ai4all.sqlite3 ".backup 'data/backups/ai4all_$(date +%Y%m%d_%H%M%S).sqlite3'"
tar -czf "data/backups/user_profiles_$(date +%Y%m%d_%H%M%S).tar.gz" data/user_profiles data/system
```

后续需要补充自动备份检查：

- 当日没有 SQLite 备份时报警。
- 当日没有 user_profiles / system 备份时报警。
- 备份目录过大时提醒清理或同步 OSS。

### 17.10 Runbook 待补

服务器现场验证完成后，新增 `docs/ops/production_runbook.md`，至少覆盖：

- 后端不可用怎么办。
- OpenClaw disconnected 怎么办。
- LLM 大面积失败怎么办。
- 微信收不到回复怎么办。
- proactive scheduler 停止怎么办。
- SQLite 备份与恢复。
- 磁盘满处理。
- 如何临时停用某个账号。
- 如何回滚最近版本。

### 17.11 现场验收完成标准

服务器现场工作完成后，至少满足：

- `/health/ready` 正常。
- `/ui/ops.html` 可访问且展示指标。
- `monitor_health.py --dry-run` 正常。
- 飞书失败报警和恢复通知各测试一次成功。
- scheduler heartbeat 能在 Admin 页面看到。
- 阿里云云监控基础报警已配置。
- 发布前备份已完成。
- OpenClaw channel 手动 probe 正常。
