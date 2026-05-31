# Conversation Orchestrator 主对话场景技术设计

更新时间：2026-05-31

## 1. 文档定位

本文是 AI4ALL Phase 1 主对话场景的主技术设计，定义一条微信私聊入站消息如何被 Conversation Orchestrator 处理：身份解析、active session、messages、Intent Gate、Prompt/Context、Tool Use、LLM 回复、同步返回、after-turn 动作，以及与异步任务和主动消息的接口。

本文不作为临时 roadmap，而是主场景实现口径。它学习 OpenClaw 的 agent loop、prompt block、tool schema、Dreaming、heartbeat 和 trace 思路，但落地边界以 AI4ALL 一对多后端服务为准。

它承接以下产品需求：

- [陪伴式聊天 PRD](../product/companion_chat_prd.md)
- [记忆与上下文 PRD](../product/memory_prd.md)
- [搜索与异步任务 PRD](../product/search_and_async_tasks_prd.md)
- [主动消息与提醒 PRD](../product/proactive_prd.md)
- [语音输入 PRD](../product/voice_prd.md)

Context Files、active session 生命周期、daily notes、Dreaming 和 Memory 细节见 [Agent Context Files 与记忆机制设计](agent_context_files.md)。主动消息、reminder、commitment、heartbeat、outbound policy 和 outbound 写入 `messages` 的细节见 [主动消息与提醒设计](proactive_messaging_design.md)。

Web Search 的 tool schema、provider、同步/异步边界、结果补发和成本事件细节见 [搜索与异步任务技术设计](search_async_tasks_design.md)。微信语音、豆包 ASR、60 秒限制和转写链路细节见 [语音输入技术设计](voice_input_design.md)。贝壳 wallet/ledger、成本事件映射、邀请奖励和可选支付见 [贝壳、增长与可选支付技术设计](entitlement_growth_design.md)。

## 2. 设计目标

Phase 1 编排目标：

- 让微信私聊普通 turn 足够短、稳定、可观测。
- 将陪伴式聊天作为默认路径，将提醒、搜索、ASR、长内容整理等能力清晰分流。
- Web Search 对齐 OpenClaw 风格的 LLM tool use：普通搜索同步执行，长耗时搜索或复杂整理才快速确认并异步补发。
- 将 OpenClaw 的 prompt/context 分层、Dreaming、tool schema、heartbeat、trace 思想转化为 AI4ALL 一对多服务架构。
- 每个用户以 `ai4all_account_id` 为业务隔离主键，不把 OpenClaw 原生 workspace 或 channel account 当作业务状态中心。
- 为内测阶段的成本、权益扣减、Admin 排障和客服支持预留 trace 与审计。

当前不做：

- 复杂多 Agent 编排。
- 子代理派发和跨 agent 记忆共享。
- 用户自定义工具市场。
- 让同步聊天路径执行长工具链。
- 在真实 provider 和失败体验未确定前，让模型承诺可搜索、可下单或可修改外部系统。

## 3. 当前实现状态

已实现：

- `app/turn_service.py` 承载 `/openclaw/turn` 主业务链路。
- `app/prompt_builder.py` 结构化组装 prompt，并注入账号级 Project Context。
- `app/user_profiles.py` 管理 `AGENTS / SOUL / IDENTITY / USER / TOOLS / MEMORY`。
- `app/memory_writer.py` 在回复后异步写 daily notes。
- `app/dreaming.py` 提供手动 Dreaming 入口。
- Debug trace 支持 AI4ALL prompt/messages/reply 记录。
- Bridge 支持测试账号 OpenClaw shadow trace / prompt 对比。
- 显式一次性提醒、hidden commitment、heartbeat candidate 和 proactive scheduler 已有基础代码。

仍需补齐：

- 固化 Conversation Orchestrator 边界，进一步减少 HTTP route、DB、LLM、工具判断混杂。
- 统一 `tasks` / `task_runs`，支撑 Web Search 异步兜底、ASR、长内容整理和失败补发。
- Search Provider、ASR Provider、内容源 Provider 的 provider adapter 和成本记录。
- tool registry / task registry，区分“模型可见能力说明”和“后端真实可执行工具”。
- token usage、provider request id、成本、权益扣减和 trace 的统一记录。
- 陪伴体验回归集和长任务体验验收集。

