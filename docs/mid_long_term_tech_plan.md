# AI4ALL 中长期技术规划

> 创建时间：2026-05-18
> 当前定位：Demo 验证完成后的架构基准文档。
> 本文不替代 `docs/tech_plan.md`。`tech_plan.md` 保留为 Phase 1 早期开发方案和历史脉络；当两者在身份模型、系统边界或长期演进上出现不一致时，以本文为准。

## 1. 文档脉络

项目文档按时间和职责分层维护，避免后来者误以为所有文档都是同一阶段的最终设计。

| 文档 | 作用 | 当前状态 |
| --- | --- | --- |
| `start.md` | 项目出发点、产品定位、为什么基于 OpenClaw 做微信 AI 陪伴 | 保留为原始背景 |
| `docs/prd.md` | Phase 1 产品需求和验收标准 | 仍是 Phase 1 产品范围参考 |
| `docs/tech_plan.md` | 早期技术开发方案，描述首版三层架构和模块拆分 | 历史方案，继续保留 |
| `docs/current_status.md` | 当前已验证事实、限制、运行假设 | 事实状态参考 |
| `docs/identity_model_and_wechat_binding.md` | 当前身份解析逻辑、微信注册身份和后续绑定流程专题 | 身份交接和产品运营参考 |
| `docs/roadmap.md` | 分阶段产品和工程路线 | 阶段路线参考 |
| `docs/agent-orchestration-roadmap.md` | Prompt、Context、记忆、工具等 Agent 编排专项路线 | 专项技术路线 |
| `docs/mid_long_term_tech_plan.md` | Demo 之后的中长期架构基准和边界决策 | 本文，新的架构基准 |

`tech_plan.md` 里的部分字段命名来自早期模型，例如 `users`、`sender_id`、`account_id`。这些命名不应直接解释为长期数据模型。2026-05-18 之后，身份模型以本文第 3 节为准。

## 2. 已确认的架构边界

### 2.1 用户模型

Phase 1 和中期目标确认采用：

```text
一个接入的个人微信账号/微信会话身份
-> 一个独立 AI4ALL Account
-> 一个独立 AI bot 体验
```

暂不做：

- 一个公共服务微信号接待大量外部微信用户的客服号模型。
- 群聊 bot。
- 公开 SaaS 自助 onboarding。
- C 端用户直接配置底层系统能力。

这里的 `AI4ALL Account` 是产品侧和业务侧的隔离单元，不等同于 OpenClaw payload 里的 `account_id` 字段。

### 2.2 OpenClaw 的角色

长期确认 OpenClaw 只承担通道和运行时 hook 职责：

```text
微信连接
消息接收
消息发送
plugin hook runtime
```

AI4ALL 不依赖 OpenClaw 原生 `soul.md`、长期记忆或实例级配置承载产品体验。OpenClaw 可以继续升级，但它不是 AI4ALL 的业务状态中心。

### 2.3 Plugin / Skill 开放边界

`Plugin` 是系统能力扩展：

- 只由管理员安装、升级、灰度、回滚。
- 普通用户不可见，也不能安装。
- 用于接入通道、Provider、工具后端、RAG、TTS、任务系统、管理集成等。

`Skill` 是模型行为和任务流程扩展：

- 由后台按账号、场景、套餐、实验组启用。
- 普通用户可以感知能力结果，但不直接编辑底层 skill 指令。
- Skill 的启用、版本、Prompt 注入和权限范围都必须可审计。

### 2.4 同步与异步边界

普通文本聊天继续走同步 turn：

```text
微信消息
-> OpenClaw hook
-> AI4ALL Backend
-> LLM 生成
-> Bridge 返回
-> 微信回复
```

以下能力走异步任务模型：

- 语音处理。
- RAG 和复杂知识检索。
- 多工具调用。
- 定时提醒。
- 主动触达。
- 需要较长等待的后台任务。

