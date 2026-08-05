# AI4ALL 多产品服务技术总平面

更新时间：2026-08-04

> **定位（2026-07-25 校正）：** 本文是跨产品的详细技术基线；当前生产产品为朝夕相伴，Phase 1 是其已完成里程碑。共享数据与身份隔离以 [多产品 ADR](shared/data/multi_product_modular_monolith_design.md) 为准，产品需求见 [`../products/`](../products/README.md)。其中 **§7 工作包摘要**仅作历史参考，不再更新；当前现状与近期队列见 [`STATUS.md`](../STATUS.md)。
>
> **产品边界校正（2026-08-04）：** 原朝夕 Native App / Companion World 已决议拆为独立产品鸣蝉
>（`app_id=mingchan`）；朝夕（`zhaoxi`）保留微信/OpenClaw 与 Web/H5 接入。本文早期章节如把
> App/World 记作朝夕形态，均以[核心模型](core-model.md)和[拆分计划](../plans/shared/zhaoxi_mingchan_product_split_plan.md)
> 的新边界为准。

## 1. 文档定位

本文是项目的“总技术平面 + 工作包摘要”。它不再承载各专题的字段级设计、策略细则或 provider 细节，而是用于回答四个问题：

1. 整体技术平面如何划分。
2. 各专题技术设计沉淀后，对总架构有哪些反思和调整。
3. 关键状态的 source of truth 在哪里。
4. （历史）Phase 1 曾如何拆成工作包、按什么依赖推进。

详细设计以专题文档为准：

| 领域 | 产品需求 | 技术设计 |
| --- | --- | --- |
| 总体架构 | [Phase 1 PRD](../products/zhaoxi/prd.md) | [总体架构](overview.md) |
| 需求追踪 | [产品专题 PRD 导航](../products/zhaoxi/README.md) | [Phase 1 需求追踪矩阵（已冻结快照）](../archive/phase1/phase1_traceability_matrix.md) |
| 注册与扫码接入 | [注册与扫码接入 PRD](../products/zhaoxi/capabilities/onboarding_prd.md) | [身份模型与微信绑定](shared/access/identity_model_and_wechat_binding.md)、[OpenClaw Bridge](shared/access/openclaw_bridge_design.md)、[QR 补丁](shared/access/openclaw_weixin_gateway_qr_patch.md) |
| 陪伴式聊天 | [陪伴式聊天 PRD](../products/zhaoxi/capabilities/companion_chat_prd.md) | [Conversation Orchestrator 主对话场景技术设计](agent-runtime/conversation_orchestrator_design.md) |
| 记忆与上下文 | [记忆与上下文 PRD](../products/zhaoxi/capabilities/memory_prd.md) | [Agent Context Files 与记忆机制](agent-runtime/agent_context_files.md)、[Dreaming 记忆压缩与长期记忆](agent-runtime/dreaming_memory_design.md) |
| 主动消息与提醒 | [主动消息与提醒 PRD](../products/zhaoxi/capabilities/proactive_prd.md) | [主动消息与提醒设计](products/zhaoxi/proactive_messaging_design.md) |
| Web Search 同步工具调用 | [Web Search 同步工具调用 PRD](../products/zhaoxi/capabilities/search_and_async_tasks_prd.md) | [Web Search 同步工具调用技术设计](agent-runtime/search_async_tasks_design.md) |
| 语音输入 | [语音输入 PRD](../products/zhaoxi/capabilities/voice_prd.md) | [语音输入技术设计](products/zhaoxi/voice_input_design.md) |
| 贝壳、增长与支付后置 | [贝壳、增长与支付后置 PRD](../products/zhaoxi/capabilities/entitlement_growth_prd.md) | [贝壳、增长与支付后置技术设计](shared/platform/entitlement_growth_design.md) |
| 运营与后台 | [运营与后台 PRD](../products/zhaoxi/capabilities/admin_ops_prd.md) | [隐私与后台访问控制](shared/platform/privacy_admin_access_control_design.md) |

若本文与旧草案或归档文档冲突，以 [总体架构](overview.md)、[Phase 1 PRD](../products/zhaoxi/prd.md)、本文件和对应专题技术设计为准。`docs/archive/` 下文档只保留历史脉络。

## 2. Phase 1 技术目标