## 4. P0 主对话实现切片

P0 的目标是尽快把主对话链路稳定下来，同时不把 Search、ASR、完整权益扣减、复杂主动消息策略等后续能力提前塞进同步路径。

### 4.1 P0 范围

P0 必须交付：

- 微信文本入站进入 `/openclaw/turn`。
- 通过 identity resolver 得到 `ai4all_account_id`，不直接使用 OpenClaw payload 原生 `account_id` 做业务隔离。
- 获取或创建当前账号 active session。
- 入站用户消息写入 `messages`，并带账号、session、channel binding、message id 和 raw metadata。
- 消息去重、账号状态检查和基础限流。
- Intent Gate v0：pending state 优先、高置信一次性提醒、明确后台/长任务请求的快速确认或能力占位，其余进入普通聊天与可用工具链路。
- Prompt assembly：读取 active session 最近消息、账号 Context Files、`MEMORY.md` 和必要 runtime metadata；P0 明确不读取 daily notes 注入 prompt。
- 调用 LLM，保存 assistant reply、usage/trace metadata，并通过 Bridge synthetic reply 同步返回微信。
- after-turn 异步写 daily notes raw material 作为静态事实归档；不做 LLM memory extraction，长期记忆晋升交给 Dreaming 机制；hidden commitment extraction 可以保留为后置异步动作，失败不影响用户回复。

P0 暂不交付：

- 真实 Web Search provider 和完整 task worker。
- 微信语音 ASR。
- 完整 wallet/ledger 扣减。
- 内容推送、完整 heartbeat 自动发送和复杂 proactive policy。
- 每日 4 点 Dreaming 自动调度、500 轮自动 session 压缩和 memory candidate 审核 UI。
- 多 Agent、子代理派发和用户自定义工具市场。

### 4.2 P0 数据写入口径

`messages` 是用户可见对话时间线和后续 prompt 上下文的主表：

- 每条实际收到的用户入站消息都写入 `messages`。
- 每条实际发送给用户的 assistant 消息都写入 `messages`，包括同步 LLM 回复、提醒、陪伴跟进、内容推送和异步任务结果补发。
- `outbound_messages` 只负责发送幂等、策略、重试和 Gateway 状态；不能替代 `messages`。
- pending、cancelled、failed 且未实际发出的 outbound 不写入 `messages`。
- 当前代码里的 `account_id` 暂按 `ai4all_account_id` 兼容处理；新增代码和注释优先使用 `ai4all_account_id`。

active session 口径：

- session 的核心边界按 AI4ALL Account，不按 OpenClaw `session_key`；`session_key` 只是 channel route、兼容和排障字段。
- 每个 AI4ALL Account 默认有一个 active session。
- 如果写入 user/assistant message 时没有 active session，则先为该账号创建 active session。
- P0 不因长时间不聊天自动关闭 session。
- 每日 4 点后的下一次入站会关闭旧 session 并开启新 session；500 轮上限同样按下一次入站懒切换。
- 当前 carryover 是 deterministic excerpt，用于保证新 session prompt 不完全断上下文；LLM 压缩摘要和 4 点主动调度后续补齐。

P0 代码状态：

- 已移除普通聊天 prompt 中对 daily notes 的默认读取和注入。
- 已将 after-turn 记忆写入从 LLM extraction 调整为 raw daily notes append。
- 已在现有 `sessions(account_id, session_key)` 兼容结构下，收敛主上下文读取到账号级 active session；后续迁移为显式 account-level active session 字段或表结构。
- 已补齐 session lifecycle P0 字段、业务日懒切换、最大轮次懒切换、`close_reason` 和 carryover prompt 注入。

### 4.3 Intent Gate v0

P0 不对每条消息额外调用一次 LLM 做总分类。

执行顺序：

1. Pending-state gate：如果账号存在待确认的提醒取消/更新、任务确认等状态，优先处理。
2. Explicit reminder gate：高置信时间 + 事项才创建提醒；歧义场景追问。
3. Long-task gate：明确“慢慢整理”“做一份报告”“整理好再发我”等后台或长耗时请求，先快速确认；真实 worker 未接入前只能进入占位或 stub。
4. Normal chat + tool use：其余消息走普通聊天 LLM 回复；`web_search` 只有在真实 provider、失败体验和成本记录接入后才作为 tool schema 暴露给模型。

