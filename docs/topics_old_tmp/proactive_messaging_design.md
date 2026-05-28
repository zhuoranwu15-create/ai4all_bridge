# 主动消息与提醒设计

更新时间：2026-05-27

本文是 AI4ALL 对齐 OpenClaw 主动消息体验的专题设计。后续 reminder、heartbeat、commitment、content push 和异步补发相关开发以本文为准。

## 2026-05-23 阶段状态

主动消息与提醒机制的代码开发可以认为已经基本闭环，但尚未完成真实微信端到端联调。

已完成的代码闭环包括：

- OpenClaw Gateway `send` 文本主动发送封装。
- outbound ledger、主动触达每日上限、quiet hours、幂等键和发送状态流转。
- one-shot reminder 的创建、due scan、claim、发送和状态回写。
- `/openclaw/turn` 中的高确定性显式提醒识别和模糊提醒澄清。
- 系统级 scheduler：due reminder -> due commitment -> due account heartbeat scan。
- 账号级 proactive state，作为 heartbeat / commitment 的 cheap pre-filter。
- heartbeat hidden LLM draft、人工 promote/clear、发送后候选清理。
- hidden commitment 抽取、`proactive_commitments` 存储、due dispatch、Admin 查看/取消。

当前暂停继续扩展主动消息功能。下一阶段先整体梳理 AI4ALL 后端架构，确认模块边界、数据模型、调度/worker 进程模型、OpenClaw Gateway 依赖边界和后续生产化方向；架构对齐后再做统一联调与策略调参。

尚未完成的验收：

- 真实微信端到端联调：普通对话 -> hidden commitment pending -> 到期 scheduler -> 微信主动收到消息。
- heartbeat draft -> promote -> scheduler -> 微信主动收到消息的完整联调。
- Gateway 重启后、长时间无入站后 context token / outbound route 稳定性验证。
- 多实例部署下的全局 worker lease / leader election。

## 当前事实修正

### 1. 主动发送不从零建设大号 ChannelSendClient

OpenClaw Gateway 已经提供通用 `send` RPC，可用于 channel/account/target 定向发送。AI4ALL Backend 第一阶段应封装 Gateway `send`，而不是直接绕过 OpenClaw 调 `openclaw-weixin` 的上游 `sendmessage` HTTP endpoint。

`openclaw-weixin` README 中的 `sendmessage` 是微信插件和其上游 API 之间的 endpoint；当前插件声明给 OpenClaw host 的 `gatewayMethods` 仍是 `web.login.start` / `web.login.wait`。因此 Backend 的正确集成面是：

```text
AI4ALL Backend
-> openclaw gateway call send
-> OpenClaw durable outbound path
-> openclaw-weixin outbound adapter
-> weixin sendmessage upstream endpoint
```

当前代码已新增 `app.openclaw_gateway.send_weixin_text()`，封装 Gateway `send` 的最小文本发送参数。

### 2. context_token 先依赖 OpenClaw/openclaw-weixin 管理

`context_token` 是微信主动发送可靠性的关键字段。`openclaw-weixin` 会在入站时缓存 token，并在 outbound adapter 里按 `accountId + to` 查找；该缓存已经支持内存和磁盘持久化。

第一阶段不把 `context_token` 复制进 AI4ALL SQLite。必须先做真实 POC 验证：

- 刚收到用户消息后主动发是否成功。
- OpenClaw Gateway 重启后主动发是否成功。
- 缺少 `accountId` 时是否能解析到正确发送账号。
- `accountId` 错误时是否明确失败。
- 长时间无新入站后 token 是否仍可用。

如果 Gateway `send` 路径无法稳定复用 token，再评估 bridge 是否需要把 token 或可解析 token 的字段传给 Backend。

### 3. 主动发送目标不能未经验证地假设为 sender_id

理论上 Weixin 的 `to_user_id` 应来自当前私聊 peer，例如 `xxx@im.wechat`。现有 `channel_bindings` 已保存 `sender_id`、`chat_id`、`session_key` 和 raw identity，但第一阶段要用真实消息确认哪个字段稳定等于 Weixin `to_user_id`。

在 POC 验证完成前，不要让 reminder/heartbeat 直接批量使用 `channel_bindings.sender_id`。

### 4. SQLite scheduler 必须显式约束进程模型