Phase 1 不是技术 POC，而是可正式发布给内测用户的微信个人 AI 陪伴服务。内测版需要支撑普通用户通过手机号验证和微信扫码接入 OpenClawBot 通道，并获得由 AI4ALL Backend 驱动的个人 AI 陪伴与轻量助理能力。

技术目标：

- 接入闭环：手机号 OTP、扫码绑定、微信私聊、解绑和异常恢复可运营。
- 账号隔离：所有 Soul、上下文、记忆、消息、提醒、权益和配置都绑定 `ai4all_account_id`。
- 对话优先：普通聊天是最短同步路径；提醒、`web_search` 工具和语音各自进入清晰编排边界。
- 调度可靠：用户提醒、陪伴跟进和内容推送都有调度状态、幂等、重试和 outbound ledger；用户请求后的后台整理、异步任务补发或长耗时报告生成不进入 Phase 1。
- 隐私先行：Admin/Debug 默认脱敏，明文查看是例外能力并必须有权限和日志。
- 成本可追踪：LLM、Search、主动触达首条平台成本和用户扣减都有 cost event。
- 可演进部署：当前可用 SQLite、本地文件和单 scheduler，目标可迁移到 PostgreSQL、Redis、独立 scheduler worker 和对象/共享文件存储。

## 3. 总技术平面

```text
Ordinary User
  |-- Web/H5 onboarding: phone + captcha + OTP + QR
  |-- WeChat private chat: text / voice
        |
        v
Channel & Bridge Plane
  OpenClaw Runtime / openclaw-weixin / ai4all-openclaw-bridge / Gateway send
        |
        v
AI4ALL Backend
  Identity & Account Plane
  Conversation Orchestrator Plane
  Context & Memory Plane
  Tool Provider & Trace Plane
  Outbound Delivery & Proactive Policy Plane
  Entitlement, Growth & Support Plane
  Privacy, Admin & Observability Plane
        |
        v
Storage & Infrastructure
  Phase 1 local: SQLite + files + optional in-process scheduler
  Internal beta target: PostgreSQL + Redis + scheduler worker + object/shared storage
```

| 技术平面 | 职责 | Source of truth | 专题设计 |
| --- | --- | --- | --- |
| Channel & Bridge | 微信登录、入站消息、Bridge hook、同步回复、Gateway send、raw payload 保存 | OpenClaw 只持有通道运行态；业务状态不在 OpenClaw | [OpenClaw Bridge](shared/access/openclaw_bridge_design.md)、[QR 补丁](shared/access/openclaw_weixin_gateway_qr_patch.md) |
| Identity & Account | `platform_user`、默认 `ai4all_account`、owner binding、binding intent、channel binding、入站身份解析 | AI4ALL Backend DB | [身份模型与微信绑定](shared/access/identity_model_and_wechat_binding.md) |
| Conversation Orchestrator | `/openclaw/turn`、去重、限流、Intent Gate、Tool Use、prompt/context assembly、LLM 回复、after-turn | AI4ALL Backend DB + Context Files | [Conversation Orchestrator](agent-runtime/conversation_orchestrator_design.md) |
| Context & Memory | 账号级 Context Files、active session、daily notes、Dreaming、`MEMORY.md`、记忆纠错 | 目标为结构化状态；Markdown 是可读视图和 prompt 输入 | [Agent Context Files](agent-runtime/agent_context_files.md) |
| Tool Provider & Trace | Web Search provider、provider adapter、provider trace、tool invocation trace | AI4ALL Backend DB；搜索只在当前 turn 同步执行 | [Web Search 同步工具调用](agent-runtime/search_async_tasks_design.md) |
| Outbound Delivery & Proactive Policy | `outbound_messages`、提醒发送、陪伴跟进、内容推送、频控、避让、冷却 | AI4ALL Backend DB；Gateway 只发送 | [主动消息与提醒](products/zhaoxi/proactive_messaging_design.md) |
| Entitlement, Growth & Support | 贝壳 wallet/ledger、cost events、注册赠送、邀请奖励、客服补偿、支付后置 | AI4ALL Backend DB；未来可拆 billing/growth/support | [贝壳、增长与支付后置](shared/platform/entitlement_growth_design.md) |
| Privacy, Admin & Observability | Admin/Debug 脱敏、角色、临时明文授权、明文访问日志、trace、告警 | AI4ALL Backend DB + 日志平台 | [隐私与后台访问控制](shared/platform/privacy_admin_access_control_design.md) |
| Storage & Infrastructure | 数据库、Redis、文件/对象存储、scheduler、部署和版本检查 | 内测目标以 DB 为主状态，文件为视图或临时介质 | [总体架构](overview.md) |

