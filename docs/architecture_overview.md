# AI4ALL 微信个人 AI 陪伴服务总体架构

更新时间：2026-06-13

## 一句话理解

AI4ALL 微信个人 AI 陪伴服务 = **微信/OpenClaw 通道层** + **AI4ALL 一对多个人 AI 后端** + **账号级 Agent 体验与运营控制面**。

它不是 OpenClaw 的简单托管版，也不是公共客服号机器人。OpenClaw 负责微信连接、消息收发和 hook runtime；AI4ALL Backend 负责普通用户注册、账号隔离、陪伴式 Agent、记忆、提醒、权益、拉新和运营管理。

## 文档分层

本文回答“系统整体长什么样、各层职责是什么、核心链路如何流动”。

详细落地以以下文档为准：

| 文档 | 作用 |
| --- | --- |
| `docs/prd.md` | Phase 1 产品范围、需求和验收标准 |
| `docs/product/README.md` | 产品专题 PRD 索引，对齐单项能力的详细产品需求 |
| `docs/phase1/phase1_technical_design.md` | Phase 1 详细技术设计、数据模型和工作包 |
| `docs/phase1/phase1_traceability_matrix.md` | 产品需求、技术设计、当前代码和开发缺口的追踪索引 |
| `docs/tech_design/identity_model_and_wechat_binding.md` | 身份模型、扫码绑定和账号路由专题 |
| `docs/tech_design/proactive_messaging_design.md` | 主动消息、提醒、commitment 和 scheduler 专题 |
| `docs/tech_design/agent_context_files.md` | Agent Context Files、daily notes、长期记忆和 Dreaming |
| `docs/tech_design/conversation_orchestrator_design.md` | 主对话 turn、Session/Messages、Intent/Tool Use、Prompt、同步回复和主动消息衔接 |
| `docs/tech_design/entitlement_growth_design.md` | 贝壳 wallet/ledger、成本事件、邀请奖励和支付后置 |

## 1. 顶层架构

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│                                Ordinary User                                 │
│                                                                              │
│       Web/H5 Onboarding                         WeChat Private Chat          │
│  phone + captcha + OTP + QR                         text / voice             │
└───────────────┬──────────────────────────────────────────────┬───────────────┘
                │                                              │
                ▼                                              ▼
