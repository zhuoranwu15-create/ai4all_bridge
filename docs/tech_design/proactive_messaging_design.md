# 主动消息与提醒技术设计

更新时间：2026-05-31

本文承接 [主动消息与提醒 PRD](../product/proactive_prd.md)，定义 Phase 1 主动消息、用户提醒、陪伴跟进、内容推送和异步结果补发的技术边界。后续 `app.proactive.*`、reminder parser、scheduler、outbound ledger 和 OpenClaw Gateway send 相关开发以本文为准。

## 1. 设计目标

Phase 1 的目标不是做一个高频推送系统，而是在个人微信号场景下提供低风险、可审计、可恢复的主动触达基础能力：

- 用户提醒：用户明确设置的一次性或周期性提醒，按用户设定时间发送。
- 陪伴跟进：基于聊天历史、hidden commitment 或关系上下文的低频关怀。
- 内容推送：新闻、搞笑内容或其他垂类内容的低频试探与推送。
- 异步结果补发：长耗时 Web Search 兜底、ASR 或其他用户触发长任务完成后的结果投递。

其中异步结果补发复用 outbound ledger 和 Gateway send，但不是产品意义上的主动消息；它来自用户已触发任务，不受主动触达总开关、陪伴/内容推送日上限约束。

## 2. 产品不变量

以下规则是技术实现必须保持的产品不变量：

| 类型 | 触发来源 | 总开关 | Quiet hours | 默认日上限 | 6 小时避让 | 费用 |
| --- | --- | --- | --- | --- | --- | --- |
| 用户提醒 | 用户明确设置 | 不受影响 | 严格按用户设定时间发送 | 不设上限 | 不被低优先级反向压制 | 提醒投递首条不扣用户贝壳 |
| 陪伴跟进 | hidden commitment、heartbeat、历史话题 | 受影响 | 受影响，默认 22:00 到次日 7:00 | 默认 1 条 | 避让用户提醒 | 首条不扣用户贝壳 |
| 内容推送 | 内容策略、垂类源、运营配置 | 受影响 | 受影响，默认 22:00 到次日 7:00 | 默认 1 条 | 避让用户提醒和陪伴跟进 | 首条不扣用户贝壳 |
| 异步结果补发 | 用户触发的长任务或搜索兜底 | 不受主动触达总开关影响 | 默认不因 quiet hours 丢弃 | 不占主动消息日上限 | 不参与主动消息避让 | 任务本身按任务成本扣减 |

补充规则：

- 所有 outbound 都必须写入 `outbound_messages`，保留幂等键、策略结果、发送状态、Gateway message id 和错误信息。
- 用户收到主动触达后继续回复，后续 AI 回复、搜索、ASR 或其他任务按普通聊天或任务规则扣减贝壳。
- 陪伴跟进和内容推送不设置合计日上限或周上限，但各自默认每日最多 1 条。
- 内容推送可以低频试探；用户明确拒绝某类内容后，Phase 1 至少 1 个月不再推送该类别，1 个月后重新试探策略只记录，不实施。

## 3. OpenClaw 借鉴与 AI4ALL 差异

OpenClaw 的 heartbeat、cron 和 channel send 证明了 agent 产品需要具备定时任务、主动发送、任务恢复和上下文感知能力。AI4ALL 应借鉴这些设计，但不能照搬运行边界：

- OpenClaw 更接近一对一个人 agent；AI4ALL 是一对多服务，所有主动判断必须按 `ai4all_account_id` 隔离。
- OpenClaw Gateway 当前作为微信通道适配层；用户提醒、候选、策略、权益和审计的 source of truth 在 AI4ALL Backend。
- OpenClaw Cron 可以作为参考或 POC 工具，但 Phase 1 生产提醒应落在 AI4ALL 自己的 DB 和 scheduler。
- Heartbeat 在 AI4ALL 中拆成系统级 scheduler 与用户级 run；系统级只做 cheap pre-filter，用户级只读取单个账号状态和上下文。

## 4. 当前代码基线

当前已有可复用基础：

