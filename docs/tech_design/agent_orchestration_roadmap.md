# Agent 编排、工具与异步任务设计

更新时间：2026-05-31

## 1. 文档定位

本文定义 AI4ALL Phase 1 的 Agent 编排边界：普通聊天 turn 如何执行，Prompt/Context/Memory 如何组装，工具和高耗时任务如何分流，如何学习 OpenClaw 的 agent loop、tool schema、Dreaming、账号主动检查和 trace。

它承接以下产品需求：

- [陪伴式聊天 PRD](../product/companion_chat_prd.md)
- [记忆与上下文 PRD](../product/memory_prd.md)
- [搜索与异步任务 PRD](../product/search_and_async_tasks_prd.md)
- [主动消息与提醒 PRD](../product/proactive_prd.md)
- [语音输入 PRD](../product/voice_prd.md)

Context Files 的文件定义和记忆细节见 [Agent Context Files 与记忆机制设计](agent_context_files.md)。主动消息、reminder、commitment 和账号主动检查的发送底座见 [主动消息与提醒设计](proactive_messaging_design.md)。

## 2. 设计目标

Phase 1 编排目标：

- 让微信私聊普通 turn 足够短、稳定、可观测。
- 将陪伴式聊天作为默认路径，将提醒、搜索、ASR、长内容整理等能力清晰分流。
- Web Search 对齐 OpenClaw 风格的 LLM tool use：普通搜索同步执行，长耗时搜索或复杂整理才快速确认并异步补发。
- 将 OpenClaw 的 prompt/context 分层、Dreaming、tool schema、账号主动检查、trace 思想转化为 AI4ALL 一对多服务架构。
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
- 显式一次性提醒、hidden commitment、account check candidate 和 proactive scheduler 已有基础代码。

仍需补齐：

- 统一 Message Orchestrator 边界，进一步减少 HTTP route、DB、LLM、工具判断混杂。
- 统一 `tasks` / `task_runs`，支撑 Web Search 异步兜底、ASR、长内容整理和失败补发。
- Search Provider、ASR Provider、内容源 Provider 的 provider adapter 和成本记录。
- tool registry / task registry，区分“模型可见能力说明”和“后端真实可执行工具”。
- token usage、provider request id、成本、权益扣减和 trace 的统一记录。
- 陪伴体验回归集和长任务体验验收集。

## 4. OpenClaw 借鉴点

OpenClaw 对 AI4ALL 有价值的部分：

- Agent loop：输入、context assembly、tool use、LLM、memory、after-turn action 的清晰分层。
- Prompt block：稳定系统规则、Project Context、runtime metadata、output directives 分区。
- Workspace files：AGENTS、SOUL、IDENTITY、USER、TOOLS、MEMORY。
- Memory：daily notes、长期记忆和 Dreaming。
- Tool schema：工具名称、参数、权限、失败结果和 trace。
- Cron/账号主动检查：状态可恢复、低打扰、可跳过的主动循环。
- Debug trace：能解释模型为什么这样回复。

不能照搬的部分：

- OpenClaw 是一对一本地实例；AI4ALL 是一对多后端服务。
- OpenClaw 的 agent workspace 不能作为 AI4ALL 用户状态源。
- OpenClaw 的定时自检 不能变成一个全局用户循环，必须按账号隔离执行。
- OpenClaw 工具清单不能直接暴露给普通用户微信 bot。
- AI4ALL 需要额外处理权益扣减、通道风险、客服和运营审计。

## 5. Agent Turn 总体流程

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

## 6. Intent Gate

Intent Gate 位于限流和去重之后、LLM 主回复之前。它负责 pending state、提醒、显式后台任务等会直接改变后端状态的场景；搜索的主触发机制是 LLM 看到 `web_search` tool schema 后自然调用工具。

### 6.1 显式提醒

命中条件：

- 用户表达明确时间和明确事项。
- 置信度足够高。
- 不涉及医疗、法律、金融等高风险承诺。

处理：

- 在 LLM 主回复前创建 one-shot reminder。
- 回复用户创建结果或要求补充具体时间。
- 到期发送由 proactive scheduler 处理。

当前提醒识别为规则优先。后续可以增加 LLM 辅助解析，但写入 DB 前仍需要结构化校验。

### 6.2 高耗时任务

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

### 6.3 普通聊天

普通聊天是默认路径：