## 4. 架构反思与调整

各专题设计完成后，总架构需要做以下调整。

### 4.1 将隐私控制提升为独立横切平面

早期设计把 Admin、Debug 和 trace 主要当作排障能力。结合运营后台 PRD 和记忆 PRD 后，需要把隐私控制前置为内测发布前置条件：

- Admin/Debug 默认只展示元数据、状态、统计和脱敏内容。
- 用户聊天正文、AI 回复、raw payload 正文、prompt/messages、daily notes 和 debug trace 明文都属于敏感正文。
- 明文查看必须走 `admin` 最高权限，或普通后台用户申请管理员审批的 2 小时临时权限。
- 所有明文查看必须写入 `admin_access_events`。

落点见 [隐私与后台访问控制](shared/platform/privacy_admin_access_control_design.md)。

### 4.2 拆清“用户请求异步任务”和“主动消息调度”

Phase 1 已正式取消用户请求后的通用异步任务机制。长耗时 Web Search、长内容整理、后台报告生成和“整理好后再发我”这类请求，不创建 `tasks` / `task_runs`，不通过 Gateway send 补发结果；当前 turn 返回失败或不支持说明。语音当前依赖 `openclaw-weixin` 上游转写文本，不走后端 ASR task。

需要保留的是主动消息调度边界：

- Tool Provider & Trace Plane 负责当前 turn 内的 `web_search` provider 调用、回退、trace 和成本事件。
- Outbound Delivery & Proactive Policy Plane 负责用户提醒、陪伴跟进、内容推送的发送 ledger、路由、幂等、投递状态和按类别执行发送策略。

Scheduler / due dispatcher 属于提醒、陪伴跟进、内容邀请等主动消息调度，不属于用户请求异步任务机制。细则见 [主动消息与提醒设计](products/zhaoxi/proactive_messaging_design.md)、[Web Search 同步工具调用技术设计](agent-runtime/search_async_tasks_design.md) 和 [语音输入技术设计](products/zhaoxi/voice_input_design.md)。

### 4.3 Conversation 需要演进为 Conversation Orchestrator

当前 `app/turn_service.py` 已承担主链路，但随着提醒、搜索、记忆、权益和 trace 接入，HTTP route 和业务编排容易混杂。Phase 1 应明确 Conversation Orchestrator 边界：

```text
identity resolve
-> account policy / entitlement precheck
-> message dedupe
-> intent gate
   -> explicit reminder
   -> normal chat
-> prompt/context + enabled tool schema
-> LLM tool use / reply
-> persist reply / usage / trace
-> after-turn actions
```

同步路径只做低延迟工作。普通 Web Search 作为模型可见的 `web_search` 工具同步执行；长耗时搜索、复杂资料整理和多步骤 provider 调用不进入后台任务，当前 turn 返回失败或不支持说明。语音输入当前由上游转写后进入普通文本链路，不在 Backend 内做 ASR。落点见 [Conversation Orchestrator 主对话场景技术设计](agent-runtime/conversation_orchestrator_design.md)。

### 4.4 记忆从“直接写长期记忆”调整为“原始材料 + 自动应用链路”

记忆专题明确后，daily notes 不再是“抽取出的长期记忆摘要”，而是按业务日保存的原始文字化聊天材料。`MEMORY.md` 只保存精选长期记忆。

架构调整：

- 普通聊天成功后异步写 daily notes，不阻塞用户回复。
- daily notes 改为历史材料后，不应默认全量注入 prompt。
- 每日 4 点和 500 轮 session 压缩触发 Dreaming。
- Dreaming 产出 memory item、diff、source、自动应用/跳过结果和回滚记录；没有人工审核前置环节。
- 后台保留 Dreaming run、item、skip reason 和 diff 摘要，用于 debug 和 prompt 调优。
- Admin 默认只能看 metadata，正文查看遵守隐私授权。

落点见 [Agent Context Files 与记忆机制](agent-runtime/agent_context_files.md)。

### 4.5 权益系统以 cost event 为枢纽，而不是直接扣余额

早期 `daily_usage` 只能统计消息次数，不能承担贝壳余额或计费语义。权益专题明确后，总架构调整为：

