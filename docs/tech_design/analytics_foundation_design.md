# 数据分析与指标基建设计

更新时间：2026-06-08

本文定义 AI4ALL 微信 Bot 数据分析建设的指标口径、现有数据可用性、埋点缺口和分阶段基础设施规划。当前阶段先服务运营和产品判断，不进入用户画像正文分析，不读取或导出聊天明文。

## 1. 建设目标

数据分析建设先覆盖四类问题：

1. 增长与活跃：每日注册用户数、活跃用户数、消息条数、次日留存，后续扩展周活跃、7 日留存和贝壳消耗。
2. 主动消息：整体和分类别发送条数、覆盖率、回复率，以及按时间段分析触达表现。
3. Dreaming：每日运行量、成功率、失败原因、产出记忆条目和应用情况。
4. 新用户 onboarding：注册/扫码/首次聊天/完成设置漏斗，以及人设选择分布。

核心目标不是写几条一次性 SQL，而是形成一套稳定口径：

- 指标定义可复用，避免不同报表各算一套。
- 只使用账号级隔离后的元数据，所有查询必须按 `account_id` 或明确的用户维度约束。
- 对历史数据能回算，对未来数据能通过结构化事件补齐。
- 先支持 SQLite 单库离线分析，后续可平滑迁移到 DuckDB、OLAP 或数据仓库。

## 2. 分析不变量

| 不变量 | 要求 |
| --- | --- |
| 账号隔离 | 任一明细事实必须保留 `account_id`；跨账号聚合只能在已脱敏的指标层发生。 |
| 隐私边界 | 默认不分析 `messages.content`、prompt、memory、daily notes 或图片/语音正文；只用时间、状态、类型、来源、成本等元数据。 |
| 时间口径 | 产品指标默认使用北京时间自然日；如用业务日，必须单独命名为 `business_day`。 |
| 调试账号 | 默认指标应排除 `accounts.is_debug = 1`；需要单独看测试数据时显式打开。 |
| 去重口径 | 入站消息以 `messages` 成功落库为准；重复上游事件不会落库，不应再次计数。 |
| 主动消息归因 | 主动发送和用户回复不能只靠 `messages` 粗算，必须以 `outbound_messages` 为触达事实，再用时间窗归因后续入站。 |
| 成本口径 | 贝壳余额变化看 `entitlement_ledger`，资源消耗看 `cost_events`；两者不能混为一谈。 |

## 3. 当前数据基线

现有表已经能直接支撑一部分指标：

| 领域 | 现有数据源 | 可直接计算 | 主要缺口 |
| --- | --- | --- | --- |
| 注册 | `platform_users`、`accounts`、`account_owner_bindings`、`binding_intents`、`channel_bindings` | 注册数、账号数、扫码绑定状态 | 页面曝光、验证码发送/通过、OTP 输入失败、二维码展示/扫码细分步骤缺事件 |
| 活跃和消息 | `messages`、`daily_usage`、`sessions` | DAU、消息条数、首聊日期、次日/7 日留存、session 数 | onboarding 首条触发欢迎时可能被吸收且不写 `messages`，需事件补齐 |
| 贝壳 | `entitlement_wallets`、`entitlement_ledger`、`cost_events` | 消耗、发放、余额、成本类型分布 | provider 真实 token usage 仍可能是估算，搜索等工具成本需按实现进度核对 |
| 主动消息 | `outbound_messages`、`messages.raw_json`、`reminders`、`proactive_commitments`、`content_invitations`、`proactive_account_state` | 发送量、状态、类别、发送时段、内容邀请状态 | 回复率需要归因规则；覆盖率 denominator 需要定义 eligible account |
| Dreaming | `dreaming_runs`、`dreaming_memory_items`、`memory_events`、`scheduler_heartbeats` | 运行量、状态、耗时、条目生成/应用/跳过、scheduler 健康 | token 字段可能为空；输入覆盖量和记忆质量需要后续评估维度 |
| 首次聊天 onboarding | `accounts.onboarding_state`、`accounts.onboarding_updated_at`、账号上下文文件 | 当前状态分布、完成率粗口径 | 状态流转历史、人设选择、跳过原因当前缺结构化事件 |

