# AI4ALL Bridge 当前状态

更新时间：2026-05-16

## 项目定位

AI4ALL Bridge 是一个面向微信私聊场景的个人 AI 陪伴与生活助理服务。

当前阶段不是通用办公效率助手，也不是心理咨询产品。重点是：

- 日常聊天中的情感陪伴。
- 轻量生活助理能力。
- 通过 OpenClaw 类架构，让更多普通用户以较低成本使用个人 AI。
- 先把私聊文本链路做稳定，再扩展语音、后台、部署和多入口能力。

## 关于“多用户”的定义

这里需要区分两个层次。

### 已验证能力

当前已经验证的是：

```text
一个 OpenClaw 实例
-> 一个已登录的服务微信号
-> 多个真实微信用户私聊这个服务微信号
-> AI4ALL Backend 按用户/session 隔离上下文
```

也就是说，多个真实微信用户可以加同一个服务微信号，并分别获得独立会话。

### 尚未验证能力

尚未验证的是：

```text
一个 OpenClaw 实例
-> 同时接入多个不同的服务微信号
-> 每个服务微信号下面再服务多个真实微信用户
```

这是否可行取决于 `openclaw-weixin` 对多账号的支持方式，需要后续专项验证。

当前数据模型已经预留 `account_id`，可以支持多个入口账号；但运行时多微信号接入还没有完成验证。

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

### Backend

已实现 FastAPI Backend：

- `GET /health`
- `POST /openclaw/turn`
- SQLite 存储。
- OpenAI-compatible LLM 调用。
- DeepSeek 真实 token/model 已验证。
- 未配置 `LLM_API_KEY` 时走本地 mock fallback。

### 多用户数据隔离

Backend 当前按以下维度隔离用户：

```text
account_id + session_key
```

当前数据表：

- `accounts`：微信/OpenClaw 入口账号。
- `contacts`：某个入口账号下的聊天用户。
- `sessions`：会话。
- `profiles`：用户级风格、Prompt、偏好。
- `messages`：收发消息历史、耗时、错误。

### 管理能力

当前还没有 Web 后台页面。

已经实现的是基于 Token 鉴权的 Admin API：

- 查看账号。
- 查看用户。
- 查看会话。
- 查看消息。
- 修改用户备注。
- 启用/禁用用户。
- 修改用户 profile。
- 重置会话。

禁用用户后，`/openclaw/turn` 会返回 `no_reply=true`，不会进入 LLM 生成。

### 可观测性

已实现：

- 消息持久化。
- 基于 `message_id` 的去重。
- turn 级耗时 `latency_ms`。
- outbound message 记录错误信息。
- 本地开发用 debug API。

## 已验证事项

- 微信已连接 OpenClaw。
- 微信消息可以进入 AI4ALL Backend。
- AI4ALL Backend 可以回复到微信。
- DeepSeek 真实模型调用可用。
- SQLite schema 可以自动创建和迁移当前字段。
- `message_id` 去重可用。
- Admin API 不带 token 返回 `401`。
- 禁用用户后不回复。
- 重新启用用户后恢复回复。

## 当前限制

- 已验证主路径是微信私聊文本。
- 语音仍属于 Phase 1 范围，但 ASR 尚未实现。
- 暂不支持群聊。
- 暂不支持图片/多模态。
- 暂无 Web 管理后台，只有 Admin API。
- 暂无正式登录系统，Admin API 使用共享 token。
- 当前使用 SQLite，本地和早期测试够用；生产建议评估 PostgreSQL。
- 只验证了一个服务微信号入口；多服务微信号接入尚未验证。

## 当前运行假设

- OpenClaw 本地运行。
- AI4ALL Backend 本地运行在 `http://127.0.0.1:8000`。
- Bridge 插件指向这个 Backend。
- DeepSeek 通过 `.env` 配置。
- 本地数据库路径为 `data/ai4all.sqlite3`。
