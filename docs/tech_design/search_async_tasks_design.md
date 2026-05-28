# 搜索与异步任务技术设计

更新时间：2026-05-28

本文承接 [搜索与异步任务 PRD](../product/search_and_async_tasks_prd.md)，定义 Phase 1 Web Search 和高耗时任务的技术设计。

语音输入有独立技术文档，见 [语音输入技术设计](voice_input_design.md)。两者可以复用任务、worker、provider adapter、成本事件和 outbound result delivery，但产品流程和验收分别维护。

## 1. 设计目标

- Web Search 默认异步执行，不在普通聊天同步路径中等待搜索结果。
- 用户触发搜索后先收到快速确认，任务完成后再补发完整结果。
- 默认搜索 provider 为 DuckDuckGo，失败后回退 Tavily / Kimi 搜索。
- 所有搜索任务都有状态、幂等、provider trace、成本事件和失败体验。
- 任务结果补发复用 outbound ledger 和 OpenClaw Gateway send，但不算无触发主动推送。

## 2. 范围

本文覆盖：

- 搜索意图识别后的任务创建。
- 搜索 provider adapter。
- 异步任务状态机。
- worker 执行和 provider 回退。
- 搜索结果整理、引用和最终答案生成。
- 任务完成后的微信补发。
- 搜索成本事件和贝壳扣减所需数据。

本文不覆盖：

- 微信语音媒体获取和 ASR，见 [语音输入技术设计](voice_input_design.md)。
- 主动消息策略，见 [主动消息与提醒设计](proactive_messaging_design.md)。
- 贝壳 wallet/ledger 细则，见 [贝壳、增长与可选支付技术设计](entitlement_growth_design.md)。

## 3. 用户体验链路

```text
user message
-> intent gate 判断为 web_search
-> 同步回复确认
-> create task
-> worker claim task
-> provider search
-> result clean / rank / summarize
-> final answer build
-> outbound result delivery
-> user receives final answer
```

同步确认示例：

```text
我先帮你查一下，整理好后发你。
```

失败补发示例：

```text
刚才这次搜索没成功，可能是搜索服务暂时不可用。你可以稍后再试，或者换个关键词发我。
```

## 4. Intent Gate

搜索触发应在 LLM 主回复前进入 intent gate。

命中条件：

- 用户明确要求“查一下”“搜一下”“最近/最新”“网上有没有”等信息检索。
- 当前问题需要实时或外部信息，不能仅靠模型静态知识回答。
- 不属于明显高风险或不支持任务。

处理规则：

- 搜索场景默认全部异步。
- 命中后不进入普通聊天 LLM 生成完整答案。
- 同步回复只做确认，不假装已经搜索。
- 创建 task 时记录触发消息、原始 query、会话、账号和幂等键。

## 5. 数据模型

建议新增 `tasks`：

```text
tasks
- id
- account_id
- session_id
- trigger_message_id
- task_type: web_search
- status: pending | running | succeeded | failed | cancelled
- payload_json
- result_json
- idempotency_key
- created_at
- started_at
- finished_at
- error
```

建议新增 `task_runs`：

```text
task_runs
- id
- task_id
- provider
- attempt
- status: running | succeeded | failed
- request_json
- response_json
- started_at
- finished_at
- latency_ms
- error
```

建议新增 `cost_events` 或等价记录：

```text
cost_events
- id
- ai4all_account_id
- source_type: task
- source_id
- cost_type: llm_tokens | provider_call | outbound_delivery
- cost_owner: user | platform
- billable_to_user
- provider
- model
- input_tokens
- output_tokens
- units
- computed_shell_micros
- entitlement_ledger_id
- status: pending | charged | waived | failed
- metadata_json
- created_at
```

`computed_shell_micros` 是按 fixed-point 计算的贝壳成本；具体金额精度和换算规则见 [贝壳、增长与可选支付技术设计](entitlement_growth_design.md)。

## 6. Provider 策略

Phase 1 provider：