搜索不采用纯字符串匹配或独立正则 intent gate 作为主路径。后续可以增加轻量 LLM classifier 提升提醒、任务等状态型能力的召回，但它只能产出 intent candidate，不能绕过代码级校验、幂等、权限、成本和用户确认。

### 4.4 P0 最小测试清单

- 两个 `ai4all_account_id` 的消息、Context Files、session、trace 不串线。
- 同一账号连续多轮对话进入同一个 active session。
- 入站 user message 和同步 assistant reply 都写入 `messages`。
- after-turn raw daily notes 写入只归档已经写入 `messages` 的用户消息和已发送 assistant 消息，不额外改写 session 对话事实。
- 高置信一次性提醒在 LLM 主回复前被 Intent Gate 分流，并返回确定性确认。
- P0 provider 未接入时，普通聊天不会声称已执行 Search、ASR 或长工具链。
- 已发送 outbound assistant message 写入 active session `messages`，用户后续回复能在最近上下文中看到它。
- after-turn daily notes 写入失败不影响用户已收到的回复。

## 5. OpenClaw 借鉴点

OpenClaw 对 AI4ALL 有价值的部分：

- Agent loop：输入、context assembly、tool use、LLM、memory、after-turn action 的清晰分层。
- Prompt block：稳定系统规则、Project Context、runtime metadata、output directives 分区。
- Workspace files：AGENTS、SOUL、IDENTITY、USER、TOOLS、MEMORY。
- Memory：daily notes、长期记忆和 Dreaming。
- Tool schema：工具名称、参数、权限、失败结果和 trace。
- Cron/heartbeat：状态可恢复、低打扰、可跳过的主动循环。
- Debug trace：能解释模型为什么这样回复。

不能照搬的部分：

- OpenClaw 是一对一本地实例；AI4ALL 是一对多后端服务。
- OpenClaw 的 agent workspace 不能作为 AI4ALL 用户状态源。
- OpenClaw heartbeat 不能变成一个全局用户循环，必须按账号隔离执行。
- OpenClaw 工具清单不能直接暴露给普通用户微信 bot。
- AI4ALL 需要额外处理权益扣减、通道风险、客服和运营审计。

## 6. Conversation Turn 总体流程

普通文本 turn：

```text
Inbound WeChat Message
-> OpenClaw / Bridge
-> /openclaw/turn
-> identity resolve
-> account policy / disabled / limits / entitlement precheck
-> message dedupe
-> intent gate
   -> explicit reminder: rule-based create reminder + confirmation
   -> long task: quick acknowledgement + create task
   -> normal chat: context assembly + enabled tool schema + LLM tool use/reply
-> persist reply / usage / trace
-> Bridge synthetic reply -> WeChat
-> after-turn: daily notes / hidden commitment extraction / metrics
```

核心原则：

- 同步路径只做低延迟工作；普通 Web Search 属于低延迟工具调用，长耗时搜索不在当前 turn 硬等。
- 高耗时任务进入异步任务，并通过 Gateway send 补发结果。
- after-turn 动作失败不影响用户已收到的回复。
- 所有步骤必须带 `ai4all_account_id`。

## 7. Intent Gate

Intent Gate 位于限流和去重之后、LLM 主回复之前。

Phase 1 不采用“每条消息都先额外调用一次 LLM 做总分类”的默认方案，也不只依赖简单正则。Intent Gate 负责 pending state、提醒、显式后台任务等会直接改变后端状态的场景；搜索的主触发机制是 LLM 看到 `web_search` tool schema 后自然调用工具。

1. Cheap deterministic gate：先用状态机、结构化规则、时间解析器和高置信 parser 处理明确提醒、取消/更新、后台整理等场景。
2. Pending-state gate：如果账号有待确认的提醒取消/更新、任务确认或其他 pending interaction，优先处理该状态，不进入普通聊天。
3. Optional LLM classifier：只在规则无法判断、且该能力值得额外延迟和成本时调用轻量 LLM 分类器；分类器只产出结构化 intent candidate，不直接写业务状态。
4. Validation and confirmation：任何会创建 DB 状态、发起任务、取消提醒或触发外部动作的 intent，都必须经过代码级结构化校验；低置信或有歧义时向用户追问确认。