- `app.openclaw_gateway.send_weixin_text()` 已封装 OpenClaw Gateway `send`。
- 已验证主动发送目标应使用 `channel_bindings.chat_id`，OpenClaw 通道账号应使用 `channel_bindings.channel_account_id`。
- `outbound_messages` 已有 `pending -> sending -> sent / failed / cancelled` 生命周期。
- `reminders` 已支持 one-shot reminder 的创建、due scan、claim、发送和状态回写。
- `app.reminder_parser.parse_explicit_reminder()` 已支持高确定性一次性提醒解析。
- `app.proactive.scheduler.ProactiveScheduler` 已按 due reminder、due commitment、account heartbeat scan 的顺序运行。
- `proactive_account_state` 已作为 heartbeat / commitment 的账号级 cheap pre-filter。
- `proactive_commitments` 已支持 hidden commitment 抽取、存储、due dispatch、Admin 查看和取消。
- `heartbeat_candidate_draft -> promote -> heartbeat_candidate -> send` 已形成人工确认链路。

当前主要偏差：

- `app.proactive.messaging.enqueue_proactive_text()` 仍使用单一全局策略，统一套用 `proactive_outbound_enabled`、quiet hours 和 `proactive_outbound_daily_limit`。
- 用户提醒当前会被 quiet hours 或全局日上限影响，除非调用方显式传 `bypass_quiet_hours`；这与最新 PRD 冲突。
- `proactive_outbound_daily_limit` 当前是单个全局上限，不区分用户提醒、陪伴跟进、内容推送和异步结果补发。
- due commitment 和 heartbeat 当前使用 `source="commitment"` / `source="heartbeat"`，产品上都应归入“陪伴跟进”。
- 尚未实现周期性提醒、自然语言取消/更新的二次确认、6 小时避让、内容推送、内容拒绝冷却和用户级 timezone。
- 尚未完成真实微信端到端联调、Gateway 重启后主动发送稳定性验证、长时间无入站后的 route/context token 验证。
- 多实例部署下尚缺全局 worker lease 或 leader election。

## 5. 目标分类模型

技术侧保留更细的 `source`，但必须映射到产品分类：

| source | product_category | 说明 |
| --- | --- | --- |
| `reminder` | `user_reminder` | 用户设置的一次性或周期性提醒 |
| `reminder_change_confirmation` | `user_reminder` | 取消/更新提醒的确认回复，通常走入站同步回复，不一定进入 outbound |
| `commitment` | `companion_followup` | 从聊天中隐藏抽取的后续跟进 |
| `heartbeat` | `companion_followup` | 低频主动关怀或历史话题跟进 |
| `content_push` | `content_push` | 新闻、搞笑内容或其他垂类推送 |
| `async_task_result` | `task_result` | 用户触发长任务完成后的结果补发 |

建议新增常量或枚举：

```text
OutboundCategory.USER_REMINDER
OutboundCategory.COMPANION_FOLLOWUP
OutboundCategory.CONTENT_PUSH
OutboundCategory.TASK_RESULT
```

`outbound_messages.source` 继续记录具体来源，新增或在 metadata 中稳定记录 `product_category`、`policy_version`、`policy_decision` 和 `policy_reason`。

## 6. Outbound Policy Engine

应将当前散落在 `enqueue_proactive_text()`、`decide_heartbeat_action()` 和 scheduler 调用参数里的策略，收敛为一个类型化 policy engine。

目标接口：

```python
evaluate_outbound_policy(
    account_id: str,
    category: OutboundCategory,
    source: str,
    scheduled_at: datetime | None,
    now: datetime,
    metadata: dict,
) -> PolicyDecision
```

PolicyDecision 至少包含：

```text
allowed: bool
status: pending | cancelled
reason: string | None
quota_date: string
counts: dict
next_allowed_at: string | None
metadata: dict
```

通用检查：

- 账号存在且 active。
- 有可用 channel route：`channel`、`channel_account_id`、`to_user_id`、`session_key`。
- 幂等键稳定，重试不重复发送。
- outbound ledger 写入失败时，不调用 Gateway。
- Gateway 发送失败时，写入 `failed`，保留错误原因和重试所需字段。

分类策略：

| category | 策略 |
| --- | --- |
| `user_reminder` | 不检查主动触达总开关；不检查 quiet hours；不占陪伴跟进/内容推送日上限；仍写 ledger、route、幂等和失败状态 |
| `companion_followup` | 检查主动触达总开关、quiet hours、账号 cooldown、分类日上限 1、6 小时内是否有用户提醒 |
| `content_push` | 检查主动触达总开关、quiet hours、账号 cooldown、分类日上限 1、6 小时内是否有用户提醒或陪伴跟进、内容拒绝冷却 |
| `task_result` | 不检查主动触达总开关；不占主动消息日上限；默认不因 quiet hours 丢弃；仍写 ledger、route、幂等和任务成本 metadata |

