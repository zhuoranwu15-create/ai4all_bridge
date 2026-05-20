# 技术开发方案：微信个人 AI 陪伴与生活助理 Phase 1

> 状态说明：本文是早期技术方案草案，保留用于理解 Phase 1 的最初设计思路和模块拆分。部分实现细节已被当前代码、`docs/roadmap.md` 和 `docs/mid_long_term_tech_plan.md` 更新。
>
> 重要命名修正：早期文档里的 `account_id`、`user`、`sender_id` 语义可能混杂。2026-05-18 之后，中长期架构以 `docs/mid_long_term_tech_plan.md` 为准：AI4ALL 的业务隔离主键是 `ai4all_account_id`，当前来源是 OpenClaw `session_key`；OpenClaw payload 里的 `account_id` 不应直接作为业务用户主键。

## 1. 开发目标

Phase 1 的技术目标是搭建一套多账号可用、会话隔离、成本可控的类 OpenClaw 后端服务。

首版需要做到：

- 一个 OpenClaw 实例连接多个个人微信账号
- OpenClaw 负责微信协议接入和消息收发
- 自研后端负责身份识别、记忆、人设、限流、ASR、LLM 调用和回复发送
- 账号之间上下文、Soul 和记忆逻辑隔离
- 服务具备后续扩展 RAG、多 Soul、管理后台、付费系统的模块边界

## 2. 总体架构

系统分为三层：

### 2.1 接入层：微信 + OpenClaw

职责：

- 登录并维持微信连接
- 接收私聊文本和语音消息
- 提取用户 ID、消息 ID、消息类型、文本内容、媒体引用
- 将标准化事件转发给业务后端 webhook
- 接收后端发送指令，并向用户发送文本回复

Phase 1 中，OpenClaw 只承担 I/O 与协议适配，不承担 AI 业务逻辑。

### 2.2 业务服务层：自研后端

职责：

- 提供 webhook 接收入口
- 消息去重
- 用户与会话识别
- 限流和用量控制
- 语音转文本
- 记忆读取和裁剪
- Soul 注入和 Prompt 组装
- LLM 调用
- 回复发送调度
- 日志和状态记录

### 2.3 计算引擎层：LLM + ASR Provider

职责：

- LLM 提供文本理解和回复生成
- ASR 提供语音识别
- Provider 通过适配器封装，避免业务代码绑定某一个厂商

## 3. 推荐技术栈

首版建议保持简单，优先可维护和快速落地。

- 后端语言：Python
- Web 框架：FastAPI
- 数据库：PostgreSQL
- 缓存与限流：Redis
- ORM：SQLAlchemy 或 SQLModel
- 任务队列：Phase 1 可先使用进程内异步任务；如需要更可靠队列，再接 Celery/RQ/Arq
- 部署：Docker Compose
- 配置：环境变量 + `.env`
- 日志：结构化 JSON 日志

如果希望更轻量，数据库可以从 SQLite 起步，但多人使用和后续扩展建议直接使用 PostgreSQL。

## 4. 核心模块

### 4.1 Webhook Receiver

接口示例：

```http
POST /webhooks/openclaw/messages
```

职责：

- 校验 OpenClaw 请求签名或共享 token
- 解析标准消息事件
- 写入原始事件表
- 基于消息 ID 去重
- 将消息交给 Message Orchestrator 处理

建议标准事件结构：

```json
{
  "event_id": "evt_xxx",
  "message_id": "msg_xxx",
  "sender_id": "wxid_xxx",
  "chat_id": "wxid_xxx",
  "chat_type": "private",
  "message_type": "text",
  "text": "你好",
  "media": null,
  "timestamp": 1710000000
}
```

语音事件：

```json
{
  "event_id": "evt_xxx",
  "message_id": "msg_xxx",
  "sender_id": "wxid_xxx",
  "chat_id": "wxid_xxx",
  "chat_type": "private",
  "message_type": "voice",
  "text": null,
  "media": {
    "media_id": "media_xxx",
    "url": "https://example.com/voice.amr",
    "format": "amr",
    "duration_ms": 4200
  },
  "timestamp": 1710000000
}
```