- 读取最近消息。
- 读取账号 Context Files。
- 读取 `MEMORY.md`；P0 不读取 daily notes 注入 prompt。
- 构建 prompt。
- 调 LLM。
- 保存回复、usage、trace。
- 异步触发 daily notes 和 hidden commitment extraction。

## 7. Prompt 与 Context Assembly

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

## 8. 工具体系边界

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

## 9. 异步任务设计

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

## 10. 记忆与 After-Turn 编排

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

## 11. 主动消息与账号主动检查编排

主动消息分三类：

| 类型 | 来源 | 是否用户触发 | 发送约束 |
| --- | --- | --- | --- |
| reminder | 用户明确请求 | 是 | due time + outbound ledger |
| async task result | 用户触发任务 | 是 | task status + idempotency + outbound ledger |
| 账号主动检查/content push | 系统候选 | 否或弱触发 | proactive state + quiet hours + daily limit + cooldown |

账号主动检查机制必须按账号执行：

```text
system scheduler
-> list due accounts
-> per-account state claim
-> read USER/MEMORY/recent chat/proactive state/candidates
-> decide no-op or outbound
-> outbound ledger + Gateway send
```

禁止：

- 读取已废弃的旧策略文件作为用户个人任务来源。
- 在用户未开启或无高置信候选时发送账号主动检查。
- 把内容推送混入普通聊天主链路。

## 12. Debug Trace 与 OpenClaw 对比

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

## 13. 能力演进顺序

### P0：聊天编排稳定

- 明确 Message Orchestrator 边界。
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

- LLM / ASR / Search / proactive 消耗计量。
- 将工具调用、任务执行和权益扣减流水关联。
- Admin 支持任务、trace、权益和客服排查。
- 可选支付开启时，把购买权益纳入同一 ledger。

## 14. 暂缓能力

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

## 15. 决策记录

- 2026-05-17：不追 OpenClaw 多 agent 编排。产品场景是一对一私聊陪伴，先把单 agent 的记忆、工具、人格做扎实。
- 2026-05-17：记忆写入使用异步后台任务，不阻塞回复。用户等待时间优先，失败可通过日志和后续补写处理。
- 2026-05-17：Safety 区块全局固定，运营覆盖分离。安全边界不能由单账号覆盖绕过。
- 2026-05-18：旧账号级主动策略文件从 Context Files 移出。用户主动触达和提醒单独建模。
- 2026-05-24：Phase 1 目标调整为正式内测版本，P0/P1/P1.5 纳入同一 Phase；支付购买可选，不阻塞内测。
- 2026-05-24：长耗时任务必须先快速确认，再异步补发完整结果。
- 2026-05-24：记忆机制必须学习 OpenClaw Dreaming，但写入要账号隔离、可追溯、可回滚。
- 2026-05-31：Web Search 与 OpenClaw 对齐为 LLM tool use；普通搜索同步完成，异步任务只作为超时、深度整理、用户明确后台整理等场景的兜底；不采用纯字符串匹配作为主触发机制。

## 16. 验收标准

- 普通文本 turn 可以完成身份解析、去重、限流、context assembly、LLM 回复和 trace。
- 明确提醒请求不会进入长工具链，能创建 one-shot reminder 并回复确认。
- Web Search 由模型通过 tool use 自然触发，普通搜索在当前 turn 同步返回带来源边界的回答；长耗时搜索进入异步兜底后，用户先收到确认回复，任务完成后收到完整结果，失败时收到失败说明。
- 同一任务不会重复补发，补发写入 outbound ledger。
- Prompt trace 能展示 Project Context、daily notes、runtime、override 和模型输入。
- 记忆写入、hidden commitment 和账号主动检查不阻塞同步聊天回复。
- 工具或任务未接入时，模型不会承诺已经完成对应动作。

## 17. 待确认问题

- Search Provider、搜索引用格式和失败话术。
- ASR Provider、语音最大时长和媒体保留策略。
- 普通 Web Search 的同步时间预算。
- 哪些任务允许同步执行，哪些必须异步。
- tool schema 是否完全采用 OpenAI-compatible tool calling，以及多轮 tool call 的最小实现范围。
- `tasks` / `task_runs` 的最小落地版本是否先用 SQLite 兼容模型。
- memory search 是否进入 Phase 1。
- Dreaming 自动调度频率和审批边界。
- 陪伴式回复质量回归集如何评估。