┌────────────────────────────────┐           ┌─────────────────────────────────┐
│          AI4ALL Web API         │           │        OpenClaw Runtime          │
│  register / binding intent      │           │  openclaw-weixin + Gateway       │
│  admin / user visible pages      │           │  channel login / recv / send     │
└───────────────┬────────────────┘           └───────────────┬─────────────────┘
                │                                            │
                │                                            ▼
                │                            ┌─────────────────────────────────┐
                │                            │    ai4all-openclaw-bridge       │
                │                            │  before_agent_reply hook         │
                │                            │  normalize payload + synthetic   │
                │                            │  reply / shadow trace            │
                │                            └───────────────┬─────────────────┘
                │                                            │
                └──────────────────────┬─────────────────────┘
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              AI4ALL Backend                                  │
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  ┌──────────────┐ │
│  │ Identity &   │  │ Conversation │  │ Agent Context &   │  │ Tools &      │ │
│  │ Binding      │  │ Runtime      │  │ Memory            │  │ Providers    │ │
│  │              │  │              │  │                   │  │              │ │
│  │ platform_user│  │ turn service │  │ SOUL / USER       │  │ LLM / Search │ │
│  │ aid_...      │  │ prompt build │  │ daily notes       │  │ Search       │ │
│  │ channel bind │  │ LLM reply    │  │ Dreaming          │  │ content src  │ │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  └──────┬───────┘ │
│         │                 │                   │                   │         │
│         ▼                 ▼                   ▼                   ▼         │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │              Proactive Scheduler & Outbound Runtime                  │   │
│  │ reminders / commitment / content push / scheduler                    │   │
│  └──────────────────────────────────────┬───────────────────────────────┘   │
│                                         ▼                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  ┌──────────────┐ │
│  │ Entitlement  │  │ Growth       │  │ Control Plane     │  │ Observability│ │
│  │ wallet/ledger│  │ referral     │  │ Admin / Support   │  │ traces/logs  │ │
│  └──────────────┘  └──────────────┘  └──────────────────┘  └──────────────┘ │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Storage & Infrastructure                             │
│                                                                              │
│   Phase 1 local/demo: SQLite + files + optional in-process scheduler          │
│   Internal beta target: PostgreSQL + Redis + scheduler worker + object/file storage │
└──────────────────────────────────────────────────────────────────────────────┘
```

核心原则：

- **通道层薄**：OpenClaw / Bridge 不保存长期业务状态。
- **账号隔离强**：所有业务状态都挂到 `ai4all_account_id`。
- **同步链路短**：普通聊天和普通 Web Search 尽量在当前 turn 同步返回；长耗时搜索、复杂整理或后台报告请求直接返回失败/不支持说明，不创建后台任务。语音当前依赖上游转写后进入普通文本链路。
- **主动触达受控**：所有主动消息经过 outbound ledger 和类型化策略；用户提醒按用户设定时间发送，陪伴跟进和内容推送受 quiet hours、日上限和偏好约束。
- **运营可见**：账号、绑定、会话、消息、提醒/主动发送、用量、权益、错误都需要能被 Admin 追踪。

## 2. 核心概念分层

```text
┌─────────────────────────────────────────────────────────────┐
│ Channel Layer                                                │
│ OpenClaw Gateway / openclaw-weixin / Bridge / Gateway send   │
│ 只负责微信通道、hook、收发消息和 raw payload                  │
├─────────────────────────────────────────────────────────────┤
│ Identity & Account Layer                                     │
│ platform_user / ai4all_account / binding_intent / binding    │
│ 决定“这条消息属于哪个 AI4ALL Account”                         │
├─────────────────────────────────────────────────────────────┤
│ Conversation & Agent Runtime Layer                           │
│ session / messages / turn_service / prompt_builder / LLM     │
│ 处理普通聊天、上下文、回复生成和 after-turn 动作               │
├─────────────────────────────────────────────────────────────┤
│ Context & Memory Layer                                       │
│ AGENTS / SOUL / IDENTITY / USER / TOOLS / MEMORY / Dreaming  │
│ 提供陪伴感、持续性、偏好和长期记忆                           │
├─────────────────────────────────────────────────────────────┤
│ Proactive Scheduler & Outbound Layer                         │
│ reminder / commitment / 账号主动检查 / content push / scheduler │
│ 处理提醒、陪伴跟进、内容推送和发送幂等                       │
├─────────────────────────────────────────────────────────────┤
│ Entitlement & Growth Layer                                   │
│ wallet / ledger / usage metering / referral / pay deferred   │
│ 管理内测权益、扣减、拉新奖励；支付后置                       │
├─────────────────────────────────────────────────────────────┤
│ Control & Observability Layer                                │
│ Admin UI/API / support / debug traces / logs / audit         │
│ 让内测运营、排障、补偿和风控可执行                           │
└─────────────────────────────────────────────────────────────┘
```

这套分层与 OpenClaw 的 Channel、Session、Agent、Plugin、Model 分层有相似处，但 AI4ALL 多了产品账号、权益、运营和一对多隔离层。

## 3. AI4ALL Conversation Turn

普通文本消息的核心执行流程：

```text
Inbound WeChat Message
        │
        ▼
OpenClaw / Bridge
        │
        ▼
POST /openclaw/turn
        │
        ▼
┌────────────────────┐
│ Identity Resolve    │  channel_account_id / session_key -> ai4all_account_id
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Account Policy      │  disabled / rpm / daily limit / future entitlement check
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Message Dedupe      │  message_id + account boundary
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Intent Gate         │  reminder / unsupported background request / normal chat
└──────┬───────┬─────┘
       │       │
       │       ├───────────────► unsupported/failure explanation
       │
       ▼
┌────────────────────┐
│ Context Assembly    │  recent messages + context files + memory + runtime
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Prompt + Tools      │  safety + context + enabled tool schema
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ LLM / Tool Use      │  reply or web_search tool call + timeout + fallback
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Persist Reply       │  messages / usage / debug trace
└─────────┬──────────┘
          ▼
Bridge Synthetic Reply -> WeChat
          │
          ▼