如果后续把 scheduler 跑在 FastAPI 进程内，部署必须强制 `uvicorn --workers 1`。否则多个 worker 会重复扫描同一批 due reminder，造成重复发送。

可接受的 MVP 方案只有两种：

- 单进程 FastAPI + in-process scheduler，并在部署文档中写死 `workers=1`。
- 独立 worker 进程跑 scheduler，FastAPI 不跑 scheduler。

无论哪种，都需要 DB 原子 claim：`pending -> sending` 成功后才允许发送。

### 4.1. 计划任务是 AI4ALL 原生基础能力，不依赖 OpenClaw Cron

OpenClaw 的 `cron.add` 是重要参考：它证明了精确提醒、周期任务、失败记录、delivery target 保留和 Gateway 重启恢复都应该是 agent 产品的基础能力。但 AI4ALL 长期不会把 OpenClaw Gateway 当作核心运行时，因此不要把生产提醒的 source of truth 放进 OpenClaw Cron。

AI4ALL 的长期方向是建设自己的 scheduled task substrate：

- AI4ALL DB 保存任务定义、运行状态、用户归属、channel route、幂等键、取消/更新状态和审计记录。
- worker/scheduler 从 AI4ALL DB 扫描 due work，原子 claim 后执行。
- 所有主动触达必须先经过 AI4ALL 的 outbound ledger、用户级限额、quiet hours、主动触达开关和风控策略。
- OpenClaw Gateway 在当前阶段只是微信发送适配层；未来可以替换为其他 channel adapter。
- OpenClaw Cron 可以作为设计参考或临时 POC 工具，但不是长期依赖，也不是 reminder 的主数据源。

这意味着后续的 `set_reminder` / `cancel_reminder` / recurring task / commitment delivery 都应围绕 AI4ALL 自己的任务模型展开，而不是直接绑定 `openclaw cron add`。

### 5. 主动消息需要独立 outbound 限额

当前系统已有 `/openclaw/turn` 入站 daily/rpm 限流，但主动消息是新的 outbound 入口，不能直接复用入站计数。

reminder/heartbeat/commitment 上线前，需要 outbound ledger 和硬上限，例如：

- 每账号每日主动消息总量上限。
- reminder 和 inferred commitment 分开计数。
- quiet hours 内默认不发。
- 用户可关闭主动触达。

### 6. heartbeat 是系统级调度，但单用户隔离执行

OpenClaw 的 heartbeat 更接近围绕单个使用者运行的个人循环。AI4ALL 后端天然服务多个用户，不能照搬成“一个全局用户”的机制。

AI4ALL 的正确边界是：

```text
system proactive scheduler
-> 扫描 eligible accounts / due work items
-> 原子 claim account 或 reminder/commitment
-> 对每个 account 单独执行 heartbeat run
-> 输出 no-op 或 outbound_message
-> 经过 outbound ledger / 限额 / quiet hours
-> OpenClaw Gateway send
```

也就是说，调度器是系统级或后端项目级的；每次实际判断与生成消息的 run 必须是用户级的，只读取该 `account_id` 的主动触达配置、due reminders、due commitments、必要记忆、channel binding 和 outbound limit。

第一版不能每隔 N 分钟对每个用户都调用 LLM。必须先用数据库做 cheap pre-filter：

- 有 due reminder：直接格式化或发送，不需要 LLM 判断。
- 有 due commitment：再做高阈值判断。
- 普通 heartbeat：只有在用户允许、长期未触达、有明确候选事项时才运行隐藏 LLM。
- 没有候选事项：直接 no-op。

当前已新增 `proactive_account_state` 作为未来 heartbeat / commitment cheap pre-filter 的账号级状态底座，保存 `enabled`、`next_scan_at`、`last_scan_at`、`last_proactive_sent_at`、`cooldown_until`、`metadata_json` 等字段。它是独立入口，不替换现有 due reminder 扫描；没有 state 行的账号，已创建的 due reminder 仍会被 scheduler 正常扫描和发送。

### 7. 个人微信号风控是产品硬约束

AI4ALL 当前是个人微信号伴侣场景，不是客服号、企业号或公众号推送场景。主动消息频率不能按客服模式对标 OpenClaw 的通用能力。

默认策略应是低频、高置信、用户明确允许、可随时取消。第一版宁可漏发，也不能高频打扰。

### 8. commitment 抽取必须隔离

隐式 follow-up commitment 抽取必须使用独立隐藏链路：

