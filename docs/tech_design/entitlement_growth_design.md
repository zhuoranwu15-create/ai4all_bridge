# 贝壳、增长与支付后置技术设计

更新时间：2026-06-02

本文承接 [贝壳、增长与支付后置 PRD](../product/entitlement_growth_prd.md)，定义 Phase 1 内测所需的贝壳 wallet/ledger、用量计量、邀请奖励和客服补发技术设计。

Phase 1 的目标是形成最小可信闭环：用户有余额，系统能扣减，运营能补发和排查，邀请奖励能生效。支付和用户购买正式后置，不进入 Phase 1 内测首发范围；后续如开放购买，必须接入同一套账本。

## 1. 设计目标

- 贝壳是 AI4ALL 内部权益额度，不是链上资产，不可提现，不承诺金融属性。
- 钱包余额只能由账本流水推导或同步更新，不能无流水直接改余额。
- 新用户注册默认赠送 1000 个贝壳。
- 邀请新用户注册绑定并发送 3 条有意义消息后，给邀请人暂定发放 1000 个贝壳。
- 聊天按输入 token + 输出 token 计量，默认 `1000 deepseek v4-flash token = 1 贝壳`。
- 其他模型按相对 `deepseek v4-flash` 的价格倍率折算。
- 搜索等工具调用先记录成本事件，再映射到贝壳扣减。
- 成功触发商业搜索 provider 的 `web_search` 暂定固定扣减 5 个贝壳。
- 主动触达首条消息不扣用户贝壳，但要记录平台成本事件；用户回复后的后续链路正常扣减。
- 支付和用户购买正式后置，不阻塞内测发布。

## 2. 范围

本文覆盖：

- wallet / ledger 数据模型。
- 注册赠送、运营发放、补偿发放、邀请奖励。
- LLM token、Search tool/provider、outbound platform cost 的成本事件。
- 成本事件到贝壳扣减流水的映射。
- 邀请码、邀请关系、3 条有意义消息验收。
- 客服/运营补发和误扣排查。
- 支付后置的账本边界和未来价格锚点。

本文不覆盖：

- 搜索 provider、失败体验和成本细节，见 [Web Search 同步工具调用技术设计](search_async_tasks_design.md)。
- 语音输入当前依赖上游转写，不产生后端 ASR 成本；细节见 [语音输入技术设计](voice_input_design.md)。
- Admin 明文权限，见 [隐私与后台访问控制技术设计](privacy_admin_access_control_design.md)。
- 支付 provider 选型、订单、退款和合规细则，后续单独立项。

## 3. 当前代码基线

当前代码已有：

- `daily_usage`：按账号和日期记录消息条数。
- `/admin/accounts/{account_id}/usage`：展示今日和近 7 天消息数。
- `outbound_messages`：主动发送的发送 ledger。
- `entitlement_wallets`：账号级贝壳钱包。
- `entitlement_ledger`：注册赠送、扣减等余额流水。
- `cost_events`：聊天 token 等成本事件。
- 新用户注册赠送 1000 贝壳，并通过幂等键防重复发放。
- 普通聊天成功后按估算 token 写成本事件和扣减流水。
- `/web/me/wallet` 和 `/admin/accounts/{account_id}/wallet` 可查看余额和基础流水。

当前缺口：

- token usage 当前主要是估算，尚未接入 provider 返回的真实 usage。
- 模型价格倍率配置尚未产品化。
- 搜索 provider 调用尚未按固定 5 贝壳写入扣减流水。
- 运营补发和误扣回滚入口尚未完整产品化。
- 无邀请码、邀请关系和拉新奖励。
- 无支付订单和回调；该项已正式后置，不作为 Phase 1 内测首发缺口。

因此，`daily_usage` 继续作为消息次数统计；贝壳余额和计费语义由 wallet / ledger / cost event 承担。

## 4. 核心概念

| 概念 | 含义 |
| --- | --- |
| `entitlement_wallet` | 某个 AI4ALL Account 的贝壳钱包 |
| `entitlement_ledger` | 所有影响余额的发放、扣减、补偿和回滚流水；未来支付生效也必须进入 ledger |
| `cost_event` | LLM、Search、outbound 等资源消耗记录，可映射为用户扣减或平台成本 |
| `model_price_rule` | 模型相对基准模型的价格倍率 |
| `referral_relationship` | 邀请人和被邀请人的拉新关系及奖励状态 |