Admin 手动 `bypass_quiet_hours` 只能作为排障参数进入 metadata，不能成为生产策略绕过用户提醒规则的主要机制。用户提醒本身应天然不受 quiet hours 影响。

## 7. 6 小时避让

避让只约束陪伴跟进和内容推送，不约束用户提醒。

调度候选阶段：

1. 生成陪伴跟进候选前，查询未来 6 小时是否已有 pending/sending/scheduled 用户提醒。
2. 生成内容推送候选前，查询未来 6 小时是否已有用户提醒或陪伴跟进。
3. 如果被更高优先级压制，记录 `policy_reason=avoidance_window`，候选延后或丢弃。

实际发送阶段：

1. `companion_followup` 发送前再次查询 `now..now+6h` 的用户提醒。
2. `content_push` 发送前再次查询 `now..now+6h` 的用户提醒和陪伴跟进。
3. 如果发送前发现更高优先级消息，低优先级 outbound 应进入 `cancelled` 或 `deferred`。Phase 1 如果暂不新增 `deferred` 状态，可以取消并让候选生成器下次重新判断。

建议查询口径：

- 用户提醒来源：`reminders.status in ('pending', 'sending')` 且 `due_at` 在窗口内。
- 陪伴跟进来源：`proactive_commitments.status in ('pending', 'sending')`、active `heartbeat_candidate`、以及 `outbound_messages` 中同日 pending/sending/sent 的 `companion_followup`。
- 内容推送来源：未来新增 `content_push_candidates` 或 `outbound_messages`。

## 8. 用户提醒

### 8.1 一次性提醒

当前实现可以复用：

- 入站 `/openclaw/turn` 在 LLM 前执行高确定性规则解析。
- 解析成功后写入 `reminders`，返回确定性确认。
- Scheduler 到期 claim reminder，再发送 outbound。

需要调整：

- `dispatch_reminder()` 调用 outbound 时应传入 `category=user_reminder`，而不是依赖 `bypass_quiet_hours`。
- 用户提醒发送失败不能因为 quiet hours 被 `cancelled`；只有 route 缺失、Gateway 失败、用户取消或幂等冲突等原因才应失败或取消。
- 测试中“quiet hours 取消 reminder”的旧用例应改成“quiet hours 仍发送 reminder”。

### 8.2 周期性提醒

目标数据模型可以在现有 `reminders` 上扩展，也可以新增 `recurrence` 相关表。Phase 1 推荐在 `reminders` 中扩展以下字段：

```text
reminder_type: one_shot | recurring
recurrence_rule_json
next_due_at
timezone
last_sent_at
parent_reminder_id
```

`recurrence_rule_json` 最少支持：

```json
{
  "freq": "daily|weekly|monthly",
  "interval": 1,
  "by_weekday": ["SA"],
  "by_monthday": [15],
  "time": "10:00"
}
```

技术规则：

- 最小周期为 1 天，小于 1 天的规则直接拒绝。
- 用户只给周期没有具体时点时，parser/LLM classifier 可选择合理时点，并在回复中请求确认。
- 周期提醒到期发送成功后，计算下一次 `next_due_at`，状态回到 `pending`。
- 如果某次发送失败，保留失败次数和下一次重试/补偿策略，不能直接丢失整个周期任务。

### 8.3 自然语言取消/更新

取消和更新必须二次确认。

建议新增 `reminder_change_requests`：

```text
id
account_id
target_reminder_id
change_type: cancel | update
proposed_patch_json
status: pending_confirmation | applied | rejected | expired
source_message_id
created_at
expires_at
confirmed_at
```

流程：

1. 入站消息识别为取消或更新提醒。
2. 检索候选 reminders；无法唯一匹配时让用户补充。
3. 生成 `reminder_change_requests.pending_confirmation`。
4. 同步回复拟调整结果，等待用户确认。
5. 下一条用户消息若是正向确认，则 apply patch；否则保持原提醒不变。

确认期内，普通聊天主链路需要优先判断是否在处理 pending change request，避免把“确认”“好的”误当普通聊天。

## 9. 陪伴跟进

陪伴跟进包括当前代码中的 hidden commitment 和 heartbeat。

hidden commitment：