异步任务完成后，通过 OpenClaw 的发送能力主动回复或通知。

### 2.5 记忆边界

记忆必须账号级隔离，不跨账号共享。

长期方向：

```text
短期上下文：session messages
长期记忆：account-level memory/profile
系统上下文：admin-managed context files
```

必须预留：

- 删除记忆。
- 重置记忆。
- 禁用记忆写入。
- 敏感记忆过滤。
- 记忆变更审计。

具体记忆架构另行设计，不在本文展开到实现细节。

### 2.6 数据层演进

SQLite 只作为 demo 和早期本地验证方案。

生产目标：

```text
PostgreSQL
+ Redis
+ 后台任务队列
+ 文件/对象存储
```

代码可以继续按阶段轻量推进，但 schema 和服务边界应按生产模型设计，避免将 SQLite 文件模型固化成长期架构。

### 2.7 控制面边界

管理后台是内部运营工具，不开放给 C 端用户。

控制面负责：

- 管理账号状态、备注、限流、套餐和实验组。
- 管理 Soul、Profile、Context Files 和 Skill 启用。
- 查看消息、trace、错误、用量。
- 管理 plugin/skill 版本、灰度和回滚。
- 做审计和成本统计。

## 3. 身份模型基准

这是当前最重要的架构修正。

### 3.1 不再把 OpenClaw `account_id` 当业务主键

当前 OpenClawBot 接口里，OpenClaw payload 的 `account_id` 可能对多个微信用户相同。它更接近通道侧机器人账号或 provider 账号标识，不能作为 AI4ALL 的用户隔离主键。

因此长期模型中：

```text
OpenClaw account_id
≠ AI4ALL account id
```

OpenClaw 字段应该保存为通道绑定信息，例如：

```text
channel_account_id
bot_account_id
provider_account_id
```

这些字段用于排查通道和发送路由，不用于业务隔离。

### 3.2 AI4ALL Account 的来源

当前最可靠的业务隔离来源是 `session_key`。

建议定义：

```text
ai4all_account_id
= AI4ALL 内部账号 ID
= 当前由 normalized(session_key) 派生
```

后续如果拿到更稳定的真实微信身份，例如 wxid、openid、unionid 或其他可靠 peer id，不直接替换历史主键，而是新增身份映射：

```text
identity_bindings:
  ai4all_account_id
  provider
  external_identity_type
  external_identity_value
  confidence
  first_seen_at
  last_seen_at
```

这样可以支持迁移和合并，而不会破坏已有消息、记忆和审计记录。

### 3.3 会话键与账号键

建议区分：

```text
ai4all_account_id
业务隔离单元。权限、记忆、套餐、限流、profile 都挂在这里。

session_key
通道侧当前会话身份。当前可作为 ai4all_account_id 的来源。

ai4all_session_id
AI4ALL 内部会话 ID。消息上下文、短期历史挂在这里。

channel_binding_id
OpenClaw / 微信等通道绑定记录。负责消息路由和排障。
```

短期为了少改代码，可以继续使用现有 `accounts.id` 字段，但语义上应明确它是 `ai4all_account_id`，不是 OpenClaw 原生 `account_id`。未绑定 legacy 入站仍可 fallback 为 `session_key`；Web onboarding 绑定完成后，入站消息会通过 `channel_account_id` 或 `openclaw_login_session_key` 路由到 Backend 预创建的 `acct_...`。当前代码通过 `app.identity.resolve_openclaw_identity()` 和绑定 lookup 统一完成这一步解析，并用轻量 `channel_bindings` 表记录 AI4ALL 账号与 OpenClaw 通道身份的关系。