关键边界：

- `cost_event` 负责记录资源消耗和成本归因。
- `entitlement_ledger` 负责改变用户贝壳余额。
- 平台承担的成本只写 `cost_event`，不写用户扣减 ledger。
- 所有用户扣减都必须能追溯到一个或多个 `cost_event`。

## 5. 金额精度

产品侧叫“贝壳”，技术侧不要用浮点数保存余额。

建议使用 fixed-point：

```text
1 shell = 1_000_000 shell_micros
```

所有表内金额字段使用整数：

```text
amount_shell_micros
balance_shell_micros
```

显示层再转换成贝壳数量。小数贝壳是否展示、最小展示粒度和四舍五入规则仍由产品确认；账本内部先保留足够精度，避免模型倍率和任务成本计算时丢精度。

LLM 聊天扣减公式：

```text
billable_tokens = input_tokens + output_tokens
shell_micros = ceil(billable_tokens * model_price_multiplier * 1_000_000 / 1000)
```

示例：

```text
2500 deepseek v4-flash token
=> ceil(2500 * 1.0 * 1_000_000 / 1000)
=> 2_500_000 shell_micros
=> 2.5 贝壳
```

## 6. 数据模型

### 6.1 entitlement_wallets

```text
entitlement_wallets
- id
- platform_user_id
- ai4all_account_id
- balance_shell_micros
- status: active | frozen | closed
- created_at
- updated_at
```

约束：

- Phase 1 不允许一个产品用户拥有多个 AI4ALL Account，因此默认一个 `platform_user_id` 对一个 active wallet。
- 如果未来支持多 account，wallet 仍按 `ai4all_account_id` 隔离。
- 余额更新必须和 ledger insert 在同一个数据库事务中完成。

### 6.2 entitlement_ledger

```text
entitlement_ledger
- id
- wallet_id
- platform_user_id
- ai4all_account_id
- direction: credit | debit
- amount_shell_micros
- balance_after_shell_micros
- reason
- source_type
- source_id
- idempotency_key
- operator_admin_user_id
- related_ledger_id
- metadata_json
- created_at
```

常见 `source_type`：

| source_type | direction | 说明 |
| --- | --- | --- |
| `new_user_grant` | credit | 新用户注册赠送 |
| `manual_grant` | credit | 运营手工发放 |
| `compensation` | credit | 客服补偿或误扣补偿 |
| `referral_reward` | credit | 邀请奖励 |
| `payment_order` | credit | 后续支付成功后贝壳生效，Phase 1 内测不使用 |
| `usage_charge` | debit | 聊天、搜索等用户消耗 |
| `reversal` | credit/debit | 反向冲正，永远不删除原流水 |

幂等键示例：

```text
new-user-grant-{ai4all_account_id}
usage-message-{message_id}
usage-tool-{tool_invocation_id}
referral-reward-{referral_relationship_id}
payment-order-{order_id}
reversal-{ledger_id}
```

### 6.3 cost_events

```text
cost_events
- id
- ai4all_account_id
- wallet_id
- source_type: message | tool_call | outbound | admin_operation
- source_id
- cost_type: llm_tokens | asr | search_provider | provider_call | outbound_delivery
- cost_owner: user | platform
- billable_to_user: boolean
- provider
- model
- input_tokens
- output_tokens
- duration_seconds
- provider_units
- model_price_multiplier
- computed_shell_micros
- entitlement_ledger_id
- status: pending | charged | waived | failed
- metadata_json
- created_at
```

说明：

- 用户聊天和用户触发搜索工具调用默认 `cost_owner=user`。
- 主动触达首条消息默认 `cost_owner=platform`、`billable_to_user=false`。
- `computed_shell_micros` 是技术计算值，是否实际扣减由 `billable_to_user` 和扣减策略决定。

### 6.4 model_price_rules

```text
model_price_rules
- id
- provider
- model
- base_model: deepseek v4-flash
- price_multiplier
- status: active | disabled
- valid_from
- valid_until
- metadata_json
- created_at
- updated_at
```

规则：

- `deepseek v4-flash` 默认 multiplier 为 `1.0`。
- 不同模型的倍率由后台配置，不暴露给普通用户。
- 每次扣减都把当时生效的倍率写入 `cost_events` 和 ledger metadata，避免后续倍率变化影响历史账。