因此，P0/P1 的默认落地顺序是“高置信规则优先，LLM 辅助后置”。LLM classifier 可以提升召回，但不能替代幂等、权限、成本、风险和字段校验。`web_search` 不走纯字符串 intent gate；Backend 只有在真实 provider 和失败体验接入后才把工具暴露给模型。

### 7.1 显式提醒

命中条件：

- 用户表达明确时间和明确事项。
- 置信度足够高。
- 不涉及医疗、法律、金融等高风险承诺。

处理：

- 在 LLM 主回复前创建 one-shot reminder。
- 回复用户创建结果或要求补充具体时间。
- 到期发送由 proactive scheduler 处理。

当前提醒识别为规则优先。后续可以增加 LLM 辅助解析，但写入 DB 前仍需要结构化校验。

### 7.2 高耗时任务

典型任务：

- 长耗时 Web Search 或深度资料整理。
- 复杂资料整理。
- 长内容生成。
- 长语音 ASR。
- 多步骤 provider 调用。

处理：

1. 当前 turn 快速回复确认。
2. 创建 `task`，记录触发消息、账号、会话、工具输入、幂等键。
3. Worker 执行 provider 调用和总结。
4. 完成后写 `outbound_messages` ledger。
5. 通过 OpenClaw Gateway send 补发完整结果。
6. 失败或超时也补发简短说明。

这类补发是“用户请求结果投递”，不等同无触发主动推送，但仍必须有状态、幂等和发送记录。

普通 Web Search 不在这里用字符串规则硬触发。模型在普通聊天链路中看到 `web_search` schema 后决定是否调用；工具 handler 根据同步预算、provider 状态和用户是否要求后台整理，决定同步返回结果或创建异步兜底任务。

### 7.3 普通聊天

普通聊天是默认路径：

- 读取最近消息。
- 读取账号 Context Files。
- 读取 `MEMORY.md`；P0 不读取 daily notes 注入 prompt。
- 构建 prompt。
- 调 LLM。
- 保存回复、usage、trace。
- 异步触发 daily notes 和 hidden commitment extraction。

## 8. Prompt 与 Context Assembly

Phase 1 prompt 由稳定前缀和动态上下文组成。当前 `PromptBuilder` 已实现结构化拼装，后续可以继续贴近 OpenClaw 的 block 思路，但不要求完全复制 17-block。

建议逻辑分区：

| 分区 | 内容 | 稳定性 |
| --- | --- | --- |
| Tooling / capability | 真实可用工具 schema 或空 | 低频变化 |
| Execution bias | 回复纪律和任务完成偏好 | 稳定 |
| Safety | 全局安全边界 | 稳定 |
| Skills / product capability | 产品能力摘要 | 低频变化 |
| Project Context | AGENTS、SOUL、IDENTITY、USER、TOOLS、MEMORY | 账号级变化 |
| Daily notes | 今天/昨天或检索结果 | turn 级变化 |
| Override | 运营临时覆盖 | 账号级变化 |
| Output directives | 微信文本格式和风格 | 低频变化 |
| Runtime | 日期、模型、环境元数据 | turn 级变化 |

关键规则：

- Safety 不能被运营覆盖绕过。
- 有 Project Context 时，不重复注入旧 `user_profile.md` section。
- `TOOLS.md` 只描述能力边界，不等同可执行 tool schema。
- Daily notes 不应全量无限注入，后续需要检索和 token budget。
- Debug trace 必须记录 prompt block metadata，便于排查。

## 9. 工具体系边界

AI4ALL 需要区分三层：

| 层 | 作用 | 示例 |
| --- | --- | --- |
| 能力说明 | 告诉模型当前产品能做什么、不能做什么 | `TOOLS.md` |
| Tool schema | 模型本轮可调用的结构化工具 | `web_search(query)`、`get_datetime()` |
| Task registry | 后端可异步执行和补发的任务 | `web_search_task`、`asr_task`、`content_summary_task` |

原则：