- 普通聊天成功回复后异步抽取。
- 使用独立 system prompt，不复用主聊天 prompt。
- 不写入用户可见聊天历史。
- 不读取跨账号信息。
- 只在高置信、有明确未来时间或明确陪伴价值时创建。
- 到期发送前走 `companion_followup` policy。

heartbeat：

- 系统级 scheduler 只扫描 due account，不直接调用 LLM。
- 用户级 heartbeat run 只读取单个账号的 proactive state、USER/MEMORY、daily notes、候选事项和 channel route。
- Phase 1 默认走 draft -> 人工 promote -> send，减少误触达。
- 如果未来支持自动 promote，仍必须经过高阈值、低频、可审计策略。

需要调整：

- `source="commitment"` 和 `source="heartbeat"` 都映射到 `product_category=companion_followup`。
- 日上限按 `companion_followup` 分类计算，默认每日 1 条。
- 发送前执行 6 小时用户提醒避让。
- 用户拒绝陪伴跟进后，应写入 proactive state 或 `USER.md` 摘要，并进入冷却。

## 10. 内容推送

内容推送 Phase 1 目标是低频试探，不是订阅系统。

建议目标链路：

```text
content source / operator config
-> content_push_candidate
-> policy check
-> outbound_messages
-> OpenClaw Gateway send
-> feedback detector
-> preference / cooldown update
```

建议数据模型：

```text
content_push_candidates
- id
- account_id
- category
- source
- title
- body
- scheduled_at
- status: pending | sent | cancelled | rejected_by_policy
- metadata_json

content_push_preferences
- account_id
- content_category
- status: allowed | cooled_down | blocked
- cooldown_until
- last_rejected_at
- metadata_json
```

规则：

- 发送前走 `content_push` policy。
- 每账号内容推送默认每日最多 1 条。
- 各垂类内容默认试探频率每天最多 1 次，但受内容推送总日上限约束。
- 用户明确拒绝某类内容后，至少 1 个月内不再推送该类别。
- Phase 1 不实施 1 个月后的重新试探，只记录状态。

## 11. 异步结果补发

普通 Web Search 默认在当前 turn 通过 LLM tool use 同步完成。长耗时搜索、复杂资料整理、provider 超时或用户明确要求后台整理时，才转为异步兜底：先同步告知“后台正在工作”，任务完成后再补发完整结果。

异步结果补发应复用 outbound ledger，但策略不同：

- `source=async_task_result`。
- `product_category=task_result`。
- 不受主动触达总开关影响。
- 不占陪伴跟进或内容推送日上限。
- 默认不因 quiet hours 丢弃；如果未来产品决定夜间延迟补发，应在搜索/异步任务 PRD 中另行明确。
- 成本按任务本身的 provider、LLM、搜索、摘要和补发消耗拆解，映射到贝壳扣减。

### 11.1 Outbound 与 Session / Message 关系

所有成功发送给用户的 outbound 都是已经发生的对话事实，必须进入当前账号的 active session，写入 `messages`，不依赖用户是否反馈。

适用范围：

- 用户提醒到期发送。
- 陪伴跟进 / hidden commitment / heartbeat。
- 内容推送。
- 异步任务结果补发。
- 发送失败说明或任务失败说明，只要实际发给了用户，也应写入 `messages`。

写入规则：

- 只有 Gateway send 成功或被系统判定为已投递/已接受发送后，才写入 `messages`；pending、cancelled、failed 且未发出的 outbound 不写入对话消息。
- `messages.direction = outbound`，`role = assistant`，`message_type = text` 或对应媒体类型。
- `messages.session_id` 使用该账号当前 active session；如果不存在 active session，则先按账号创建一个 active session。
- `messages.channel_binding_id`、`channel`、`message_id/gateway_message_id`、`outbound_message_id`、`source`、`product_category` 等信息应进入结构化字段或 metadata，便于追踪。
- `outbound_messages` 继续作为发送幂等、策略、重试和 Gateway 状态 ledger；`messages` 作为用户可见对话时间线和后续 prompt 上下文。两者不能互相替代。

后续用户回复时，普通入站链路解析到同一个 `ai4all_account_id` 后读取 active session 最近消息，自然能看到此前主动发出的 assistant message。模型可以基于这条事实继续对话，例如用户回复“好”“别再发这个”“展开说说”时，都能找到上一条主动消息的上下文。

## 12. Scheduler 与部署

MVP 可接受两种部署方式：