### 6.5 grant_rules

```text
grant_rules
- id
- rule_type: new_user | referral | campaign | compensation_template
- amount_shell_micros
- status: active | disabled
- starts_at
- ends_at
- metadata_json
- created_at
- updated_at
```

Phase 1 默认规则：

```text
new_user = 1000 贝壳
referral_reward = 1000 贝壳
```

### 6.6 referral_codes

```text
referral_codes
- id
- platform_user_id
- code
- status: active | disabled
- created_at
- updated_at
```

要求：

- 一个用户至少有一个稳定邀请码。
- 注册链接中的邀请码自动填充到 onboarding 页面。
- 非邀请链接进入的用户可以手动输入邀请码。

### 6.7 referral_relationships

```text
referral_relationships
- id
- inviter_platform_user_id
- invitee_platform_user_id
- referral_code_id
- status: pending_registration | registered | bound | qualified | rejected | rewarded
- meaningful_message_count
- review_status: pending | passed | failed
- reward_ledger_id
- metadata_json
- created_at
- updated_at
- rewarded_at
```

约束：

- 不能自邀请。
- 同一个被邀请用户只能绑定一个有效邀请关系。
- 邀请奖励只发一次。
- 发奖使用 `referral-reward-{referral_relationship_id}` 幂等键。

### 6.8 meaningful_message_reviews

```text
meaningful_message_reviews
- id
- referral_relationship_id
- invitee_platform_user_id
- ai4all_account_id
- message_ids_json
- reviewer_type: ai | admin
- status: pending | passed | failed
- reason
- metadata_json
- created_at
```

Phase 1 可以先由后台 AI 自动判断 3 条消息是否有意义，Admin 只提供查看和必要手工修正入口。

### 6.9 orders / payments / refunds

支付和用户购买正式后置。Phase 1 内测不创建以下表、不暴露用户购买入口、不实现订单或回调。以下模型仅作为后续立项时的账本边界参考。

```text
payment_products
- id
- name
- price_cents
- amount_shell_micros
- status
- metadata_json

orders
- id
- platform_user_id
- ai4all_account_id
- payment_product_id
- amount_cents
- amount_shell_micros
- status: pending | paid | cancelled | expired | refunded
- provider
- provider_order_id
- created_at
- updated_at

payments
- id
- order_id
- provider
- provider_payment_id
- status: pending | succeeded | failed
- paid_at
- raw_callback_json
- created_at

refunds
- id
- order_id
- payment_id
- amount_cents
- amount_shell_micros
- status
- reason
- created_at
```

购买计划默认：

```text
1 元人民币 = 100 贝壳
```

支付后续如开放，支付成功后只能通过 `entitlement_ledger(source_type=payment_order)` 发放贝壳。

## 7. 核心流程

### 7.1 注册赠送

```text
手机号 OTP 注册成功
-> create / reuse platform_user
-> create default ai4all_account
-> create entitlement_wallet
-> insert entitlement_ledger credit new_user_grant 1000 贝壳
-> update wallet balance
```

要求：

- 注册接口重试不能重复赠送。
- 幂等键使用 `new-user-grant-{ai4all_account_id}`。
- 如果老用户换手机号后重新注册，按新账号注册赠送处理；旧账号不会自动迁移贝壳。

### 7.2 普通聊天扣减

```text
inbound text
-> identity resolver
-> wallet precheck
-> LLM reply
-> record cost_event(llm_tokens)
-> entitlement_ledger debit usage_charge
-> update wallet balance
```

Phase 1 推荐先采用简单策略：

- 请求开始前检查 wallet active 且余额大于 0。
- 成功生成回复后按实际 token 扣减。
- 如果扣减后余额小于等于 0，下一轮请求提示用户余额不足或需要等待运营补发。
- 暂不做复杂预授权和冻结；搜索等高成本工具调用可以先做最低余额门槛。

失败处理：

- LLM 未调用成功，不扣用户贝壳。
- LLM 已成功返回但发送微信失败，是否扣费进入待定；技术上必须能通过 `message_id`、`debug_trace_id` 和 cost event 排查并手工补偿。

### 7.3 搜索扣减

搜索默认采用同步 `web_search` tool use。长耗时搜索、复杂整理、provider 超时或用户明确要求后台整理时，Phase 1 当前回合返回失败/不支持说明，不创建后台任务。