中期当产品进入“用户先注册、创建智能体、再扫码绑定微信”的流程后，`ai4all_account_id` 应改为由 AI4ALL Backend 在创建智能体时预先生成；OpenClaw `session_key` 降级为通道会话键。扫码绑定通过一次性 `binding_intent_id` 串联：Backend 生成 `binding_intent_id`，传给 OpenClaw QR 登录能力作为本次登录的 `accountId/sessionKey`，再用同一个 ID 等待 OpenClaw 返回真实 `channel_account_id`，最终写入 `channel_bindings`。

截至 2026-05-20，Web onboarding 的最小实现已经采用这个方向：`/ui/onboarding.html` 可创建 `platform_user`、预创建 AI4ALL Account、发起 `binding_intent`，后端调用 OpenClaw Gateway `web.login.start/wait` 并把二维码与状态返回给前端。本机已完成真实扫码绑定和首条微信消息验收；QR wait 返回的 raw `channel_account_id` 与 Bridge 入站 normalized `channel_account_id` 已通过 alias lookup 对齐。本机 OpenClaw CLI/Gateway 设备已经批准 `operator.pairing` 和 `operator.admin` scope；官方 `@tencent-weixin/openclaw-weixin@2.4.3` 缺少 `gatewayMethods` provider discovery 声明的问题已通过本地补丁修复。补丁维护说明见 `docs/openclaw-weixin-gateway-qr-patch.md`。

### 3.4 命名迁移建议

近期代码和文档应逐步迁移到以下命名：

| 旧命名 | 新语义 | 建议 |
| --- | --- | --- |
| `account_id` | 含义混杂，可能是 OpenClaw 字段，也可能是 AI4ALL 业务账号 | 新代码避免裸用 |
| `ai4all_account_id` | AI4ALL 业务账号主键 | 核心业务层使用 |
| `channel_account_id` | 通道侧账号，例如 OpenClawBot 账号 | 通道层使用 |
| `session_key` | OpenClaw 会话键，当前身份来源 | 入站标准事件保留 |
| `channel_binding` | AI4ALL 账号和通道身份的绑定 | 已有轻量表，后续可扩展为完整绑定模型 |

## 4. 目标技术分层

中长期系统按五个平面规划。

### 4.1 Channel Plane

职责：

- 适配 OpenClaw、微信和未来其他渠道。
- 接收消息事件。
- 标准化 payload。
- 维护发送能力。
- 不承载 Soul、记忆、用户配置或业务状态。

当前实现：

```text
openclaw-weixin
-> OpenClaw Gateway
-> ai4all-openclaw-bridge
-> POST /openclaw/turn
```

中期目标：

- 把 OpenClaw payload 规范化成统一 `InboundTurn`。
- 标准化文本、语音、图片、文件、事件消息结构。
- 将发送能力抽象成 `ChannelSendClient`，支持同步回复和异步主动发送。

### 4.2 Identity & Routing Plane

职责：

- 从 `session_key` 等通道字段解析 `ai4all_account_id`。
- 建立和维护 channel binding。
- 处理消息去重。
- 选择目标 session。
- 保证账号和会话隔离。

关键要求：

- 所有业务读取必须带 `ai4all_account_id`。
- 所有消息历史必须带 `ai4all_session_id`。
- 任何通道字段都不能直接越过 identity resolver 进入业务层。

### 4.3 AI Business Core

职责：

- Message Orchestrator。
- Prompt / Context 组装。
- Soul / Profile / Style 管理。
- 短期上下文读取。
- 长期记忆读写。
- LLM / ASR / TTS Provider 适配。
- 限流、额度、成本控制。
- 安全策略和失败降级。

这是 AI4ALL 的核心业务层，不能被 OpenClaw 的实例级配置替代。

### 4.4 Extension Plane

职责：

- 管理 plugin。
- 管理 skill。
- 管理 tool schema 和工具权限。
- 管理 RAG、外部服务、任务系统等扩展能力。

长期应形成三个注册表：

```text
plugin_registry
skill_registry
tool_registry
```

运行时根据账号、场景、套餐、实验组选择可用能力，再注入 prompt 或 Agent Runtime。