当前标准库 `data/ai4all.sqlite3` 已有上述核心表。后续实现时不应新增依赖 contacts 表或 `/admin/contacts/*`。

## 4. 指标口径

### 4.1 增长、活跃和留存

| 指标 | 推荐口径 | 数据源 |
| --- | --- | --- |
| 注册用户数 | 当日新增 `platform_users` 数。若产品视角更关注 AI 账号，则同时展示当日新增 `accounts` 数。 | `platform_users.created_at`、`accounts.created_at` |
| 有效绑定用户数 | 当日完成微信绑定的账号数。 | `binding_intents.completed_at` 或 `channel_bindings.first_seen_at` |
| 活跃用户数 DAU | 当日有至少 1 条用户入站消息的 `account_id` 数。 | `messages.direction='inbound' AND role='user'` |
| 消息条数 | 默认指用户入站消息条数；可同时展示 AI 回复条数和总消息条数。 | `messages` |
| 人均消息数 | 用户入站消息条数 / DAU。 | `messages` |
| 首聊用户数 | 当日首次产生入站消息的账号数。 | `MIN(messages.created_at)` |
| 次日留存 | 以首聊日为 cohort，D+1 仍发送用户入站消息的账号数 / cohort 账号数。 | `messages` |
| 周活跃 WAU | 最近 7 个自然日内至少有 1 条用户入站消息的账号数。 | `messages` |
| 7 日留存 | 首聊日 cohort 在 D+7 当天或 D+1 到 D+7 窗口内活跃的比例，需报表标题明确使用哪一种。 | `messages` |
| 贝壳消耗 | 当日 `entitlement_ledger.entry_type='debit'` 的 `amount_shell_micros` 绝对值汇总。 | `entitlement_ledger` |
| 平台资源成本 | 当日 `cost_events` 按 `cost_type/cost_owner/billable_to_user` 汇总。 | `cost_events` |

次日留存建议先采用“首聊留存”，即用户真的发过消息才进入 cohort。这更符合当前“按用户发送消息计算”的要求，也能避免注册后未扫码或未发消息用户稀释聊天体验指标。注册留存可作为单独指标，用注册日作为 cohort。

示例口径：

```sql
-- DAU 与用户入站消息数
SELECT
  date(created_at) AS day,
  COUNT(DISTINCT account_id) AS dau,
  COUNT(*) AS inbound_user_messages
FROM messages
WHERE direction = 'inbound'
  AND role = 'user'
GROUP BY 1
ORDER BY 1;
```

```sql
-- 首聊次日留存
WITH first_seen AS (
  SELECT account_id, MIN(date(created_at)) AS first_day
  FROM messages
  WHERE direction = 'inbound' AND role = 'user'
  GROUP BY account_id
),
activity AS (
  SELECT DISTINCT account_id, date(created_at) AS active_day
  FROM messages
  WHERE direction = 'inbound' AND role = 'user'
)
SELECT
  f.first_day,
  COUNT(*) AS cohort_accounts,
  SUM(CASE WHEN a.account_id IS NOT NULL THEN 1 ELSE 0 END) AS retained_d1,
  ROUND(1.0 * SUM(CASE WHEN a.account_id IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 4) AS d1_retention
FROM first_seen f
LEFT JOIN activity a
  ON a.account_id = f.account_id
 AND a.active_day = date(f.first_day, '+1 day')
GROUP BY f.first_day
ORDER BY f.first_day;
```

### 4.2 主动消息

主动消息分析以 `outbound_messages` 为事实表，不能只用 `messages`。成功发送后系统会把主动消息补进 `messages` timeline，方便后续上下文使用，但运营分析的发送状态、策略拦截、类别和幂等键都在 `outbound_messages`。