- `cost_events` 记录资源消耗和成本归因。
- `entitlement_ledger` 负责所有改变余额的发放、扣减、补偿、回滚和支付生效。
- `entitlement_wallets.balance_shell_micros` 只能由 ledger 同事务更新或推导。
- 主动触达首条只记录平台成本事件，不产生用户扣减 ledger。
- 用户回复后的 AI 回复、Search 和其他任务再按普通规则扣减。

落点见 [贝壳、增长与支付后置技术设计](shared/platform/entitlement_growth_design.md)。

### 4.6 OpenClaw 继续保持通道层边界

OpenClaw 的 Agent OS 思路值得借鉴，但 AI4ALL 是一对多后端服务，不能把 OpenClaw workspace、Cron、channel account 或 payload `account_id` 当作业务状态中心。

必须保持：

- OpenClaw / Bridge 负责微信连接、消息 hook、raw payload 和 Gateway send。
- AI4ALL Backend 持有产品用户、业务账号、记忆、任务、权益、主动消息策略和运营审计。
- 入站消息只能通过 identity resolver 得到 `ai4all_account_id` 后访问业务状态。
- 当前代码里的 `account_id` 只是 `ai4all_account_id` 的兼容别名，新代码优先使用 `ai4all_account_id` 命名。

落点见 [身份模型与微信绑定](shared/access/identity_model_and_wechat_binding.md) 和 [OpenClaw Bridge](shared/access/openclaw_bridge_design.md)。

### 4.7 先单仓闭环，按服务边界写代码

Phase 1 建议继续在当前仓库形成最小闭环，因为用户链路、任务、权益、后台和排障强耦合，过早拆服务会增加联调成本。

但模块和 API 边界要按未来可拆分设计：

- `channel`：OpenClaw 和未来渠道适配。
- `identity`：注册、owner binding、channel binding。
- `orchestrator`：turn 编排、Intent Gate、Tool Use、prompt、after-turn。
- `scheduler`：用户提醒、陪伴跟进、内容推送的到期扫描和发送。
- `provider`：当前 turn 内的 Web Search adapter、trace 和成本事件。
- `billing`：wallet、ledger、cost event；支付后续单独立项。
- `growth`：邀请码、邀请关系、奖励规则和反作弊。
- `support/admin`：后台、客服、权限、审计和隐私访问。

## 5. 状态所有权与数据模型摘要

本文只保留目标模型摘要。字段级模型见各专题技术设计。

| 状态域 | 关键实体 | 所有权 | 说明 |
| --- | --- | --- | --- |
| 产品用户与业务账号 | `platform_users`、`accounts`、`account_owner_bindings` | AI4ALL Backend | Phase 1 普通入口默认一个产品用户对应一个 active AI4ALL Account；当前表名仍是历史 `accounts` |
| 微信通道绑定 | `binding_intents`、`channel_bindings` | AI4ALL Backend | OpenClaw 只提供通道 identity 和登录结果 |
| 会话与消息 | `sessions`、`messages`、`debug_traces` | AI4ALL Backend | 当前 DB 字段 `account_id` 语义上等同 `ai4all_account_id` |
| Context Files | `AGENTS.md`、`SOUL.md`、`IDENTITY.md`、`USER.md`、`TOOLS.md`、`MEMORY.md` | AI4ALL Backend | Markdown 是 prompt 输入和可读视图，长期应配合结构化审计状态 |
| 记忆材料与晋升 | `daily notes`、`dreaming_runs`、`dreaming_memory_items`、`memory_events` | AI4ALL Backend | daily notes 是原始文字材料，长期记忆由 Dreaming 自动应用或跳过，需 source、diff、debug metadata 和回滚 |
| 同步工具与 provider trace | `tool_invocations`、`search_provider_runs`、`cost_events` | AI4ALL Backend | Web Search 只在当前 turn 同步执行；不创建用户请求异步任务 |
| 主动发送 | `outbound_messages`、`reminders`、`proactive_commitments`、`content_push_preferences` | AI4ALL Backend | 用户提醒、陪伴跟进和内容推送写 ledger；不同 category 执行不同策略 |
| 权益与成本 | `entitlement_wallets`、`entitlement_ledger`、`cost_events`、`model_price_rules` | AI4ALL Backend | cost event 记录消耗，ledger 改变余额 |
| 增长与客服 | `referral_codes`、`referral_relationships`、`support_tickets` | AI4ALL Backend | Phase 1 可先最小闭环，未来可拆 growth/support |
| 后台隐私 | `admin_users`、`admin_plaintext_grants`、`admin_access_events` | AI4ALL Backend | 默认脱敏，明文查看必须授权并审计 |
| 微信登录态和底层 token | OpenClaw / openclaw-weixin runtime | OpenClaw | AI4ALL 不复制底层通道 token，只保存必要 route 和 raw metadata |