- 单进程 FastAPI + in-process scheduler，部署必须固定 `uvicorn --workers 1`。
- 独立 worker 进程运行 `scripts/run_proactive_scheduler.py`，FastAPI 不启动 in-process scheduler。

进入正式内测前，推荐使用独立 worker，并补齐：

- worker lease 或 leader election，避免多实例重复扫描。
- DB 原子 claim 覆盖 reminder、commitment、content candidate 和 account scan。
- outbound quota claim 原子化，避免并发下突破分类上限。
- 失败重试策略和死信/人工处理状态。

推荐执行顺序：

```text
dispatch due user reminders
-> dispatch due companion followups
-> scan due accounts for heartbeat candidates
-> dispatch content push candidates
-> dispatch async task results
```

用户提醒优先级最高，但异步任务结果是用户触发任务结果，不应被内容推送或陪伴跟进阻塞。

## 13. 数据与配置调整

建议新增配置：

```text
proactive_quiet_hours_start = "22:00"
proactive_quiet_hours_end = "07:00"
companion_followup_daily_limit = 1
content_push_daily_limit = 1
proactive_avoidance_window_hours = 6
content_push_rejection_cooldown_days = 30
```

当前 `proactive_outbound_daily_limit` 可临时保留为兼容配置，但不应继续作为所有 outbound 类型的统一上限。

建议新增或调整字段：

- `outbound_messages.product_category`
- `outbound_messages.policy_version`
- `outbound_messages.policy_reason`
- `outbound_messages.scheduled_at`
- `reminders.reminder_type`
- `reminders.recurrence_rule_json`
- `reminders.next_due_at`
- `reminders.timezone`
- `reminder_change_requests`
- `content_push_candidates`
- `content_push_preferences`

SQLite 阶段可先用 JSON metadata 承载部分字段，但进入 PostgreSQL 前应把策略查询高频字段结构化。

## 14. Admin 与隐私边界

后台默认视图可以查看：

- outbound 状态、source、product category、policy reason、时间、错误码、Gateway message id。
- reminder 的任务文本摘要、状态、due_at、recurrence metadata。
- proactive state、candidate 状态、confidence、reason 和发送统计。

后台默认视图不能查看：

- 用户完整聊天记录。
- daily notes 正文。
- prompt/messages 全文。
- 模型回复正文。
- 用户语音 ASR 正文和图片识别正文。

明文查看必须遵守 [运营与后台 PRD](../product/admin_ops_prd.md)：管理员可在必要 debug 和事故排查时查看；普通后台用户需要管理员审批后的 2 小时临时明文权限；所有明文查看都记录操作日志。

## 15. 开发切分

建议按以下顺序推进：

1. 类型化 outbound policy：新增 category 映射，修正 reminder 不受 quiet hours、总开关和全局日上限影响。
2. 分类限额：陪伴跟进和内容推送分别每日 1 条，废弃单一全局主动消息上限在策略里的主导地位。
3. 6 小时避让：先覆盖 reminder 压制 commitment/heartbeat，后覆盖 commitment/heartbeat 压制 content push。
4. 周期性提醒：扩展 reminder model 和 parser/classifier，支持最小 1 天周期。
5. 取消/更新提醒：新增 pending change request 和确认处理。
6. 内容推送：新增 candidate、preference、拒绝识别和 1 个月冷却。
7. 异步任务结果补发：接入 `async_task_result` category，和搜索/语音技术设计统一。
8. 生产化：worker lease、用户 timezone、PostgreSQL schema、真实微信端到端联调和回归测试。

## 16. 验收点

- 用户提醒在 quiet hours 内仍按用户设定时间发送。
- 用户提醒不受主动触达总开关和陪伴/内容推送日上限影响。
- 陪伴跟进默认每日最多 1 条，且会避让未来 6 小时内的用户提醒。
- 内容推送默认每日最多 1 条，且会避让未来 6 小时内的用户提醒和陪伴跟进。
- 用户明确拒绝某类内容后，1 个月内不会再次推送该类别。
- 周期性提醒可以创建、发送、计算下一次触发时间，且不支持小于 1 天周期。
- 自然语言取消/更新提醒必须先返回拟调整结果，用户确认后才生效。
- outbound ledger 能区分 `source` 与 `product_category`，并记录 policy reason。
- Gateway 发送失败、重复调度和 worker 重启不会造成重复发送。
- 真实微信端到端链路覆盖 reminder、companion followup、content push 和 async task result。