| 指标 | 推荐口径 | 说明 |
| --- | --- | --- |
| 主动消息创建数 | `outbound_messages` 当日新增数。 | 含 `pending/sending/sent/failed/blocked`。 |
| 主动消息发送数 | `status='sent'`，按 `sent_at` 或 `quota_date` 汇总。 | 运营看触达建议用 `sent_at`。 |
| 分类发送数 | 按 `product_category` 和 `source` 分组。 | `user_reminder`、`companion_followup`、`reactivation_content_invitation` 等。 |
| 策略拦截数 | `status` 非发送成功且有 `policy_reason`。 | 看 quiet hours、日上限、冷却等策略影响。 |
| 覆盖率 | 当日收到至少一条主动消息的 eligible accounts / 当日 eligible accounts。 | eligible 先定义为 active account + 有可用 `channel_bindings` + `proactive_account_state.enabled=1`；用户提醒可单独算，不受总开关影响。 |
| 回复率 | 主动消息发送后 attribution window 内产生用户入站消息的 outbound 数 / sent outbound 数。 | 默认窗口建议 24 小时；也可看 1h、6h、24h。 |
| 首次回复时长 | `sent_at` 到后续第一条用户入站消息的时间差。 | 用中位数和 P75，比平均数稳定。 |
| 分时段表现 | 按 `strftime('%H', sent_at)` 聚合发送数、回复数、回复率。 | 需要注意样本量，低样本时不做策略结论。 |

回复归因规则建议：

1. 对每条 `outbound_messages.status='sent'` 找同账号 `messages.direction='inbound' AND role='user'` 且 `created_at > sent_at` 的第一条入站消息。
2. 该入站消息必须发生在 attribution window 内，默认 24 小时。
3. 若同账号在窗口内有下一条主动消息，则上一条主动消息的归因窗口截断到下一条 `sent_at`。
4. 用户提醒、陪伴跟进、内容邀请分别计算，不同类别不要混用同一个回复率结论。
5. 内容邀请除通用回复率外，还应看 `content_invitations.status`：`invited -> titles_sent/accepted/declined/expired/rejected_by_policy`。

### 4.3 Dreaming 运行

Dreaming 分析既要看系统健康，也要看产出质量的代理指标。

| 指标 | 推荐口径 | 数据源 |
| --- | --- | --- |
| 每日运行账号数 | 当日有 `dreaming_runs` 的 distinct account。 | `dreaming_runs.created_at` |
| 每日 run 数 | 按 `source_type/status` 汇总。 | `dreaming_runs` |
| 成功率 | `status='succeeded'`；`partial` 单独展示，不能简单归入成功或失败。 | `dreaming_runs.status` |
| 失败率和失败原因 | `status='failed'`，按 `error` 类别聚合。 | `dreaming_runs.error` |
| 运行耗时 | `completed_at - started_at`。 | `dreaming_runs` |
| token 用量 | `token_input/token_output` 汇总。 | `dreaming_runs` |
| 记忆条目数 | 每个 run 生成的 `dreaming_memory_items` 数。 | `dreaming_memory_items` |
| 自动应用率 | `apply_status='applied'` 条目数 / 生成条目数。 | `dreaming_memory_items` |
| 跳过原因 | 按 `skip_reason` 汇总。 | `dreaming_memory_items` |
| scheduler 健康 | `dreaming_scheduler` heartbeat 是否在阈值内、最近错误。 | `scheduler_heartbeats` |

需要避免的误判：

- run 级状态当前主要是 `succeeded/partial/failed`；`skipped` 是 memory item 的应用状态，不是 run 成功状态。
- 自动跳过敏感或低置信条目是预期安全行为，不应只追求应用率越高越好。
- Dreaming 的质量最终需要人工抽检或后续建立质量标注；当前只用元数据做运行健康和产出规模判断。

### 4.4 新用户 onboarding 漏斗和人设

Onboarding 实际包含两段漏斗：

1. Web 注册与扫码绑定：手机号验证、创建平台用户、创建 AI4ALL Account、创建 binding intent、二维码成功、绑定完成。
2. 微信首次聊天 onboarding：`pending -> step1_sent -> step2_sent -> complete/timed_out`。

现有表可支持的粗口径：