### 4.2 Message Orchestrator

统一处理文本和语音消息。

流程：

1. 检查是否为私聊消息。
2. 获取或创建用户。
3. 获取默认会话。
4. 检查黑名单、频率限制、每日额度。
5. 如果是语音，调用 ASR 得到文本。
6. 保存用户消息。
7. 读取最近 N 轮历史。
8. 组装 Prompt。
9. 调用 LLM。
10. 保存 AI 回复。
11. 通过 OpenClaw Send Client 发送回复。
12. 记录处理状态和用量。

### 4.3 User & Conversation Service

职责：

- 根据 `sender_id` 创建用户
- 为用户创建默认会话
- 维护用户状态：正常、限流、黑名单
- 提供用户级用量统计

Phase 1 暂不支持用户主动创建多个会话。

### 4.4 Memory Manager

职责：

- 按 `conversation_id` 查询最近消息
- 按轮数和 token 预算裁剪
- 不跨用户、不跨会话读取上下文
- 将语音识别后的文本作为普通用户消息进入记忆

Phase 1 只实现滑动窗口记忆。

### 4.5 Soul Manager

职责：

- 加载默认 Soul
- 将 Soul 作为 system prompt 注入
- 为后续多 Soul 留出数据结构

默认 Soul 可以先用 Markdown 文件或数据库记录保存。首版推荐文件配置，便于快速迭代。

### 4.6 Prompt Assembler

输入：

- Soul system prompt
- 最近历史消息
- 当前用户输入

输出：

- LLM provider 所需的 messages 数组

约束：

- 不把其他用户消息加入上下文
- 不暴露内部系统信息
- 超过 token 预算时优先裁剪最早历史

### 4.7 LLM Client

职责：

- 统一封装不同 LLM Provider
- 支持超时、重试、错误分类
- 返回文本、token 用量、耗时、provider request id

Phase 1 可以先实现一个 Provider，但接口要保留扩展能力。

### 4.8 ASR Client

职责：

- 下载或读取语音文件
- 调用 ASR Provider
- 返回识别文本、耗时、错误信息

约束：

- ASR 失败时不进入 LLM 调用
- 语音文件处理失败要有明确日志
- 语音时长可设置上限，避免成本失控

### 4.9 Rate Limiter & Quota

维度：

- 用户短时间频率，例如每分钟最多 N 条
- 用户每日交互次数
- 用户每日 token 预算
- 单次最大上下文 token
- 语音最长时长

实现建议：

- 高频限流使用 Redis
- 每日统计落 PostgreSQL
- 命中限制时直接返回友好提示，不调用 LLM 或 ASR

### 4.10 OpenClaw Send Client

职责：

- 调用 OpenClaw 主动发送文本消息接口
- 记录发送成功或失败
- 支持基础退避和重试
- 避免重复发送同一条 AI 回复

## 5. 数据模型草案

### 5.1 users

- `id`
- `wxid`
- `display_name`
- `status`
- `default_soul_id`
- `created_at`
- `updated_at`

### 5.2 conversations

- `id`
- `user_id`
- `channel`
- `chat_id`
- `status`
- `created_at`
- `updated_at`

### 5.3 messages

- `id`
- `conversation_id`
- `user_id`
- `role`
- `message_type`
- `content`
- `raw_message_id`
- `token_count`
- `asr_status`
- `llm_status`
- `created_at`

### 5.4 souls

- `id`
- `name`
- `description`
- `system_prompt`
- `status`
- `created_at`
- `updated_at`

### 5.5 usage_daily

- `id`
- `user_id`
- `date`
- `message_count`
- `asr_seconds`
- `input_tokens`
- `output_tokens`
- `llm_cost`
- `asr_cost`
- `created_at`
- `updated_at`

### 5.6 inbound_events

- `id`
- `event_id`
- `message_id`
- `sender_id`
- `chat_id`
- `chat_type`
- `message_type`
- `payload_json`
- `status`
- `error_message`
- `created_at`

### 5.7 outbound_messages

- `id`
- `conversation_id`
- `message_id`
- `recipient_id`
- `content`
- `status`
- `provider_response`
- `created_at`
- `sent_at`