- 独立 system prompt。
- 不复用主聊天 prompt。
- 不写入用户可见聊天历史。
- 不让用户感知到该调用。
- tools disabled。
- 只输入必要 turn/context。
- 有 token 和成本预算。

## 更新后的推进顺序

### Step 1：主动发送 POC

目标：证明 Backend 能通过 OpenClaw Gateway 主动发送一条微信文本消息。

已完成代码面：

- `app.openclaw_gateway.send_weixin_text()` 封装 Gateway `send`。
- 单元测试覆盖 Gateway method、参数形状、idempotencyKey 和必填校验。

真实验证结果：

- 用真实绑定账号调用 `send_weixin_text()`。已验证：`openclaw gateway call send` 返回 `messageId=openclaw-weixin:1779414753885-6bd8a9be`。
- 确认 `to_user_id` 应使用 `channel_bindings.sender_id`、`chat_id` 还是从 `session_key` 解析出的 direct peer。当前验证结论：Weixin 主动发送目标使用 `channel_bindings.chat_id`，例如 `xxx@im.wechat`，不要使用 `sender_id`。
- 确认 `accountId` 必须显式传入 OpenClaw 通道账号，而不是 AI4ALL 业务账号。当前验证结论：传 `channel_bindings.channel_account_id`，例如 `d075590ddfc2-im-bot`。
- 已验证刚收到用户消息后主动发成功；仍需验证 Gateway 重启后和长时间无新入站后 context token 是否仍可支撑主动发送。

### Step 2：outbound ledger 与限额

新增 `outbound_messages`，记录主动消息生命周期：

```text
pending -> sending -> sent / failed / cancelled
```

关键字段：`account_id`、`channel`、`channel_account_id`、`to_user_id`、`session_key`、`source`、`idempotency_key`、`status`、`attempts`、`error`、`sent_at`。

同时加入主动发送每日上限和 quiet hours，但策略需要按消息类型区分。

已完成代码面：

- `outbound_messages` 表已加入 SQLite schema，额外保存 `text`、`gateway_message_id`、`quota_date`、`metadata_json`、`created_at`、`updated_at`。
- `app.db` 已提供创建、查询、按日计数、claim、mark sent、mark failed、cancel 的生命周期函数。
- `app.proactive.messaging.enqueue_proactive_text()` 当前会统一应用 `proactive_outbound_daily_limit` 和 quiet hours，未通过策略的消息写成 `cancelled`，不消耗主动发送额度；`pending`、`sending`、`sent`、`failed` 会计入当日额度，避免 Gateway 异常时无限尝试。
- 该实现口径需要按最新 PRD 调整：用户提醒不受主动触达总开关、默认每日主动消息上限和 quiet hours 影响；陪伴跟进和内容推送才受总开关、quiet hours、默认日上限、分类上限和 6 小时避让约束。
- 后续实现建议将 outbound policy 拆为 `source=reminder`、`source=companion_followup`、`source=content_push`、`source=async_task_result` 等类型化策略，而不是在 `send_proactive_text()` 中使用单一全局策略。
- `app.proactive.messaging.send_proactive_text()` 会先写 ledger，再原子 claim 为 `sending`，调用 OpenClaw Gateway，最后写 `sent` 或 `failed`。
- 新增配置：`proactive_outbound_enabled`、`proactive_outbound_daily_limit`、`proactive_quiet_hours_start`、`proactive_quiet_hours_end`。
- 单元测试覆盖 ledger 生命周期、daily limit、quiet hours 和 Gateway 成功发送状态流转。

当前约束：

- quiet hours 和 `quota_date` 先按后端本机时区计算；后续多地区用户需要迁移到用户级 timezone。
- limit check 适配当前单进程/单 worker MVP；进入多 worker 前需要把 quota claim 做成更严格的 DB 原子保留或独立 worker。

### Step 3：一次性 reminder MVP

新增 `reminders` 表，只支持 one-shot `due_at`。scheduler 扫描 due reminder，先原子 claim，再调用 outbound send。

MVP 明确单进程或独立 worker，不允许多 worker 重复跑 scheduler。

已完成代码面：