After Turn: memory write / commitment extraction / metrics
```

Phase 1 的同步 turn 可以执行低延迟工具调用。普通 Web Search 按 OpenClaw 风格由模型通过 `web_search` tool use 触发并同步返回；长耗时搜索、长内容整理或复杂工具链超过同步等待体验时，当前回合返回失败或不支持说明。Phase 1 不创建用户请求后的后台任务，也不补发搜索结果。

## 4. 主动调度与发送

```text
User-triggered long search / complex background request
        │
        ▼
Failure or unsupported explanation in current turn
  “这个需要后台长时间整理，Phase 1 暂时不支持整理好后再发。”
        │
        ▼
no task / no worker / no outbound result delivery
```

主动提醒和内容推送走发送底座：

```text
reminder / commitment / 账号主动检查 / content candidate
        │
        ▼
Scheduler due scan
        │
        ▼
policy check
  category-specific policy / idempotency / cooldown
        │
        ▼
outbound_messages ledger
        │
        ▼
Gateway send + status writeback
```

区别在于：

- Scheduler / due dispatcher 只服务提醒、陪伴跟进和内容推送，不属于用户请求异步任务机制。
- 用户提醒是用户明确设定任务，严格按设定时间发送，不受主动触达总开关和默认日上限影响。
- 新闻、搞笑等内容推送是“试探性主动触达”，必须严格低频、可拒绝、可冷却。

## 5. 状态所有权

| 状态 | Source of Truth | 说明 |
| --- | --- | --- |
| 微信登录态、context token | OpenClaw / openclaw-weixin | AI4ALL 暂不复制底层通道 token |
| 平台用户和手机号 | AI4ALL Backend | `platform_users` |
| 业务隔离账号 | AI4ALL Backend | `ai4all_account_id` / 当前 DB `account_id` |
| 通道绑定关系 | AI4ALL Backend | `channel_bindings` + raw identity |
| 会话和消息 | AI4ALL Backend | `sessions` / `messages` |
| Soul、Profile、Context Files | AI4ALL Backend | 文件视图 + 后续结构化存储 |
| daily notes / long-term memory | AI4ALL Backend | 学习 OpenClaw Dreaming，但账号级隔离 |
| reminders / commitments / content push | AI4ALL Backend | 不放入 OpenClaw Cron 作为主状态 |
| Web Search provider trace | AI4ALL Backend | `tool_invocations` / `search_provider_runs` / `cost_events` |
| 权益代币/点数和扣减流水 | AI4ALL Backend | 未来可拆 billing 服务 |
| Debug trace / audit | AI4ALL Backend | 可包含 OpenClaw shadow trace |

## 6. 与 OpenClaw 的关系

AI4ALL 借鉴 OpenClaw 的“Agent OS”思想，但产品边界不同：

| 维度 | OpenClaw | AI4ALL Phase 1 |
| --- | --- | --- |
| 使用者 | 单个 owner 或开发者 | 普通 C 端用户 |
| 运行模型 | 本地一对一 agent runtime | Backend 一对多服务 |
| 通道 | 多通道 Gateway | Phase 1 先微信/OpenClawBot |
| 状态 | 实例/workspace/session 为主 | `ai4all_account_id` 为主 |
| Prompt | 本地 agent context | 账号级 context + 后端策略 |
| Memory | 本地 memory/Dreaming | 账号级记忆 + 运营审计 |
| Tools | agent 可直接调用工具 | 后端按账号/权益/权限启用 |
| Cron/定时检查 | 单用户 agent 循环 | 系统 scheduler + 账号级检查 |
| 运营 | 用户自管理 | Admin/Support/Entitlement 控制面 |

OpenClaw 仍然是重要参考，尤其是 Agent Loop、context engine、tool schema、Dreaming、定时检查和 trace。但 AI4ALL 的业务状态必须留在自己的后端。

## 7. 部署形态

### 当前本地形态

```text
FastAPI Backend
SQLite
data/user_profiles/*
OpenClaw Gateway local process
ai4all-openclaw-bridge plugin
optional in-process scheduler
```

适合开发、验证真实微信链路和单机内测。

### 内测目标形态

```text
Nginx / HTTPS
        │
        ▼
AI4ALL Backend API
        │
        ├── PostgreSQL
        ├── Redis
        ├── Scheduler worker
        ├── Object or shared file storage
        └── OpenClaw Gateway / channel runtime
```

关键生产化要求：

- FastAPI 和 scheduler 明确进程边界，避免多实例重复扫描。
- reminder 和 outbound 必须原子 claim。
- Redis 承担短期限流、锁、队列或缓存。
- SQLite 文件模型不能成为长期生产依赖。
- OpenClaw 插件和 Gateway 能力要有启动前检查和版本固定。

### 多机接入形态（中心大脑 + 瘦接入节点）

为突破「单一出口 IP 挂大量微信号」触发风控的瓶颈，接入层可横向扩展到多机。一套代码按 `AI4ALL_ROLE` 选能力，**central（中心大脑）与 node（瘦接入节点）可独立、也可同机共存**：

| 角色 | 跑什么 | 碰 SQLite? |
| --- | --- | --- |
| `standalone`（默认） | = 今天单机形态，全部 + openclaw 本机直发 | 是（本机） |
| `central` | FastAPI 大脑、SQLite（唯一写者）、画像、三调度器、审核台、admin、节点面向 API | 是（本机） |
| `node` | openclaw + 微信会话、节点 agent（入站转发 + 出站轮询 + 登录 exec） | **否，一律走 HTTP 与中心通信** |

生产落地（aliyun1+aliyun2 双机 MVP）：

```text
                         微信用户
        ┌───────────────────┴───────────────────┐
┌───────┴────────┐                      ┌────────┴────────────┐
│ aliyun2 (node) │                      │ aliyun1             │
│ openclaw+会话   │                      │ central + node 同机  │
│ node-agent     │  ① 入站转发(HTTP→中心) │ openclaw+会话         │
│                │  ② 登录 push(中心拨节点)│ FastAPI 大脑 + 调度   │
│                │  ③ 出站 pull 认领       │ SQLite(唯一写者)      │
└────────────────┘  ④ 结果回报            └─────────────────────┘
```

- **不变量**：只有 central 进程读写 `data/ai4all.sqlite3`；节点永不直连 SQLite。`node_id` 只是路由属性，不参与账号隔离判定。
- **入站**：节点 openclaw 把 `/openclaw/turn` POST 到中心；**被动回复内联在 HTTP 响应里原路返回**，零跨机。
- **出站混合**：登录/登出/扫码走 **push**（中心按 `access_nodes.base_url` 直拨目标节点 agent，二维码低延迟同步回传）；主动消息走 **pull**（节点轮询 `/node/outbound/claim` 认领，复用 `outbound_messages` 抢占式 claim，节点掉线消息留队列可续传）。
- **节点 → 中心**走「稳定指纹地址」`CENTRAL_URL`（MVP=内网 hosts 别名），切换中心只改该指向，节点零改配。
- **中心可切换 aliyun1↔aliyun2**：中心状态 = SQLite + 画像目录 + system 目录，复用既有 `backup_data.py` 快照 + rsync；切换后原中心机降为 node，**微信会话不重扫码**。

`standalone` 等价于「central+node 同机 + 出站本机即时直发」，保证本地开发与现有测试零回归。详细落地、迁移 Runbook 与切换流程见 [`docs/tech_design/multi_node_access_refactor.md`](tech_design/multi_node_access_refactor.md)。

## 8. Phase 1 架构主线

Phase 1 的架构收口顺序：

1. 接入与绑定稳定：OTP、QR、重复绑定、解绑、异常状态。
2. 对话主链路稳定：身份、去重、限流、prompt、LLM、trace。
3. 记忆产品化：daily notes、Dreaming、查看/重置/禁用/纠错。
4. Web Search 同步工具调用：provider、回退、引用、失败说明、成本事件和 5 贝壳扣减。
5. 主动触达闭环：reminder、commitment、content push、冷却和设置。
6. 权益和增长：内测赠送、扣减流水、拉新奖励、客服处理。
7. 内测部署：PostgreSQL/Redis/scheduler worker、日志、告警、Admin UI。

详细数据模型、工作包和实现缺口见 `docs/phase1/phase1_technical_design.md`。
