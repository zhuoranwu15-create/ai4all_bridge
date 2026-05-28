# AI4ALL 微信个人 AI 陪伴服务总体架构

更新时间：2026-05-24

## 一句话理解

AI4ALL 微信个人 AI 陪伴服务 = **微信/OpenClaw 通道层** + **AI4ALL 一对多个人 AI 后端** + **账号级 Agent 体验与运营控制面**。

它不是 OpenClaw 的简单托管版，也不是公共客服号机器人。OpenClaw 负责微信连接、消息收发和 hook runtime；AI4ALL Backend 负责普通用户注册、账号隔离、陪伴式 Agent、记忆、任务、权益、拉新和运营管理。

## 文档分层

本文回答“系统整体长什么样、各层职责是什么、核心链路如何流动”。

详细落地以以下文档为准：

| 文档 | 作用 |
| --- | --- |
| `docs/prd.md` | Phase 1 产品范围、需求和验收标准 |
| `docs/product/README.md` | 产品专题 PRD 索引，对齐单项能力的详细产品需求 |
| `docs/phase1_technical_design.md` | Phase 1 详细技术设计、数据模型和工作包 |
| `docs/phase1_traceability_matrix.md` | 产品需求、技术设计、当前代码和开发缺口的追踪索引 |
| `docs/tech_design/identity_model_and_wechat_binding.md` | 身份模型、扫码绑定和账号路由专题 |
| `docs/tech_design/proactive_messaging_design.md` | 主动消息、提醒、commitment 和 scheduler 专题 |
| `docs/tech_design/agent_context_files.md` | Agent Context Files、daily notes、长期记忆和 Dreaming |
| `docs/tech_design/conversation_orchestrator_design.md` | 主对话 turn、Session/Messages、Intent Gate、Prompt、同步回复和异步任务衔接 |
| `docs/tech_design/entitlement_growth_design.md` | 贝壳 wallet/ledger、成本事件、邀请奖励和可选支付 |

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
│  │ platform_user│  │ turn service │  │ SOUL / USER       │  │ LLM / ASR    │ │
│  │ acct_...     │  │ prompt build │  │ daily notes       │  │ Search       │ │
│  │ channel bind │  │ LLM reply    │  │ Dreaming          │  │ content src  │ │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  └──────┬───────┘ │
│         │                 │                   │                   │         │
│         ▼                 ▼                   ▼                   ▼         │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                 Async Task & Proactive Runtime                       │   │
│  │ reminders / Web Search / ASR / content push / commitments / worker   │   │
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
│   Internal beta target: PostgreSQL + Redis + worker + object/file storage     │
└──────────────────────────────────────────────────────────────────────────────┘
```

核心原则：

- **通道层薄**：OpenClaw / Bridge 不保存长期业务状态。
- **账号隔离强**：所有业务状态都挂到 `ai4all_account_id`。
- **同步链路短**：普通聊天同步返回，搜索/语音/复杂任务异步补发。
- **主动触达受控**：所有主动消息经过 outbound ledger 和类型化策略；用户提醒按用户设定时间发送，陪伴跟进和内容推送受 quiet hours、日上限和偏好约束。
- **运营可见**：账号、绑定、会话、消息、任务、用量、权益、错误都需要能被 Admin 追踪。

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
│ Async Task & Proactive Layer                                 │
│ task / worker / reminder / commitment / heartbeat / push     │
│ 处理长任务、提醒、主动触达和补发结果                         │
├─────────────────────────────────────────────────────────────┤
│ Entitlement & Growth Layer                                   │
│ wallet / ledger / usage metering / referral / optional pay   │
│ 管理内测权益、扣减、拉新奖励和可选购买                       │
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
│ Intent Gate         │  reminder / long task / normal chat
└──────┬───────┬─────┘
       │       │
       │       ├───────────────► async task quick acknowledgement
       │
       ▼
┌────────────────────┐
│ Context Assembly    │  recent messages + context files + daily notes + memory
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ Prompt Build        │  safety + AGENTS/SOUL/USER/MEMORY + output directives
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ LLM Inference       │  provider call + timeout + fallback
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

Phase 1 的同步 turn 不应该执行 Web Search、长内容整理或复杂工具链。超过微信同步等待体验的任务进入 Async Task。

## 4. 异步任务与主动发送

```text
User-triggered long task
  Web Search / ASR / complex summary
        │
        ▼
Quick reply in current turn
  “我先帮你查一下，整理好后发你。”
        │
        ▼
tasks / task_runs
        │
        ▼
Worker
  provider call / retry / timeout / summarize
        │
        ▼
outbound_messages ledger
        │
        ▼
OpenClaw Gateway send
        │
        ▼
WeChat final result
```

主动提醒和内容推送也走同一个发送底座：

```text
reminder / commitment / heartbeat / content candidate
        │
        ▼
Scheduler / worker claim
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

- Web Search 结果补发是“用户请求结果投递”，不等同无触发主动推送。
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
| Web Search / ASR 等任务 | AI4ALL Backend | `tasks` / `task_runs` |
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
| Cron/Heartbeat | 单用户 agent 循环 | 系统 scheduler + 用户级 run |
| 运营 | 用户自管理 | Admin/Support/Entitlement 控制面 |

OpenClaw 仍然是重要参考，尤其是 Agent Loop、context engine、tool schema、Dreaming、heartbeat 和 trace。但 AI4ALL 的业务状态必须留在自己的后端。

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
        ├── Worker / Scheduler
        ├── Object or shared file storage
        └── OpenClaw Gateway / channel runtime
```

关键生产化要求：

- FastAPI 和 scheduler 明确进程边界，避免多 worker 重复扫描。
- reminder、task、outbound 必须原子 claim。
- Redis 承担短期限流、锁、队列或缓存。
- SQLite 文件模型不能成为长期生产依赖。
- OpenClaw 插件和 Gateway 能力要有启动前检查和版本固定。

## 8. Phase 1 架构主线

Phase 1 的架构收口顺序：

1. 接入与绑定稳定：OTP、QR、重复绑定、解绑、异常状态。
2. 对话主链路稳定：身份、去重、限流、prompt、LLM、trace。
3. 记忆产品化：daily notes、Dreaming、查看/重置/禁用/纠错。
4. 异步任务底座：Web Search、ASR、任务状态、worker、结果补发。
5. 主动触达闭环：reminder、commitment、content push、冷却和设置。
6. 权益和增长：内测赠送、扣减流水、拉新奖励、客服处理。
7. 内测部署：PostgreSQL/Redis/worker、日志、告警、Admin UI。

详细数据模型、工作包和实现缺口见 `docs/phase1_technical_design.md`。