Phase 1 暂定规则：

- 成功触发商业搜索 provider 的 `web_search` 固定扣减 5 个贝壳。
- 5 个贝壳用于覆盖约 3 分钱人民币的商业搜索 API 成本和少量结果处理成本。
- 搜索所在 turn 的 LLM 工具决策、最终回答 token 仍按普通聊天 token 规则另行扣减。
- 未实际调用商业 provider 的失败搜索，不扣搜索固定费用。
- 已调用商业 provider 但后续回答失败，先记录成本事件；是否补偿由运营/客服处理。

技术上仍需记录多段成本：

```text
tool decision / query rewrite cost
-> search provider cost
-> result summarize cost
-> final answer cost
```

技术要求：

- 每段写 `cost_events`。
- provider 失败、回退和重试都要单独记录。
- 同步工具调用成功后，商业搜索 provider 成功调用产生 `usage_charge`，金额为 5 贝壳。
- 后台整理、长耗时报告和异步补发不进入 Phase 1；不产生任务型汇总扣减。
- 技术上必须区分“未实际消耗 provider/LLM”和“已消耗但结果失败”。

### 7.4 语音转写扣减

当前 Phase 1 不接入后端 ASR，依赖 `openclaw-weixin` 上游提供转写文本：

```text
voice message
-> upstream transcript
-> transcript enters normal text turn
```

要求：

- 当前不产生 ASR cost event，不扣 ASR 贝壳。
- 上游转写文本进入普通聊天后，后续 LLM 回复按聊天 token 扣减。
- 如果未来接入后端 ASR fallback，再补充 `cost_event(asr)`、duration、provider request id 和贝壳扣减规则。

### 7.5 主动触达首条成本

```text
user_reminder / companion_followup / content_push first outbound
-> outbound_messages ledger
-> cost_event(cost_owner=platform, billable_to_user=false)
-> no entitlement_ledger debit
```

用户回复后：

```text
user reply
-> normal chat / search
-> cost_event(cost_owner=user)
-> entitlement_ledger debit
```

Phase 1 不支持用户请求后的异步任务结果补发；主动触达首条成本仅适用于用户提醒、陪伴跟进和内容邀请。

### 7.6 邀请奖励

```text
老用户生成或分享邀请码
-> 新用户打开邀请链接或手动输入邀请码
-> 注册成功，记录 referral_relationship
-> 扫码绑定成功，status=bound
-> 新用户发送消息
-> 满 3 条候选消息后触发 AI meaningful review
-> review passed
-> 发放 referral_reward 1000 贝壳给邀请人
```

反作弊基础约束：

- 不能邀请自己。
- 同一个手机号或 `platform_user_id` 只能作为 invitee 成功一次。
- 邀请奖励发放必须幂等。
- 异常批量注册、重复设备、明显无意义消息刷量先记录到 metadata，Phase 1 可先走人工排查。

### 7.7 运营补发和误扣处理

客服/运营处理不直接改余额：

```text
admin action
-> create support note / reason
-> entitlement_ledger credit compensation 或 manual_grant
-> update wallet balance
-> admin_access_events 记录操作
```

误扣回滚使用 `reversal`：

```text
original debit ledger
-> reversal credit ledger
-> related_ledger_id = original ledger id
```

原流水不能删除或覆盖。

### 7.8 支付后置

Phase 1 内测不开放购买，不实现订单、支付回调或退款。

后续如果开启支付，必须走以下边界：

```text
create order
-> call payment provider
-> receive verified callback
-> mark payment succeeded
-> entitlement_ledger credit payment_order
-> wallet balance updated
```

后续要求：

- 支付回调必须验签。
- 同一个 provider payment id 只能生效一次。
- 支付状态和贝壳发放必须可重放修复。
- 退款必须通过 refund 记录和 ledger 冲正，不直接删购买流水。

## 8. API 与服务边界

建议先在当前仓库内实现最小闭环，模块边界按未来可拆服务组织。

内部服务接口：

```python
ensure_wallet(account_id: str, platform_user_id: str) -> Wallet
grant_shells(wallet_id: str, amount_shell_micros: int, source_type: str, source_id: str, idempotency_key: str) -> LedgerEntry
record_cost_event(...) -> CostEvent
charge_cost_event(cost_event_id: str, idempotency_key: str) -> LedgerEntry | None
get_wallet_summary(account_id: str) -> WalletSummary
```