| 阶段 | 当前可用口径 | 数据源 |
| --- | --- | --- |
| OTP 记录创建 | 当日 `phone_verifications` 新增。 | `phone_verifications.created_at` |
| OTP 验证成功 | `verified_at IS NOT NULL`。 | `phone_verifications` |
| 平台用户创建 | `platform_users.created_at`。 | `platform_users` |
| 默认账号创建 | `accounts.created_at`。 | `accounts`、`account_owner_bindings` |
| binding intent 创建 | `binding_intents.created_at`。 | `binding_intents` |
| 绑定完成 | `binding_intents.status='completed'` 或 `completed_at IS NOT NULL`。 | `binding_intents` |
| 微信首次聊天开始 | 首条用户入站消息时间。 | `messages` |
| onboarding 当前完成 | `accounts.onboarding_state='complete'`。 | `accounts` |

当前不稳定或缺失的口径：

- `accounts.onboarding_state` 只有当前状态，没有状态流转历史，不能准确还原每一步耗时和中途流失。
- 人设选择写入 `SOUL.md/IDENTITY.md/USER.md`，不适合作为长期结构化分析来源。
- `apply_extracted_onboarding_info` 返回的 `persona/ai_name/user_name` 没有落结构化事件，无法稳定统计“留白、预设、自定义、跳过”分布。
- 首次 inbound 触发欢迎后当前可能 `no_reply=True` 返回且不写 `messages`，会低估 onboarding 第一步触发量。

建议补充事件：

```text
onboarding_events
- id
- account_id
- platform_user_id
- event_name:
  web_otp_requested | web_otp_verified | binding_intent_created |
  binding_completed | first_inbound_seen | welcome_sent |
  step1_answer_received | step2_answer_received |
  persona_selected | onboarding_completed | onboarding_timed_out
- from_state
- to_state
- event_time
- source: web | turn_service | admin | scheduler
- subject_id: phone_verification_id / binding_intent_id / message_id
- properties_json:
  persona, persona_source, ai_name_source, skipped, needs_confirmation,
  channel, error_code
- created_at
```

如果希望保持通用，也可以先建统一 `analytics_events`，用 `event_name` 和 `properties_json` 承载 onboarding 事件。无论采用专表还是通用事件表，必须保留 `account_id`，且不要写入用户原文。

## 5. 数据模型规划

建议采用三层模型，不把报表 SQL 直接写在业务表上长期运行：

### 5.1 ODS：原始业务表

业务表保持当前职责，例如 `messages`、`outbound_messages`、`dreaming_runs`。ODS 层只做只读抽取，不反向影响业务写入。

### 5.2 DWD：明细事实与维表

建议先以 SQL view 或离线脚本生成：

| 名称 | 粒度 | 来源 |
| --- | --- | --- |
| `dim_account` | 每个账号一行 | `accounts`、`account_owner_bindings`、`channel_bindings` |
| `dim_platform_user` | 每个平台用户一行 | `platform_users` |
| `fact_user_message` | 每条用户入站消息一行 | `messages` |
| `fact_assistant_message` | 每条 AI 回复或主动消息 timeline 一行 | `messages` |
| `fact_outbound_message` | 每条主动发送 ledger 一行 | `outbound_messages` |
| `fact_shell_ledger` | 每条贝壳余额流水一行 | `entitlement_ledger` |
| `fact_cost_event` | 每条资源成本事件一行 | `cost_events` |
| `fact_dreaming_run` | 每次 Dreaming run 一行 | `dreaming_runs` |
| `fact_dreaming_memory_item` | 每条 Dreaming 记忆候选一行 | `dreaming_memory_items` |
| `fact_onboarding_event` | 每个 onboarding 状态事件一行 | 新增事件表 |

### 5.3 DWM/ADS：指标汇总

面向报表和看板的宽表：

| 名称 | 粒度 | 典型字段 |
| --- | --- | --- |
| `daily_product_metrics` | 每日全局 | registrations、new_accounts、dau、inbound_messages、d1_retention、shell_debit |
| `daily_account_metrics` | 每日每账号 | inbound_messages、assistant_messages、shell_debit、active_flag |
| `daily_proactive_metrics` | 每日 + 类别 | created、sent、blocked、covered_accounts、replied_1h/6h/24h |
| `hourly_proactive_metrics` | 日期 + 小时 + 类别 | sent、reply_rate、first_reply_latency_p50 |
| `daily_dreaming_metrics` | 每日 | runs、success、failed、items_generated、items_applied、scheduler_status |
| `onboarding_funnel_daily` | cohort date + step | entered、converted、conversion_rate、median_step_latency |