## 6. 第一版接口清单

### 6.1 OpenClaw -> 后端

```http
POST /webhooks/openclaw/messages
```

接收文本、语音消息事件。

### 6.2 后端 -> OpenClaw

```http
POST {OPENCLAW_BASE_URL}/messages/send
```

发送文本回复。

实际路径以 OpenClaw 能力为准，开发前必须确认。

### 6.3 健康检查

```http
GET /health
```

返回服务、数据库、Redis 连接状态。

### 6.4 简单管理接口

Phase 1 可先提供内部接口，不做 UI。

```http
GET /admin/users/{wxid}/usage
POST /admin/users/{wxid}/blacklist
DELETE /admin/users/{wxid}/blacklist
```

## 7. 消息处理状态机

建议状态：

- `received`
- `duplicated`
- `rate_limited`
- `asr_processing`
- `asr_failed`
- `llm_processing`
- `llm_failed`
- `send_pending`
- `sent`
- `send_failed`

每条入站事件都要有最终状态，便于排查。

## 8. 错误处理策略

- 重复消息：直接忽略，不重复调用模型，不重复发送。
- 非私聊消息：记录并忽略，Phase 1 不处理群聊。
- ASR 失败：回复用户语音未识别成功。
- LLM 超时：回复用户稍后再试。
- 额度不足：回复限额提示，不调用模型。
- OpenClaw 发送失败：记录失败，可有限重试。
- 数据库失败：返回 500，保留错误日志。

## 9. 部署方案

首版 Docker Compose 服务：

- `api`: FastAPI 后端
- `postgres`: PostgreSQL
- `redis`: Redis
- `openclaw`: OpenClaw 服务，或外部已有 OpenClaw 实例

配置项：

- `DATABASE_URL`
- `REDIS_URL`
- `OPENCLAW_BASE_URL`
- `OPENCLAW_WEBHOOK_SECRET`
- `LLM_PROVIDER`
- `LLM_API_KEY`
- `LLM_MODEL`
- `ASR_PROVIDER`
- `ASR_API_KEY`
- `ASR_MODEL`
- `DAILY_MESSAGE_LIMIT`
- `DAILY_TOKEN_LIMIT`
- `VOICE_MAX_DURATION_SECONDS`

## 10. 开发里程碑

### M1：协议确认与骨架

- 确认 OpenClaw webhook 和发送接口
- 建立 FastAPI 项目
- 建立数据库迁移
- 实现健康检查
- 实现原始事件落库

### M2：文本闭环

- 实现用户、会话、消息模型
- 实现文本 webhook 处理
- 实现默认 Soul
- 实现 Prompt 组装
- 接入 LLM
- 完成文本回复发送

### M3：多人隔离与限流

- 实现消息去重
- 实现用户级上下文隔离
- 实现短期记忆裁剪
- 实现 Redis 限流
- 实现每日用量统计

### M4：语音输入

- 接收语音事件
- 获取语音文件
- 接入 ASR
- 将识别文本进入统一对话链路
- 实现语音失败降级提示

### M5：稳定性与验收

- 完善错误状态机
- 增加结构化日志
- 增加基础自动化测试
- 使用至少 3 个微信用户做并发与隔离验证
- 完成部署文档

## 11. 测试重点

- 同一用户连续多轮对话上下文正确
- 不同用户之间上下文不串线
- 重复 webhook 不产生重复回复
- 限流命中后不调用 LLM 或 ASR
- ASR 失败不会继续调用 LLM
- LLM 失败会有友好回复
- OpenClaw 发送失败有状态记录
- 服务重启后历史消息和用量统计仍可读取

## 12. 待确认技术问题

- OpenClaw 是否已有纯转发 webhook 模式。
- OpenClaw 主动发送消息 API 的真实路径、鉴权和返回结构。
- 语音消息的媒体获取方式和格式。
- 微信用户唯一 ID 在 OpenClaw 中是否稳定。
- 是否需要白名单机制作为首版默认保护。
- LLM 与 ASR 首选 Provider。
- 是否需要先支持本地开发 mock OpenClaw，以便不依赖真实微信调试。