| provider | 用途 | 默认策略 |
| --- | --- | --- |
| DuckDuckGo | 默认搜索 | 首选 |
| Tavily | 搜索 API 回退 | DuckDuckGo 失败或结果不足时尝试 |
| Kimi Search | 搜索/摘要能力回退 | Tavily 不可用或内部配置启用时尝试 |

配置要求：

- provider key、额度、启用状态由内部研发或运营配置。
- 用户侧不暴露 provider 选择过程。
- 每次 provider 调用都记录 provider、query、耗时、状态和失败原因。
- provider 回退必须写入 `task_runs`，避免成本和失败原因不可追踪。

失败判定：

- provider 调用异常。
- 超时。
- 返回空结果。
- 结果质量明显不足。
- provider 被配置禁用或额度不足。

## 7. Worker 与幂等

worker 要求：

- 原子 claim `pending -> running`。
- 同一 task 不允许多个 worker 同时执行。
- task timeout 后可进入 failed 或重试，重试必须产生新的 `task_runs`。
- 任务成功后只能补发一次结果。
- outbound 补发使用稳定 idempotency key，例如 `task-result-{task_id}`。

Phase 1 可以先单 worker；进入多 worker 前需要 DB 原子 claim 和 worker lease。

## 8. 搜索结果处理

处理步骤：

1. query normalize / rewrite。
2. provider search。
3. 结果去重和排序。
4. 过滤低质量、明显不相关或高风险内容。
5. 生成最终回答。
6. 生成来源摘要或引用。

回答要求：

- 不编造来源。
- 对不确定信息说明不确定性。
- 对新闻类结果保留时间意识。
- 对医疗、法律、金融等话题遵守 Safety 边界。
- 如果结果不足，应明确说明“没有找到可靠信息”。

搜索引用格式仍待产品确认；技术上需要保留 title、url、snippet、published_at 或 retrieved_at。

## 9. 结果补发

任务完成后使用 outbound ledger：

```text
source = async_task_result
product_category = task_result
idempotency_key = task-result-{task_id}
```

策略：

- 不受主动触达总开关影响。
- 不占陪伴跟进或内容推送日上限。
- 默认不因 quiet hours 丢弃。
- Gateway 发送失败时，task 可标记为 `failed_delivery` 或保留 delivery retry 状态。

## 10. 成本与贝壳

搜索任务需要记录成本明细：

- 意图识别和 query rewrite 的 LLM token。
- provider API 成本。
- 结果清洗、排序、摘要的 LLM token。
- 最终答案生成的 LLM token。
- provider 失败后的回退和重试成本。
- outbound 补发成本。

搜索失败是否扣费由权益规则决定；技术上至少要能区分：

- 未实际调用 provider 就失败。
- 已调用 provider 但无结果。
- 已调用 provider 且消耗 LLM 摘要后失败。
- 结果成功生成但补发失败。

## 11. 与语音输入的共用底座

可复用：

- `tasks` / `task_runs` 状态机。
- provider adapter 接口。
- worker claim / retry / timeout。
- cost events。
- outbound result delivery。

不合并：

- 搜索的 provider、query、引用和事实性要求独立。
- 语音的媒体获取、60 秒限制、ASR provider 和转写文本处理独立。

## 12. 开发切分

1. 新增 task/task_run 基础模型。
2. 在 intent gate 中识别 web_search，并创建 task。
3. 同步回复搜索确认。
4. 实现 DuckDuckGo adapter。
5. 实现 Tavily / Kimi 回退 adapter。
6. 实现 worker 和 provider run 记录。
7. 实现结果整理和最终答案生成。
8. 接入 outbound result delivery。
9. 记录 cost events。
10. 补齐失败补发和幂等测试。

## 13. 验收点

- 搜索触发后用户先收到确认回复。
- 搜索任务默认异步执行。
- DuckDuckGo 是默认 provider。
- DuckDuckGo 失败时能回退 Tavily / Kimi 搜索。
- 任务完成后用户收到完整结果。
- 失败或超时时用户收到失败说明。
- 同一任务不会重复补发。
- task/task_run 能记录 provider、耗时、状态和失败原因。
- 搜索任务能生成成本事件，并可映射到贝壳扣减。