通用约束：

- 所有业务查询必须显式带 `ai4all_account_id`。
- 原始通道字段只在 channel/raw/context 层使用。
- 新增表必须有幂等键、状态机或审计字段，避免任务和发送不可恢复。
- Markdown 文件不能成为长期唯一 source of truth。
- Admin 默认 API 不返回正文类敏感内容。

## 6. 核心链路摘要

### 6.1 注册与扫码绑定

```text
Web/H5
-> captcha + SMS OTP
-> platform_user
-> default ai4all_account
-> binding_intent
-> OpenClaw Gateway QR login start/wait
-> channel_binding
-> 微信首条消息路由到 aid_...
```

关键要求：

- OTP token 原子消耗。
- 重复注册复用默认账号。
- 同一 `channel_account_id` 已绑定其他账号时默认拒绝，不能静默迁移。
- Binding Intent 的首次绑定主路径必须对 Web 和 Admin 可见。过期、取消、wait failed、already connected、replaced 等重复/异常绑定状态先作为排障记录保留，不作为当前内测首发阻塞项。
- 普通用户入口 Phase 1 不开放多 AI4ALL Account 创建。

细节见 [身份模型与微信绑定](shared/access/identity_model_and_wechat_binding.md)。

### 6.2 普通聊天 turn

```text
微信私聊文本
-> OpenClaw / Bridge
-> /openclaw/turn
-> identity resolver
-> account policy / dedupe / entitlement precheck
-> intent gate
-> context + memory + prompt + enabled tool schema
-> LLM tool use / reply
-> persist messages / usage / trace
-> Bridge synthetic reply
-> after-turn: daily notes / commitment extraction / metrics
```

普通聊天不执行高耗时工具链。模型不得承诺未接入或后端不可执行的工具动作。

Intent Gate 的 Phase 1 默认策略是高置信规则和状态机优先，不对每条消息额外调用一次 LLM 做总分类。LLM classifier 只作为低召回场景的后续辅助，并且只能产出 intent candidate；所有会写 DB、创建任务或取消/更新状态的动作仍必须由代码校验和必要的用户确认完成。

> **实现现状校正（2026-06-15）：** 独立 Intent Gate 规则层最终未落地。实际链路为特殊命令 →
> onboarding 子流程 → 普通聊天 + LLM tool use,提醒走工具(`app/products/zhaoxi/tools/reminder_handlers.py`),
> 无 LLM 之前的规则分流层。本节及上方流程图中的 "intent gate" 节点按历史设计阅读;
> 详见 [Conversation Orchestrator 设计](agent-runtime/conversation_orchestrator_design.md) 文首校正。

细节见 [Conversation Orchestrator 主对话场景技术设计](agent-runtime/conversation_orchestrator_design.md)。

### 6.3 搜索、语音和复杂后台请求

```text
普通 Web Search
-> prompt + web_search tool schema
-> LLM decides tool_call
-> search provider call / fallback
-> tool result back to LLM
-> sync answer with source boundaries
-> cost event / trace

long search / complex background request
-> current turn failure or unsupported explanation
-> no task
-> no outbound result delivery
```

Web Search 默认采用 OpenClaw 风格的同步 LLM tool use，不通过纯字符串 intent gate 作为主触发机制。长耗时搜索、复杂整理、provider 超时或用户明确要求后台整理时，当前回合返回失败或不支持说明，不创建后台任务。语音输入依赖 `openclaw-weixin` 上游转写文本，转写后直接进入普通文本链路。

细节见 [Web Search 同步工具调用技术设计](agent-runtime/search_async_tasks_design.md) 和 [语音输入技术设计](products/zhaoxi/voice_input_design.md)。

### 6.4 记忆与 Dreaming

```text
普通聊天成功
-> async daily notes raw material
-> daily 04:00 / 500-turn session close
-> Dreaming
-> memory item + diff + source
-> auto apply / skip / rollback
-> MEMORY.md / long-term memory view
```