SQLite 阶段可以先用 `scripts/analytics_report.py` 直接输出 CSV/Markdown；当数据量增长后，再把 DWD/DWM 迁到 DuckDB 或云数仓。

## 6. 分阶段路线

### Phase 0：口径确认和离线 SQL

- 新增一组只读 SQL 或脚本，基于 `data/ai4all.sqlite3` 输出增长、主动消息、Dreaming、onboarding 粗口径。
- 明确排除 debug 账号的过滤规则。
- 不新增业务表，不改运行链路。
- 用历史数据回算，确认口径是否符合运营直觉。

### Phase 1：补结构化事件

- 新增通用 `analytics_events` 或 onboarding 专表，优先补 onboarding 状态流转、人设选择、welcome 发送结果。
- 主动消息回复归因可先离线计算，不急于写回业务库。
- 所有事件写入必须幂等，建议用 `idempotency_key` 或业务 subject 去重。

### Phase 2：指标汇总与 Admin 可视化

- 生成 `daily_*_metrics` 汇总表或 materialized snapshot。
- 在 Admin/ops 页面展示核心趋势：DAU、消息、留存、主动触达、Dreaming 健康、onboarding 漏斗。
- 支持按日期范围、账号状态、渠道、主动消息类别过滤。

### Phase 3：外部分析栈

- 当 SQLite 查询开始影响线上或数据量超过单机可接受范围时，使用只读备份导出到 DuckDB/对象存储/数据仓库。
- 保留业务库为事实源，分析系统只读消费。
- 建立每日自动任务和数据质量告警。

## 7. 数据质量检查

首批报表至少应包含以下检查：

- `messages.account_id` 必须存在于 `accounts.id`。
- `messages.direction='inbound'` 的 `role` 应为 `user`，`direction='outbound'` 的 `role` 应为 `assistant`。
- `daily_usage.message_count` 与同日用户入站消息数差异应可解释。
- `outbound_messages.status='sent'` 应有 `sent_at`，并且大多数有对应 `messages.raw_json.outbound_message_id`。
- `content_invitations.outbound_message_id` 应能关联到 `outbound_messages.id`。
- `dreaming_memory_items.dreaming_run_id` 应能关联到 `dreaming_runs.id`，且 `account_id` 一致。
- `entitlement_ledger.wallet_id/account_id/platform_user_id` 应与 `entitlement_wallets` 一致。
- 所有报表默认过滤 debug 账号，并在报表元数据中显示过滤条件。

## 8. 初始报表建议

第一版数据分析文档或日报建议包含：

1. 增长概览：注册用户、完成绑定用户、DAU、首聊用户、入站消息数、人均消息数、贝壳消耗。
2. 留存概览：首聊 cohort 次日留存，样本数低于 10 时标注“仅观察”。
3. 主动消息：按 `product_category/source/status` 的发送漏斗，24h 回复率，分小时表现。
4. Dreaming：每日 run 数、失败数、条目应用/跳过情况、scheduler heartbeat。
5. Onboarding：Web 注册到绑定完成漏斗、首次聊天 onboarding 当前状态分布、人设选择事件补齐后的分布。

## 9. 待确认

- “注册用户数”在运营日报中主口径采用 `platform_users` 还是 `accounts`；建议两者都保留，但 headline 用 `platform_users`。
- 次日留存主口径采用“首聊 cohort”还是“注册 cohort”；当前建议首聊 cohort。
- 主动消息覆盖率 denominator 是否排除最近 24 小时活跃用户、冷却期用户或主动开关关闭用户；建议先使用 eligible account，并在报表说明中拆出 excluded reason。
- 内容邀请的“回复”是否只算用户明确确认/拒绝，还是任何窗口内入站都算回复；建议两套都看：通用回复率和业务确认率。
- 人设选择分析是否需要区分“用户直接选预设”“改名后的预设”“自定义”“跳过/留白”。