- `TOOLS.md` 不能让模型声称可以调用未接入工具。
- tool schema 只在后端确实可执行、权限和失败体验明确时注入。
- 普通低延迟工具可以在同步 turn 内执行；长耗时工具调用、深度整理或 provider 超时才转成 async task。
- 每个工具必须有权限边界、成本计量、失败结果和 trace。
- 用户可见结果必须来自后端执行结果，不能由模型编造“我已经搜索到了”。

Phase 1 首批能力建议：

| 能力 | 编排方式 | 状态 |
| --- | --- | --- |
| LLM 回复 | 同步主链路 | 已有 |
| 当前日期时间 | 同步 runtime 注入或简单工具 | 待整理 |
| 明确一次性提醒 | 规则链路，非 LLM tool | 已有基础 |
| Web Search | LLM tool use，同步默认；长耗时场景转 async task + provider + 补发 | 待实现 |
| ASR | 短语音可同步，长语音走 task | 待实现 |
| 内容推送生成 | 后台候选 + proactive policy | 待实现 |
| memory search/get | 后续工具，Phase 1 可先不开放给模型 | 待定 |

## 10. 异步任务设计

目标模型：

```text
tasks
-> task_runs
-> provider adapter
-> final answer builder
-> outbound_messages
-> Gateway send
```

`tasks` 记录用户可感知任务：

- `ai4all_account_id`
- `session_id`
- `trigger_message_id`
- `task_type`
- `status`
- `payload_json`
- `idempotency_key`
- `scheduled_at / started_at / finished_at`
- `error`

`task_runs` 记录执行尝试：

- `task_id`
- `worker_id`
- `attempt`
- `status`
- `provider_request_json`
- `output_json`
- `error`

执行要求：

- claim 必须原子化，支持未来多 worker。
- 同一任务不能重复补发。
- 超时和失败必须有用户可理解的补发说明。
- outbound ledger 作为最终发送幂等层。
- 搜索、ASR、内容源都要产生成本记录，后续接入权益扣减。

## 11. 记忆与 After-Turn 编排

普通聊天回复成功后的后台动作：

```text
persist assistant reply
-> async daily memory extraction
-> hidden commitment extraction
-> metrics / trace / usage
```

原则：

- daily notes 写入不阻塞用户回复。
- hidden commitment 只产生候选或 pending work，不直接绕过主动消息策略。
- Dreaming 从 daily notes 和候选记忆中保守蒸馏长期记忆。
- 长期记忆写入必须可追溯、可回滚。
- 用户明确纠错优先级高于自动 Dreaming。

Dreaming 当前保留手动入口；正式内测前建议改为 candidate diff + review。

## 12. 主动消息与 Heartbeat 编排

主动消息分三类：

| 类型 | 来源 | 是否用户触发 | 发送约束 |
| --- | --- | --- | --- |
| reminder | 用户明确请求 | 是 | due time + outbound ledger |
| async task result | 用户触发任务 | 是 | task status + idempotency + outbound ledger |
| heartbeat / content push | 系统候选 | 否或弱触发 | proactive state + quiet hours + category daily limit + 6h avoidance + cooldown |

heartbeat 机制必须按账号执行：

```text
system scheduler
-> list due accounts
-> per-account state claim
-> read USER/MEMORY/daily notes/candidates
-> decide no-op or outbound
-> outbound ledger + Gateway send
```

禁止：

- 读取全局 `HEARTBEAT.md` 作为用户个人任务来源。
- 在用户未开启或无高置信候选时发送 heartbeat。
- 把内容推送混入普通聊天主链路。

## 13. Debug Trace 与 OpenClaw 对比

Trace 必须服务两个目标：

- 对内排障：解释某一轮为什么这样回复。
- 对标 OpenClaw：比较 prompt/context/tool/runtime 差异。

AI4ALL trace 应记录：

- `ai4all_account_id`、session、message id。
- intent gate 判断结果。
- prompt block metadata。
- Context Files metadata。
- LLM messages、model、latency、error。
- tool/task 创建和 provider 调用摘要。
- 最终回复和发送状态。

OpenClaw shadow trace 只用于测试账号：

- 用户只看到 AI4ALL 回复。
- OpenClaw native run 的 prompt/messages/native reply 可回传到 AI4ALL debug trace。
- 需要严格白名单，避免额外成本和双回复风险。

## 14. 能力演进顺序