daily notes 是原始文字材料，不是长期记忆摘要。`MEMORY.md` 的写入必须保守、可追溯、可禁用和可回滚。

细节见 [Agent Context Files 与记忆机制](agent-runtime/agent_context_files.md)。

### 6.5 用户提醒、陪伴跟进和内容推送

```text
explicit reminder / hidden commitment / 账号主动检查 / content candidate
-> scheduler due scan
-> category-specific policy
-> outbound ledger
-> Gateway send
-> write sent outbound into active session messages
-> status writeback
```

分类规则：

| 类型 | 发送策略摘要 |
| --- | --- |
| 用户提醒 | 按用户设定时间发送，不受主动触达总开关、quiet hours、陪伴/内容推送日上限影响 |
| 陪伴跟进 | 受主动触达总开关、quiet hours、分类日上限和 6 小时用户提醒避让约束 |
| 内容推送 | 受主动触达总开关、quiet hours、分类日上限、6 小时避让和内容拒绝冷却约束 |
所有成功发送给用户的 outbound 都必须写入当前账号 active session 的 `messages`，作为已经发生的对话事实。`outbound_messages` 负责发送幂等和状态追踪，`messages` 负责用户可见对话时间线和后续 prompt 上下文。

细节见 [主动消息与提醒设计](products/zhaoxi/proactive_messaging_design.md)。

### 6.6 权益与成本

```text
注册 / 拉新 / 运营发放
-> entitlement ledger credit

LLM / Search / outbound platform cost
-> cost event
-> if billable_to_user: entitlement ledger debit
-> balance and ledger visible to user/support
```

贝壳金额使用 fixed-point `shell_micros`，不能用浮点数保存余额。主动触达首条只写平台成本事件，不扣用户贝壳。

细节见 [贝壳、增长与支付后置技术设计](shared/platform/entitlement_growth_design.md)。

### 6.7 后台、客服和可观测性

```text
Admin / Support / Debug
-> default redacted metadata views
-> optional plaintext grant
-> plaintext endpoint
-> admin_access_events
```

进入内测前，后台应先做到“能排障但默认看不到正文”。trace、raw payload、daily notes 和 prompt/messages 的明文查看都必须显式授权和审计。

细节见 [隐私与后台访问控制](shared/platform/privacy_admin_access_control_design.md)。

## 7. Phase 1 工作包摘要（历史里程碑，不再更新）

> 本节是 Phase 1 的工作包拆分快照，保留以记录当时的依赖与拆分思路。**当前完成度和近期队列以 [`STATUS.md`](../STATUS.md) 为准**；逐条需求-代码映射的冻结快照见 [`archive/phase1/phase1_traceability_matrix.md`](../archive/phase1/phase1_traceability_matrix.md)。

工作包按依赖关系推进。每个工作包的详细需求和验收点以对应 PRD 与技术专题为准。