用户/客服可见 API：

```text
GET /web/accounts/{account_id}/wallet
GET /web/accounts/{account_id}/wallet/ledger
GET /admin/accounts/{account_id}/wallet
GET /admin/accounts/{account_id}/wallet/ledger
POST /admin/accounts/{account_id}/wallet/grants
POST /admin/ledger/{ledger_id}/reverse
GET /admin/referrals
POST /admin/referrals/{id}/review
```

支付后置 API，Phase 1 不实现：

```text
GET /web/payment-products
POST /web/orders
GET /web/orders/{order_id}
POST /payment/callback/{provider}
```

Admin 权限：

- `support` 或 `staff` 可查看余额、流水元数据和邀请状态。
- `operator` 或 `admin` 可补发贝壳、冲正误扣和处理邀请奖励。
- 所有补发、冲正都写 `admin_access_events` 或等价操作日志。支付修复后续立项时同样必须写操作日志。

## 9. 与其他技术文档的关系

- 对话主链路负责产出 LLM usage 和 message/debug trace。
- 搜索技术设计负责产出 tool invocation trace、provider runs 和搜索 cost events。
- 语音技术设计当前不产出 ASR cost events；后续接入后端 ASR fallback 时再补。
- 主动消息技术设计负责区分用户提醒、陪伴跟进和内容推送，并标记首条主动触达的平台成本。
- 隐私与后台访问控制负责后台查看正文、debug trace 和操作日志的权限边界。

权益模块只做三件事：

1. 判断用户是否还有可用权益。
2. 记录资源消耗和成本归因。
3. 通过 ledger 改变余额并支持排查、补偿和冲正。

## 10. 开发切分

建议按以下顺序推进：

1. 新增 fixed-point 金额工具和 `entitlement_wallets` / `entitlement_ledger`。
2. 注册成功后创建 wallet 并发放 1000 贝壳，保证幂等。
3. 新增 `cost_events` 和模型价格倍率配置。
4. 对话主链路接入 token usage cost event 和聊天扣减。
5. Admin 增加 wallet summary、ledger list、手工补发和冲正。
6. 搜索接入 cost events 和 5 贝壳固定扣减。
7. 主动触达首条写平台成本事件，但不扣用户贝壳。
8. 新增邀请码、邀请关系和 3 条有意义消息 AI review。
9. 拉新通过后给邀请人发放 1000 贝壳。
10. 支付后置；后续单独实现 payment products、orders、callbacks 和支付生效 ledger。

## 11. 验收点

- 新用户注册后得到 1000 个贝壳，重复请求不会重复赠送。
- 钱包余额变化都有 `entitlement_ledger`，不存在无流水改余额。
- 普通聊天能按输入 token + 输出 token 和模型倍率生成扣减流水。
- `deepseek v4-flash` 以 `1000 token = 1 贝壳` 扣减。
- 1.5 倍价格模型的 1000 token 扣减 1.5 贝壳。
- 搜索能生成成本事件；成功触发商业搜索 provider 后按 5 贝壳扣减。
- 当前语音依赖上游转写，不产生后端 ASR 成本事件。
- 主动触达首条消息不扣用户贝壳，但能记录平台成本事件。
- 用户回复主动触达后的后续聊天或任务按普通规则扣减。
- 邀请码链接自动填充，用户也可以手动输入邀请码。
- 新用户注册绑定并发送 3 条有意义消息后，能给邀请人发放 1000 贝壳。
- Admin 可以查看余额和流水，补发贝壳，冲正误扣，查看邀请关系。
- 支付不作为 Phase 1 内测验收项；后续如果开启支付，支付成功后只能通过 ledger 发放贝壳，回调重复不会重复发放。

## 12. 待确认

- 贝壳是否为最终正式名称。
- 小数贝壳的用户侧展示方式、最小展示粒度和四舍五入规则。
- 已调用商业搜索 provider 但最终回答失败时的补偿或退回口径。
- outbound delivery 成本如何折算为平台成本事件。
- 拉新奖励是否也给被邀请人额外奖励。
- “3 条有意义消息”的 AI 判断标准、阈值和人工复核边界。
- 支付后续立项时的 provider、套餐结构、退款策略和合规边界。
