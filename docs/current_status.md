# AI4ALL Bridge 当前状态

更新时间：2026-05-16

## 项目定位

AI4ALL Bridge 是一个面向微信私聊场景的个人 AI 陪伴与生活助理服务。

当前阶段不是通用办公效率助手，也不是心理咨询产品。重点是：

- 日常聊天中的情感陪伴。
- 轻量生活助理能力。
- 通过 OpenClaw 类架构，让更多普通用户以较低成本使用个人 AI。
- 让一个 OpenClaw 实例可以承载多个个人微信账号接入。
- 每个接入账号拥有独立的 Soul、会话上下文、记忆和配置。

## 当前事实模型

这里需要明确区分官方 OpenClaw 能力和 AI4ALL 要补齐的能力。

### 官方 OpenClaw / openclaw-weixin

官方 `openclaw-weixin` 支持在同一个 OpenClaw 实例中连接多个微信账号，并提供一定的 session 隔离配置。

但 OpenClaw 原生的 `soul.md`、长期记忆、部分全局配置更偏实例级共享。多个微信账号共用一套 Soul/记忆配置，不符合 AI4ALL 对“每个用户拥有自己的个人 AI 陪伴/助理”的需求。

这也是 AI4ALL Bridge 需要存在的核心原因之一：保留 OpenClaw 的微信连接能力，把用户、Soul、记忆和业务配置转移到 AI4ALL Backend 按账号隔离管理。

### AI4ALL 目标模型

目标链路是：

```text
一个 OpenClaw 实例
-> 多个个人微信账号
-> 每个微信账号对应一个用户自己的 AI bot 会话
-> AI4ALL Backend 按微信账号隔离 Soul、记忆、会话和配置
```

当前不采用“一个服务微信号服务多个外部微信用户”的客服号模型。

在当前微信插件约束下，用户在微信里看到的是自己的 bot 会话。这个 bot 不作为群聊机器人使用，也不作为一个公共服务号同时和多个外部用户聊天。

### 已验证能力

当前已经验证的是：

```text
一个 OpenClaw 实例
-> 两个已登录个人微信账号
-> 私聊文本消息进入 AI4ALL Backend
-> Bridge 从 sessionKey 解析真实微信账号级 account_id
-> Backend 按 account_id + session_key 隔离上下文
-> 每个 account_id 自动拥有独立 user_profile.md
-> Backend 调用 DeepSeek
-> 回复回到微信
```

已验证账号：

```text
86f866663cf9-im-bot
53de8b76fd98-im-bot
```

当前 `account_id` 来源为 OpenClaw sessionKey。典型格式：

```text
agent:main:openclaw-weixin:<account_id>:direct:<peer_id>
```

## 已完成内容

### 微信端到端链路

真实微信链路已经跑通：

```text
微信私聊消息
-> openclaw-weixin
-> OpenClaw Gateway
-> ai4all-openclaw-bridge 插件
-> AI4ALL Backend
-> DeepSeek / OpenAI-compatible LLM
-> 回复回到微信
```

当前使用官方 OpenClaw 和官方 `openclaw-weixin`，没有 fork 两个官方仓库。

### Bridge 插件

已实现 `ai4all-openclaw-bridge`：

- 注册 OpenClaw `before_agent_reply` hook。
- 将私聊文本消息转发到 AI4ALL Backend。
- 使用 `AI4ALL_BRIDGE_SECRET` 做 Bearer 鉴权。
- 将 Backend 返回的回复作为 synthetic reply 交回 OpenClaw。
- Backend 异常时返回友好兜底文案。
- 原始 `ctx` / `event` 已随消息保存，便于排查 OpenClaw payload。
- 从 `ctx.sessionKey` 解析真实微信账号级 `account_id`。
- 在 raw 中记录 `ai4all_bridge.account_candidates` 和 `resolved_account_id`。

### Backend

已实现 FastAPI Backend：

- `GET /health`
- `POST /openclaw/turn`
- SQLite 存储。
- OpenAI-compatible LLM 调用。
- DeepSeek 真实 token/model 已验证。
- 未配置 `LLM_API_KEY` 时走本地 mock fallback。
- 每个账号自动创建并读取 `data/user_profiles/<account_id>/user_profile.md`。

### 数据隔离

Backend 当前按以下维度隔离上下文：

```text
account_id + session_key
```

当前数据表：

- `accounts`：接入的微信/OpenClaw 账号。
- `contacts`：历史命名，当前可理解为账号下的会话参与方记录，后续可能重命名或弱化。
- `sessions`：会话。
- `profiles`：风格、Prompt、偏好配置。
- `messages`：收发消息历史、耗时、错误。
- `data/user_profiles/<account_id>/user_profile.md`：账号级 Soul 和简化长期记忆。

`accounts` 已是当前账号隔离的核心实体；`contacts` 仍是早期兼容命名。

### 管理能力

当前还没有 Web 后台页面。

已经实现的是基于 Token 鉴权的 Admin API：

- 查看账号。
- 查看会话。
- 查看消息。
- 修改当前兼容用户记录的备注和状态。
- 修改 profile。
- 重置会话。

现有 Admin API 仍带有 `contacts` 命名，这是早期客服号模型留下的命名，后续需要调整为账号级管理。

### 可观测性

已实现：

- 消息持久化。
- 基于 `message_id` 的去重。
- turn 级耗时 `latency_ms`。
- outbound message 记录错误信息。
- 本地开发用 debug API。
- raw payload 查询接口。
- 账号级 `user_profile.md` 查看接口。

## 已验证事项

- 微信已连接 OpenClaw。
- 一个 OpenClaw 实例已同时接入两个个人微信账号。
- 微信消息可以进入 AI4ALL Backend。
- AI4ALL Backend 可以回复到微信。
- Bridge 已能解析真实微信账号级 `account_id`。
- 两个账号分别生成独立 `user_profile.md`。
- DeepSeek 真实模型调用可用。
- SQLite schema 可以自动创建和迁移当前字段。
- `message_id` 去重可用。
- Admin API 不带 token 返回 `401`。
- 禁用当前兼容用户记录后不回复。
- 重新启用后恢复回复。

## 当前限制

- 已验证主路径是两个微信账号的私聊文本。
- 语音仍属于 Phase 1 范围，但 ASR 尚未实现。
- 当前微信插件约束下不支持群聊 bot 模型。
- 暂不支持图片/多模态。
- 暂无 Web 管理后台，只有 Admin API。
- 暂无正式登录系统，Admin API 使用共享 token。
- 当前使用 SQLite，本地和早期测试够用；生产建议评估 PostgreSQL。

## 当前运行假设

- OpenClaw 本地运行。
- AI4ALL Backend 本地运行在 `http://127.0.0.1:8000`。
- Bridge 插件指向这个 Backend。
- DeepSeek 通过 `.env` 配置。
- 本地数据库路径为 `data/ai4all.sqlite3`。
- 账号级 profile 路径为 `data/user_profiles/<account_id>/user_profile.md`。