- `reminders` 表已加入 SQLite schema，保存 `account_id`、`channel`、`channel_account_id`、`to_user_id`、`session_key`、`text`、`due_at`、`status`、`attempts`、`outbound_message_id`、`error`、`metadata_json` 和生命周期时间戳。
- `app.db` 已提供 reminder 创建、查询、due scan、原子 claim、mark sent、mark failed、cancel 的生命周期函数。
- `app.proactive.reminders.dispatch_due_reminders()` 会扫描 due reminder，逐条 claim，再调用 `app.proactive.messaging.send_proactive_text()`。
- outbound idempotency key 使用 `reminder-{reminder_id}`，避免重复 dispatch 时重复发送。
- outbound `sent` 会把 reminder 标记为 `sent`；outbound `cancelled` 会把 reminder 标记为 `cancelled`；Gateway 异常或 outbound `failed` 会把 reminder 标记为 `failed`。
- 单元测试覆盖创建/claim/sent、未到期不触发、发送成功、quiet hours 取消和 Gateway 失败；其中 quiet hours 取消 reminder 是旧口径，需要改为“用户提醒命中 quiet hours 仍按用户设定时间发送”。

当前约束：

- 目前已提供 dispatcher、admin run-once、可选 FastAPI startup loop 和独立 worker 脚本。默认不自动启动 scheduler；生产启用前必须明确单进程 `uvicorn --workers 1` 或改为独立 worker。
- `due_at` 先使用后端本机时区的 `YYYY-MM-DD HH:MM:SS` 字符串；后续要迁移到用户 timezone + UTC 存储。
- 用户提醒命中 quiet hours 时，应严格按用户设定时间发送，不应被 quiet hours 取消或自动顺延。
- quiet hours 只默认约束陪伴跟进、内容推送等非用户明确设定的主动触达。

### Step 4：显式提醒识别

先支持高确定性的显式提醒，例如“明天上午 10 点提醒我...”。可以用规则 + 独立 LLM classifier；稳定后再升级成正式工具 `set_reminder`。

接入前必须更新 `TOOLS.md`，否则主聊天 prompt 仍应约束模型不能承诺设置提醒。

已完成代码面：

- `app.reminder_parser.parse_explicit_reminder()` 使用规则解析高确定性提醒，不调用 LLM。
- 当前支持包含“提醒我 / 提醒一下我”的一次性提醒，并要求同时具备明确日期和明确时间。
- 支持示例：
  - `明天上午10点提醒我检查事情A`
  - `今天下午3点提醒我去趟派出所`
  - `2026-05-23 18:30提醒我去趟派出所`
  - `5月23日晚上8点提醒我给家里打电话`
- 暂不支持示例：
  - `下午提醒我去趟派出所`（缺少具体时间）
  - `明天10点提醒我检查事情A`（缺少上午/下午，10 点存在歧义）
  - 周期性提醒、重复提醒、自然语言模糊时间。
- `/openclaw/turn` 中，明确提醒会在 LLM 前短路：写入 `reminders` 并返回确定性确认；模糊提醒会返回“请补充具体日期和时间”的确定性澄清。
- 新建账号默认 `TOOLS.md` 已更新：明确时间的一次性提醒由后端规则链路支持，但模型仍不能自行承诺未接入的外部工具。
- 单元测试覆盖规则解析、入站创建 reminder、不明确时间澄清、重复消息不重复建 reminder。

前端/微信 bot 可测试节点：

1. 正常对话回归：发送 `你好`，应正常回复，不创建 reminder。
2. 模糊提醒澄清：发送 `下午提醒我去趟派出所`，应要求补充具体日期和时间，不创建 reminder。
3. 明确提醒创建：发送 `明天上午10点提醒我检查事情A`，应回复“好的，我会在 ... 提醒你：检查事情A”，并写入 `reminders.status=pending`。
4. 到点主动发送：可以用 admin run-once 触发一次扫描，或启动独立 worker。触发后应写 `outbound_messages` 并通过微信主动收到提醒。