### 4.5 Async & Control Plane

异步平面职责：

- 队列。
- Worker。
- 任务状态。
- 定时任务。
- 主动触达。
- 失败重试。

控制平面职责：

- Admin API / Admin UI。
- 配置、灰度、回滚。
- 审计。
- Trace。
- 用量统计。
- 成本统计。
- 告警。

## 5. 中长期目标数据模型

以下是方向性模型，不要求一次性实现。

### 5.1 Identity

```text
ai4all_accounts
- id
- status
- display_name
- notes
- plan
- memory_enabled
- created_at
- updated_at

channel_bindings
- id
- ai4all_account_id
- channel
- provider
- openclaw_instance_id
- channel_account_id
- session_key
- peer_id
- raw_identity_json
- first_seen_at
- last_seen_at

ai4all_sessions
- id
- ai4all_account_id
- channel_binding_id
- session_key
- status
- title
- created_at
- updated_at
```

### 5.2 Conversation

```text
messages
- id
- ai4all_account_id
- ai4all_session_id
- channel_binding_id
- message_id
- reply_to_message_id
- direction
- role
- message_type
- content
- raw_json
- latency_ms
- error
- created_at

debug_traces
- id
- trace_id
- ai4all_account_id
- ai4all_session_id
- source
- llm_model
- system_prompt
- messages_json
- reply
- metadata_json
- latency_ms
- error
- created_at
```

### 5.3 Context & Memory

```text
account_profiles
- ai4all_account_id
- display_name
- style
- preferences_json
- system_prompt_override
- updated_at

context_files
- id
- ai4all_account_id
- filename
- content
- version
- updated_by
- updated_at

memory_items
- id
- ai4all_account_id
- source_session_id
- memory_type
- content
- confidence
- status
- created_at
- updated_at

memory_daily_notes
- id
- ai4all_account_id
- date
- content
- created_at
- updated_at
```

### 5.4 Extension & Operations

```text
plugin_registry
- id
- name
- version
- status
- config_schema_json
- installed_by
- installed_at

skill_registry
- id
- name
- version
- description
- prompt_body
- status
- created_at

account_skill_assignments
- id
- ai4all_account_id
- skill_id
- enabled
- source
- created_at

tool_registry
- id
- name
- version
- schema_json
- permission_scope
- status

tasks
- id
- ai4all_account_id
- task_type
- status
- payload_json
- scheduled_at
- started_at
- finished_at
- error
```

## 6. 能力演进路线

### Stage 0：已完成 Demo 验证

已验证：

- OpenClaw 接入微信。
- Bridge 拦截 turn 并调用 AI4ALL Backend。
- Backend 调用 LLM 并回复微信。
- 多个会话可以隔离上下文。
- Admin / Debug API 初步可用。
- 账号级 context files 和记忆实验已有基础。

主要风险：

- 身份命名仍有历史混杂。
- SQLite 和文件系统适合本地，不适合生产。
- 同步 turn 不适合长任务。

### Stage 1：身份模型收敛和文本链路稳定

目标：

- 明确 `ai4all_account_id` 来源于 `session_key`。
- 代码和文档逐步避免混用 OpenClaw `account_id`。
- 文本私聊链路稳定。
- 基础 Admin UI 可用。
- 失败、限流、重复消息、禁用账号行为可预测。

建议任务：

- 新增 identity resolver。
- 新增轻量 channel binding 持久化。
- 入站 payload 标准化。
- Admin 展示 `ai4all_account_id`、`session_key`、`channel_account_id` 的区别。
- 继续保留 raw payload 便于排查。
- 对关键链路增加测试。
- 补齐重复绑定、解绑、QR 过期/取消/Gateway 重启等异常状态。

### Stage 2：生产基础设施

目标：