| 工作包 | 目标 | 主要交付 | 依赖 | 详细设计 |
| --- | --- | --- | --- | --- |
| WP0 隐私与后台访问控制 | 内测前避免后台形成错误明文能力 | Admin/Debug 默认脱敏、`admin/staff` 或过渡角色、临时明文授权、明文访问日志 | 无，建议最先做 | [隐私与后台访问控制](shared/platform/privacy_admin_access_control_design.md) |
| WP1 注册、扫码与通道绑定硬化 | 普通用户能稳定接入并可运营恢复 | `/web/config`、首次绑定主路径、正式解绑、Binding Intent 排障状态、OpenClaw Gateway 版本/补丁校验、绑定视图；重复绑定策略后置 | WP0 部分红线 | [身份模型与微信绑定](shared/access/identity_model_and_wechat_binding.md)、[OpenClaw Bridge](shared/access/openclaw_bridge_design.md) |
| WP2 对话主链路与 Conversation Orchestrator | 聊天体验稳定，后续能力有清晰分流点 | Orchestrator 边界、Intent Gate、首次聊天 onboarding、prompt 优先级、安全围栏、token/provider usage、陪伴质量回归集 | WP1 | [Conversation Orchestrator 主对话场景技术设计](agent-runtime/conversation_orchestrator_design.md)、[陪伴式聊天 PRD](../products/zhaoxi/capabilities/companion_chat_prd.md) |
| WP3 发送和成本底座 | 为 Proactive、Billing 和同步工具成本追踪提供共同基础 | `outbound_messages` category、idempotency、`cost_events` 初版、tool/provider trace | WP2 | [Web Search 同步工具调用](agent-runtime/search_async_tasks_design.md)、[主动消息与提醒](products/zhaoxi/proactive_messaging_design.md)、[贝壳设计](shared/platform/entitlement_growth_design.md) |
| WP4 记忆与上下文产品化 | 陪伴持续性可用且可审计 | daily notes 改为原始材料、4 点 session 结束、500 轮 LLM 压缩、LLM carryover、Dreaming memory item、自动应用/跳过、diff、debug 调优和 rollback、记忆管理入口 | WP2、WP0 | [Agent Context Files](agent-runtime/agent_context_files.md)、[Dreaming](agent-runtime/dreaming_memory_design.md) |
| WP5 主动消息与提醒闭环 | 用户提醒、陪伴跟进和内容推送低风险可控 | 类型化 policy engine、一次性提醒修正、周期提醒、自然语言取消/更新确认、6 小时避让、内容推送和拒绝冷却、真实微信联调、scheduler 进程边界 | WP3 | [主动消息与提醒设计](products/zhaoxi/proactive_messaging_design.md) |
| WP6 搜索与语音输入 | 轻量助理能力进入内测可用 | `web_search` tool schema、DuckDuckGo/Bing RSS/Aliyun IQS/Baidu AI Search adapter、搜索结果引用、同步工具调用 trace、搜索 5 贝壳扣减、失败/不支持说明；语音依赖上游转写文本 | WP3 | [Web Search 同步工具调用](agent-runtime/search_async_tasks_design.md)、[语音输入](products/zhaoxi/voice_input_design.md) |
| WP7 贝壳、增长与客服 | 内测权益可信闭环 | wallet/ledger、注册赠送、LLM/Search 扣减、模型倍率、邀请奖励、客服补偿；支付后置 | WP3，部分依赖 WP6 | [贝壳、增长与支付后置](shared/platform/entitlement_growth_design.md) |
| WP8 内测部署与观测 | 从本地验证进入可运行内测环境 | Docker Compose、PostgreSQL 迁移、Redis 限流/锁、独立 scheduler worker、结构化日志、trace id、告警、端到端验收 | WP1-WP7 持续接入 | [总体架构](overview.md)、[项目现状](../STATUS.md) |

## 8. 建议推进顺序

1. 先做 WP0，确保后台、Debug 和 trace 的默认隐私边界正确。
2. 做 WP1，收口注册、扫码首次绑定、解绑和排障状态；重复绑定策略后置。
3. 做 WP2，把普通聊天和 Intent Gate 边界稳定下来。
4. 做 WP3，补齐所有专题共用的 outbound category、tool/provider trace 和 cost event 底座。
5. 并行推进 WP4、WP5、WP6，但各自接入 WP3 的统一发送、trace 和成本模型。
6. 做 WP7，把已有 cost event 变成可扣减、可补偿、可客服排查的贝壳账本。
7. 持续推进 WP8，把单机 SQLite/文件/进程形态迁移到内测目标部署。

这个顺序的核心原因是：隐私和身份是发布前底线，Conversation Orchestrator 是用户体验主干，outbound/trace/cost 是 Search、Voice、Proactive 和 Entitlement 的共同底座。

## 9. 工程原则

- 新代码优先使用 `ai4all_account_id` 表达业务账号；`account_id` 只作为兼容别名。
- OpenClaw 原始字段只保存在 channel/raw/context 层，不作为业务主键。
- 所有业务读写必须显式带账号边界。
- 同步 turn 只处理低延迟路径；Phase 1 不支持用户请求后的后台整理、异步任务补发或长耗时报告生成。
- 所有 outbound 都必须经过 ledger、幂等键、路由检查和状态回写。
- 主动消息必须按 `user_reminder`、`companion_followup`、`content_push` 分类执行策略。
- daily notes 是原始材料，长期记忆写入必须有 memory item、source、diff、自动应用/跳过记录、debug metadata 和回滚。
- 权益扣减必须由 cost event 追溯到 ledger，不能只更新余额。
- Admin/Debug 默认脱敏；明文查看必须显式授权并记录审计日志。
- 每新增一个能力，都要明确它属于哪个技术平面、谁持有状态、如何失败、如何观测、如何扣费或记录平台成本。