### P0：聊天编排稳定

- 明确 Conversation Orchestrator 边界。
- 稳定 prompt/context 注入和 debug trace。
- 完成基础限流、去重、usage 记录。
- 建立陪伴式回复质量样例集。

### P1：工具、任务和记忆产品化

- Web Search tool use：普通搜索同步返回；长耗时搜索或复杂整理先确认、后台执行、补发结果。
- ASR 入站闭环：语音转写后进入文本链路。
- Dreaming candidate diff + review。
- 用户显式纠错后的 memory/context update candidate。
- 主动消息和内容推送接入用户偏好、拒绝识别和冷却。

### P1.5：权益、成本和运营闭环

- LLM / ASR / Search 消耗计量；主动触达首条记录平台成本事件，不扣用户贝壳。
- 将工具调用、任务执行和权益扣减流水关联。
- Admin 支持任务、trace、权益和客服排查。
- 可选支付开启时，把购买权益纳入同一 ledger。

## 15. 暂缓能力

以下能力暂缓，避免过早进入复杂局部：

- 子代理和多 Agent 编排。
- 自动 context compaction。
- 向量 memory search。
- 用户自定义 tool / skill 市场。
- 复杂任务规划器。
- 自动无审核长期记忆改写。

恢复条件：

- 出现真实长对话 token 压力，再做 compaction。
- Web Search、ASR、内容推送稳定后，再做更通用的 tool runner。
- 内测数据证明记忆质量可评估后，再扩大 Dreaming 自动化。

## 16. 决策记录

- 2026-05-17：不追 OpenClaw 多 agent 编排。产品场景是一对一私聊陪伴，先把单 agent 的记忆、工具、人格做扎实。
- 2026-05-17：记忆写入使用异步后台任务，不阻塞回复。用户等待时间优先，失败可通过日志和后续补写处理。
- 2026-05-17：Safety 区块全局固定，运营覆盖分离。安全边界不能由单账号覆盖绕过。
- 2026-05-18：账号级 `HEARTBEAT.md` 从 Context Files 移出。用户主动触达和提醒单独建模。
- 2026-05-24：Phase 1 目标调整为正式内测版本，P0/P1/P1.5 纳入同一 Phase；支付购买可选，不阻塞内测。
- 2026-05-24：长耗时任务必须先快速确认，再异步补发完整结果。
- 2026-05-24：记忆机制必须学习 OpenClaw Dreaming，但写入要账号隔离、可追溯、可回滚。
- 2026-05-31：Web Search 与 OpenClaw 对齐为 LLM tool use；普通搜索同步完成，异步任务只作为超时、深度整理、用户明确后台整理等场景的兜底；不采用纯字符串匹配作为主触发机制。

## 17. 验收标准

- 普通文本 turn 可以完成身份解析、去重、限流、context assembly、LLM 回复和 trace。
- 明确提醒请求不会进入长工具链，能创建 one-shot reminder 并回复确认。
- Web Search 由模型通过 tool use 自然触发，普通搜索在当前 turn 同步返回带来源边界的回答；长耗时搜索进入异步兜底后，用户先收到确认回复，任务完成后收到完整结果，失败时收到失败说明。
- 同一任务不会重复补发，补发写入 outbound ledger。
- Prompt trace 能展示 Project Context、runtime、override 和模型输入；如果未来某场景按需装载 daily notes 或检索结果，也必须在 trace metadata 中明确记录。
- 记忆写入、hidden commitment 和 heartbeat 不阻塞同步聊天回复。
- 工具或任务未接入时，模型不会承诺已经完成对应动作。

## 18. 待确认问题

- 搜索引用格式和失败话术。
- 语音媒体保留策略和 ASR 失败体验细节。
- 普通 Web Search 的同步时间预算。
- 除普通 Web Search 和短语音 ASR 可同步尝试外，其他高耗时任务的同步/异步边界。
- tool schema 是否完全采用 OpenAI-compatible tool calling，以及多轮 tool call 的最小实现范围。
- `tasks` / `task_runs` 的最小落地版本是否先用 SQLite 兼容模型。
- memory search 是否进入 Phase 1。
- Dreaming 自动调度频率和审批边界。
- 陪伴式回复质量回归集如何评估。