- 从 SQLite 迁移到 PostgreSQL。
- 引入 Redis 做短期限流、缓存、锁。
- 引入任务队列和 worker。
- 文件和媒体进入对象存储或统一文件存储。
- 部署方式可复现。

建议任务：

- 设计迁移脚本和数据备份方案。
- 抽象 repository / service 层，降低 SQLite 代码耦合。
- Docker Compose 支持 backend、db、redis、worker。
- 增加结构化日志、trace id、错误分类。

### Stage 3：语音和异步任务

目标：

- 支持微信语音入站。
- ASR 结果进入统一文本对话链路。
- 长任务不阻塞 Bridge hook。
- 异步完成后可主动发送回复。

建议任务：

- 明确 OpenClaw 语音 payload 形态。
- 建立 `tasks` / `task_runs`。
- 建立主动发送 client。
- 给用户提供“已收到，稍后回复”的体验策略。

### Stage 4：记忆系统产品化

目标：

- 短期上下文、每日备注、长期记忆分层。
- 记忆写入可控。
- 记忆可查看、删除、禁用。
- 不同账号绝对隔离。

建议任务：

- 把文件记忆逐步抽象成 Memory Service。
- 保留 Markdown 作为运营可读视图，但不要只依赖文件作为唯一状态。
- 建立敏感信息过滤和记忆审计。
- 为陪伴场景设计记忆质量评估集。

### Stage 5：Skills / Tools / Plugins 平台化

目标：

- Plugin 管理系统能力。
- Skill 管理模型行为能力。
- Tool 管理可执行动作。
- 按账号、套餐、场景、实验组启用。

建议任务：

- 设计 registry 和 assignment 模型。
- 每个 tool 必须有权限、限流、审计和失败体验。
- Skill 注入要有 token budget 和优先级。
- 插件安装和升级要支持版本 pin、灰度和回滚。

### Stage 6：多渠道和规模化

目标：

- 在不重构业务核心的前提下接入新渠道。
- 支持多个 OpenClaw 实例。
- 支持更多账号和更高并发。

建议任务：

- Channel adapter 标准化。
- OpenClaw instance registry。
- 账号迁移和 channel binding 迁移。
- 成本和稳定性监控。

## 7. 当前不做或暂缓

以下能力不是当前阶段优先级：

- 公开 SaaS 注册、支付、套餐自助购买。
- 群聊机器人。
- 大规模客服号模型。
- 复杂多 Agent 工作流。
- 图片理解和图片生成。
- 用户自定义插件市场。
- 让 C 端用户直接编辑底层 prompt、skill 或 plugin。

这些能力不是永远不做，而是不能影响当前核心链路：身份隔离、文本/语音体验、记忆、后台控制和可部署性。

## 8. 近期工程原则

近期开发应遵守：

- 新代码优先使用 `ai4all_account_id` 表达业务账号。
- OpenClaw 原始字段只保存在 channel/raw/context 层。
- 所有业务查询必须显式带账号边界。
- 不把长期业务状态写回 OpenClaw。
- 同步 turn 只处理短路径。
- 可能超过微信可接受等待时间的能力进入异步任务。
- Admin 能力先内部可用，再考虑体验 polish。
- 每次新增能力都要说明它属于 Channel、Core、Extension、Async 还是 Control Plane。

## 9. 与旧文档的共存规则

为了避免文档冲突，采用以下规则：

1. `docs/tech_plan.md` 保留为早期 Phase 1 技术草案，不删除、不大改。
2. `docs/roadmap.md` 继续描述产品和工程阶段，但不承载详细架构决策。
3. `docs/agent-orchestration-roadmap.md` 继续描述 Prompt、Context、记忆、工具编排专项，不替代整体架构。
4. 本文负责中长期系统边界、身份模型、技术分层和演进路线。
5. 当旧文档写到 `account_id` 时，后来者必须先判断它指的是 OpenClaw 字段还是 AI4ALL 业务账号。新代码应避免这个歧义。