```bash
curl -X POST \
  'http://127.0.0.1:8000/admin/proactive/scheduler/run-once?limit=20' \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Step 5：系统级 proactive scheduler + 用户级 heartbeat run

后端运行系统级 proactive scheduler，轮询 eligible users/accounts 和 due work items。每次实际 heartbeat run 必须限定在单个 `account_id`，只读取用户主动触达配置、due reminders、due commitments、必要记忆、channel binding 和 outbound limit。输出只能是一条短消息或 no-op，不读取全局 `HEARTBEAT.md` 作为个人任务来源。

已完成第一阶段：

- proactive 相关代码已收拢到 `app/proactive/` package：
  - `app.proactive.messaging`：outbound ledger + Gateway send。
  - `app.proactive.reminders`：one-shot reminder dispatch。
  - `app.proactive.commitments`：hidden follow-up commitment extraction + due commitment dispatch。
  - `app.proactive.scheduler`：系统级 scheduler loop。
  - `app.proactive.state`：账号级 proactive state 底座。
  - `app.proactive.heartbeat`：用户级 heartbeat 非 LLM 决策函数。
- 旧顶层 proactive 兼容模块已删除；当前仍处早期开发阶段，不保留这些兼容层，所有新代码只导入 `app.proactive.*`。
- `/openclaw/turn` 的业务逻辑已从 `app.main` 抽到 `app.turn_service.handle_openclaw_turn()`；`app.main` 只保留 FastAPI route、auth、admin/onboarding 等 HTTP 边界。
- `identity_response_metadata()` 已移动到 `app.identity`，避免 route/service 循环依赖。
- 新增 `app.proactive.scheduler.ProactiveScheduler`，作为系统级 cheap pre-filter loop。
- 当前 scheduler 处理三类 cheap work：due reminders、due commitments，以及 due proactive accounts 的 heartbeat decision + execution scan。scheduler 本身不调用 LLM；hidden LLM 只发生在后台抽取/候选生成接口里。
- 新增配置：`proactive_scheduler_enabled`、`proactive_scheduler_interval_seconds`、`proactive_scheduler_batch_size`、`proactive_scheduler_bypass_quiet_hours`、`proactive_account_scan_interval_seconds`。
- FastAPI startup 支持显式开启 in-process scheduler；默认关闭，避免多 worker 重复扫描。
- 新增 admin API：
  - `GET /admin/proactive/scheduler` 查看配置和运行状态。
  - `POST /admin/proactive/scheduler/run-once` 手动触发一次 due reminder / due commitment / due account scan。
  - `GET /admin/accounts/{account_id}/proactive-state` 查看账号 proactive state。
  - `PATCH /admin/accounts/{account_id}/proactive-state` 创建/更新 `enabled`、`next_scan_at`、`cooldown_until`、`metadata`。
  - `POST /admin/accounts/{account_id}/heartbeat-candidate-draft` 手动触发隐藏 LLM 候选生成；结果只写入 `metadata.heartbeat_candidate_draft`，不触发发送。
  - `POST /admin/accounts/{account_id}/heartbeat-candidate-draft/promote` 人工确认 draft，把它提升为可发送的 `metadata.heartbeat_candidate`。
  - `DELETE /admin/accounts/{account_id}/heartbeat-candidate-draft` 清理不合适的 draft。
  - `GET /admin/accounts/{account_id}/commitments` 查看账号下 hidden extractor 写入的 commitments。
  - `POST /admin/commitments/{commitment_id}/cancel` 取消不应发送的 commitment。
- 新增 `scripts/run_proactive_scheduler.py`，用于独立 worker 进程。
- 新增 `proactive_account_state` 表和 `app.proactive.state`，提供 `ensure_account_state()`、`list_due_proactive_accounts()`、`claim_due_account_scan()`、`scan_due_proactive_accounts()`、`mark_account_scanned()`、`mark_account_proactive_sent()` 等函数。
- `ProactiveScheduler.run_once()` 现在会先 dispatch due reminders，再 dispatch due commitments，最后扫描 due proactive accounts。account scan 当前会原子 claim/mark scanned，然后调用 `app.proactive.heartbeat.decide_heartbeat_action()` 和 `execute_heartbeat_decision()`。
- 新增 `proactive_commitments` 表，用于保存普通对话后隐藏抽取出的 inferred follow-up：`pending -> sending -> sent / failed / cancelled`。
- 普通 `/openclaw/turn` 只有在成功走完正常聊天回复后，才会在后台触发 `extract_commitment_from_turn()`；该抽取使用独立 system prompt，不复用主聊天 prompt，不写入用户可见聊天历史，不让用户感知。
- commitment 抽取默认要求：账号 active、`proactive_account_state.enabled=true`、有可用 channel route、配置了 LLM key、置信度达到 `proactive_commitment_min_confidence`、`due_at` 是未来且不超过 `proactive_commitment_max_days`。不满足则 no-op。
- due commitment 到期后由 `dispatch_due_commitments()` 原子 claim，再通过 `send_proactive_text(source="commitment")` 进入 outbound ledger、每日上限、quiet hours 和 Gateway 发送链路；发送成功后回写 commitment `sent` 和账号 `last_proactive_sent_at`。
- `decide_heartbeat_action()` 当前只做非 LLM 策略判断：账号状态、proactive enabled、cooldown、outbound 开关、quiet hours、每日主动发送额度、channel route，以及 state metadata 中的显式 `heartbeat_candidate`。
- 没有候选事项时返回 `no_op: no_candidate` 并跳过发送；如果 state metadata 中存在显式候选且策略允许，返回 `send_text` 决策，并通过 `send_proactive_text()` 写入 outbound ledger、调用 Gateway、回写 sent/failed/cancelled。
- heartbeat 发送成功后会回写 `proactive_account_state.last_proactive_sent_at`，并把 active `metadata.heartbeat_candidate` 移到 `metadata.heartbeat_last_sent_candidate`，避免下一轮重复发送同一个候选。
- 新增隐藏 LLM 候选生成函数 `generate_heartbeat_candidate_draft()`，使用独立 system prompt，只读取单个账号的最近对话、USER/MEMORY、daily notes 和 state metadata。它只写 `heartbeat_candidate_draft`，不会写 `heartbeat_candidate`，因此不会自动扩大主动发送范围。
- 新增 draft promote/clear 函数：`promote_heartbeat_candidate_draft()` 和 `clear_heartbeat_candidate_draft()`。当前 promote 是人工确认入口；后续如要策略自动 promote，仍必须沿用高阈值、低频和可审计的约束。
- 新增配置：`proactive_heartbeat_candidate_context_messages`、`proactive_heartbeat_candidate_min_confidence`、`proactive_commitment_extraction_enabled`、`proactive_commitment_context_messages`、`proactive_commitment_min_confidence`、`proactive_commitment_max_days`。
- 没有 `proactive_account_state` 行的账号不会进入 heartbeat scan，但它已有的 due reminder 仍会被 scheduler 正常扫描和发送。
- 没有 `proactive_account_state` 行或 state disabled 的账号不会执行 hidden commitment extraction，也不会 dispatch due commitments。
- 单元测试覆盖 scheduler run-once、admin 状态、admin proactive state 读写、admin run-once 真实 dispatch 链路、heartbeat no-op/send_text 决策与 outbound 执行、隐藏 LLM draft 生成/低置信拒绝、draft promote/clear、hidden commitment 抽取/低置信拒绝、due commitment dispatch、proactive state due scan/no-op claim，以及“没有 proactive_account_state 行时 due reminder 仍可发送”的回归。

阶段性验证 heartbeat 主动触达：

1. 目标账号先通过微信发过真实消息，确保 `channel_bindings` 有可用 route。
2. 用 `PATCH /admin/accounts/{account_id}/proactive-state` 开启 `enabled=true`，并把 `next_scan_at` 设为过去时间。
3. 调 `POST /admin/accounts/{account_id}/heartbeat-candidate-draft` 生成 hidden LLM draft。
4. 用 `GET /admin/accounts/{account_id}/proactive-state` 检查 `metadata.heartbeat_candidate_draft`。
5. 调 `POST /admin/accounts/{account_id}/heartbeat-candidate-draft/promote` 人工提升为 active candidate。
6. 调 `POST /admin/proactive/scheduler/run-once?limit=20`；若未被 quiet hours、daily limit 或 route 问题拦截，微信应收到主动消息。

当前仍未完成/暂缓：

- 真实微信端到端联调与策略调参。
- 架构梳理与模块边界对齐。
- 多实例部署下的全局 worker lease / leader election。
- 周期性提醒、更新/取消自然语言提醒、用户级 timezone。

### Step 6：commitment

已完成第一版代码逻辑：

- 普通聊天回复成功后后台隐藏抽取 inferred follow-up。
- 抽取链路有独立 system prompt，不进入用户可见聊天上下文。
- 高置信结果写入 `proactive_commitments.pending`，并带 `due_at`、`confidence`、`reason`、source message。
- scheduler 到期后 claim due commitment，经过 outbound ledger / quiet hours / daily limit / Gateway 发送。
- Admin 可查看和取消 commitment。

第一版仍是保守策略：只有用户已开启 proactive state、存在可用 channel route、LLM 高置信并给出明确未来时间，才会创建 commitment。
