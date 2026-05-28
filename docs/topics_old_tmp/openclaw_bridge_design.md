# OpenClaw Bridge 设计

## 1. 目标

在不 fork OpenClaw、不 fork `Tencent/openclaw-weixin` 的前提下，使用官方微信插件接入真实微信，并将 AI 业务逻辑转移到我们的后端服务。

第一版目标：

- 一个 OpenClaw 实例连接多个个人微信账号。
- OpenClaw 负责微信登录、消息接收、消息发送。
- 我们的后端负责账号隔离、记忆、人设、限流、LLM、ASR。
- 能本地跑通真实 OpenClaw 到后端再回微信的完整链路。
- 后续部署到阿里云 Docker 时只改配置，不改核心代码。

### 1.1 Phase 1 产品边界

Phase 1 的“多人使用”指的是：多个个人微信账号连接到同一个 OpenClaw 实例，但每个账号拥有独立 Soul、记忆、会话和配置。

它不是：一个公共服务微信号服务多个外部微信用户。

官方 `openclaw-weixin` 能处理微信登录和多账号连接，但 OpenClaw 原生 `soul.md`、长期记忆和部分配置偏实例级共享。AI4ALL Bridge 的职责是把这些业务能力转移到 Backend，并按微信账号隔离。

Phase 1 暂不建设完整公开 SaaS onboarding，也不做复杂租户体系；先由运营侧接入和管理账号。

### 1.2 身份模型更新

本文是早期 Bridge 方案文档。当前身份模型以 `docs/architecture_overview.md`、`docs/phase1_technical_design.md`、`docs/topics/identity_model_and_wechat_binding.md` 和 `docs/current_status.md` 为准：

- AI4ALL 业务账号 ID 是 Backend 业务隔离主键；未绑定 legacy 入站可由 OpenClaw `session_key` fallback，Web onboarding 绑定完成后路由到预创建 `acct_...`。
- OpenClaw / provider 侧账号 ID 在 Bridge payload 中显式命名为 `channel_account_id`。
- Backend DB/API 中遗留的 `account_id` 字段暂时保留，但语义是 AI4ALL 业务账号 ID。
- Bridge 会继续发送 legacy `account_id` 作为兼容别名；新代码应优先读写 `channel_account_id`，不要把 OpenClaw 原生 `account_id` 当业务隔离主键。

## 2. 总体链路

```text
微信用户
  -> openclaw-weixin 官方插件
  -> OpenClaw Gateway / Agent Loop
  -> ai4all-openclaw-bridge 插件
  -> AI4ALL Backend
  -> ai4all-openclaw-bridge 插件
  -> OpenClaw Gateway
  -> openclaw-weixin 官方插件
  -> 微信用户
```

核心思想：

- `openclaw-weixin` 继续作为微信通道，不做二次开发。
- 新增一个很薄的 OpenClaw Bridge 插件，拦截 OpenClaw 的 agent reply 流程。
- Bridge 插件把当前消息和上下文标识 POST 给我们的后端。
- 后端返回回复文本。
- Bridge 插件把该回复交还给 OpenClaw，由 OpenClaw 通过原通道发回微信。

## 3. 为什么不直接 fork OpenClaw 或 openclaw-weixin

不 fork 的好处：

- 避免维护微信协议适配代码。
- 避免跟随官方 OpenClaw 和微信插件更新。
- 避免直接处理登录态、媒体文件、发送 token、context token 等底层细节。
- 本项目可以聚焦 AI 业务层：多用户、记忆、Soul、限流、成本控制。

只有在以下情况才考虑 fork：

- OpenClaw 没有可用的插件/hook 扩展点。
- Bridge 无法阻止默认 OpenClaw agent 回复，导致重复回复。
- OpenClaw 无法把 Bridge 返回内容原路发回微信。
- 官方插件无法满足必须的媒体访问能力，且没有其他扩展方式。

## 4. 关键组件

### 4.1 openclaw-weixin

职责：

- 微信扫码登录。
- 保持微信连接。
- 接收私聊文本、语音等消息。
- 将微信消息转换成 OpenClaw 内部消息。
- 通过微信发送 OpenClaw 生成的回复。

我们不修改该插件。

### 4.2 ai4all-openclaw-bridge

这是我们新增的 OpenClaw 插件。

职责：

- 挂载在 OpenClaw agent loop 的回复前阶段。
- 捕获当前用户消息、channel、sender、session 等信息。
- 调用 AI4ALL Backend。
- 获取后端返回的回复文本。
- 将回复作为 OpenClaw 的最终回复返回。
- 必要时返回 silence 或错误降级文案。

推荐挂载点：

- 优先：`before_agent_reply`
- 备选：`message:received` + 禁用默认 agent 或其他拦截机制

选择 `before_agent_reply` 的原因：

- 它位于 OpenClaw 默认 LLM 回复前。
- 可以让 Bridge “claim the turn”。
- 可以避免 OpenClaw 默认 agent 和我们的后端同时回复。

实现要求：

- Bridge 必须被配置为当前微信通道的唯一回复来源。
- 如果 OpenClaw 仍存在默认 agent，需要禁用默认 agent、设置空 agent，或确保 Bridge 的 synthetic reply/silence 能稳定短路默认回复。
- 非内置插件如需访问原始会话 hook，需要按 OpenClaw 插件规范开启 conversation hook 访问权限，例如 `allowConversationAccess` 一类配置。
- Bridge 调 Backend 必须设置超时，建议 5-10 秒。
- Backend 超时或异常时，Bridge 返回短降级文案，不继续交给默认 agent。

### 4.3 AI4ALL Backend

职责：

- 接收 Bridge 请求。
- 识别账号和会话。
- 做消息去重。
- 做限流和每日额度。
- 处理语音 ASR。
- 读取短期记忆。
- 注入默认 Soul。
- 组装 Prompt。
- 调用 LLM。
- 保存消息和用量。
- 返回回复文本给 Bridge。

## 5. Backend 接口设计

### 5.1 对话接口

```http
POST /openclaw/turn
Authorization: Bearer <AI4ALL_BRIDGE_SECRET>
Content-Type: application/json
```

请求体：

```json
{
  "event_id": "evt_xxx",
  "message_id": "msg_xxx",
  "channel": "openclaw-weixin",
  "channel_account_id": "wechat_account_xxx",
  "account_id": "wechat_account_xxx",
  "sender_id": "wxid_xxx",
  "sender_name": "用户昵称",
  "chat_id": "wxid_xxx",
  "chat_type": "private",
  "session_key": "openclaw-weixin:wechat_account_xxx:wxid_xxx",
  "message_type": "text",
  "text": "今天有点累",
  "media": null,
  "timestamp": 1710000000,
  "raw": {}
}
```

语音请求体：

```json
{
  "event_id": "evt_voice_xxx",
  "message_id": "msg_voice_xxx",
  "channel": "openclaw-weixin",
  "channel_account_id": "wechat_account_xxx",
  "account_id": "wechat_account_xxx",
  "sender_id": "wxid_xxx",
  "sender_name": "用户昵称",
  "chat_id": "wxid_xxx",
  "chat_type": "private",
  "session_key": "openclaw-weixin:wechat_account_xxx:wxid_xxx",
  "message_type": "voice",
  "text": null,
  "media": {
    "media_id": "media_xxx",
    "url": "https://example.com/voice.amr",
    "path": null,
    "format": "amr",
    "duration_ms": 4200
  },
  "timestamp": 1710000000,
  "raw": {}
}
```

`account_id` 在请求体中只是 legacy 兼容字段。Backend 解析时优先使用 `channel_account_id` 记录通道侧账号，并使用 `session_key` 解析 AI4ALL 业务账号。

响应体：

```json
{
  "status": "ok",
  "reply": "听起来今天确实挺累的。要不要先跟我说说，是事情多，还是心里有点堵？",
  "no_reply": false,
  "metadata": {
    "conversation_id": "conv_xxx",
    "usage_limited": false
  }
}
```

限流响应：

```json
{
  "status": "limited",
  "reply": "今天先聊到这里吧，我晚些时候再继续陪你。",
  "no_reply": false,
  "metadata": {
    "usage_limited": true
  }
}
```

不回复响应：

```json
{
  "status": "ignored",
  "reply": null,
  "no_reply": true,
  "metadata": {}
}
```

### 5.2 健康检查

```http
GET /health
```

返回：

```json
{
  "status": "ok"
}
```

### 5.3 配置与管理接口

Day 1 可只做内部接口，不做 Web UI。

```http
GET /admin/accounts
GET /admin/sessions/{session_key}
PATCH /admin/sessions/{session_key}/soul
POST /admin/users/{sender_id}/blacklist
DELETE /admin/users/{sender_id}/blacklist
```

用途：

- 查看接入账号和 session。
- 调整某个 session 的 Soul。
- 拉黑异常用户。
- 为后续 Web 管理后台预留接口。

Phase 1 不建议直接开放普通微信用户通过聊天指令随意修改 Soul。若支持微信指令，需要先做管理员鉴权，例如只允许白名单管理员发送 `#设置人设`。

## 6. Bridge 插件处理逻辑

伪代码：

```text
on before_agent_reply(context):
  if context.channel != "openclaw-weixin":
    pass through

  if context.chat_type != "private":
    return silence

  payload = normalize_context(context)

  response = POST AI4ALL_BACKEND_URL/openclaw/turn
    timeout = 5-10s

  if response.no_reply:
    return silence

  if response.reply exists:
    return synthetic_reply(response.reply)

  return synthetic_reply("我这边刚刚有点卡住了，你可以稍后再发我一次。")
```

需要关注：

- 必须避免 OpenClaw 默认 agent 继续回复。
- 必须保留 OpenClaw 原始上下文的 channel/account/sender 信息。
- Bridge 不做业务逻辑，只做协议转换和转发。
- Bridge 失败时返回短降级文案，避免用户无感知。
- Bridge 不能因为 Backend 超时而 fallback 到 OpenClaw 默认 agent，否则会破坏人格、记忆和隔离逻辑。

## 7. 用户和会话隔离策略

后端内部会话主键建议：

```text
session_key = channel + ":" + account_id + ":" + chat_id
```

示例：

```text
openclaw-weixin:wechat_account_1:wxid_user_a
```

原因：

- 同一个微信用户可能来自不同接入账号。
- 后续可能有多个微信号。
- 后续可能支持其他 channel。

数据库层面：

- `users.external_user_id = sender_id`
- `conversations.session_key` 唯一
- 所有记忆读取必须基于 `conversation_id`
- 不允许跨 `session_key` 读取历史

## 8. 文本与语音处理

### 8.1 文本消息

```text
Bridge
  -> /openclaw/turn
  -> save user text
  -> load memory
  -> assemble prompt
  -> call LLM
  -> save assistant reply
  -> return reply
```

### 8.2 语音消息

```text
Bridge
  -> /openclaw/turn
  -> backend obtains media url/path
  -> call ASR
  -> recognized text enters same text pipeline
  -> return reply
```

注意：

- Day 1 可以先 mock ASR。
- 真实 ASR 依赖 OpenClaw 能否提供可访问的语音文件 URL 或路径。
- 如果 Bridge 只能拿到媒体 ID，需要进一步确认 OpenClaw 是否提供媒体下载接口。
- 微信语音可能是 AMR、SILK、M4A 等格式，ASR 前可能需要转码。
- 首选方案是 Backend 通过 OpenClaw 提供的 URL/path 获取媒体，并在 Backend 侧完成下载、转码和 ASR，保持 Bridge 足够薄。
- 如果媒体只存在于 OpenClaw 本地容器，Backend 无法访问，则允许 Bridge 做最小媒体代理：读取本地文件、必要时调用 ffmpeg 转为通用格式，再上传给 Backend 或临时暴露给 Backend。
- 不建议默认让 Bridge 直接把所有大媒体二进制同步转发给 Backend。需要设置语音时长、文件大小和超时限制。

## 9. 本地开发与线上部署差异

### 9.1 本地开发

如果 OpenClaw 在本机，Backend 在本机：

```env
AI4ALL_BACKEND_URL=http://localhost:8000
```

如果 OpenClaw 在 Docker，Backend 在宿主机：

```env
AI4ALL_BACKEND_URL=http://host.docker.internal:8000
```

如果 OpenClaw 和 Backend 都在 Docker Compose：

```env
AI4ALL_BACKEND_URL=http://api:8000
```

### 9.2 阿里云部署

推荐同一个 Docker Compose 网络：

```text
openclaw
api
postgres
redis
```

Bridge 配置：

```env
AI4ALL_BACKEND_URL=http://api:8000
AI4ALL_BRIDGE_SECRET=...
```

Backend 配置：

```env
DATABASE_URL=postgresql://...
REDIS_URL=redis://...
LLM_API_KEY=...
ASR_API_KEY=...
```

核心原则：

- 不写死 localhost。
- 不写死容器名。
- 不写死本地文件路径。
- 所有地址、密钥、模式都走环境变量。

## 10. Day 1 开发步骤

### Step 1：确认 OpenClaw 本地可用

验证：

- OpenClaw 能启动。
- `openclaw-weixin` 能扫码登录。
- 微信用户发私聊消息后，OpenClaw 能收到。
- OpenClaw 能正常原路发回消息。

### Step 2：实现 Backend 最小接口

实现：

- `GET /health`
- `POST /openclaw/turn`
- Bearer token 鉴权
- mock LLM 回复
- 入站日志打印

验收：

- curl 调 `/openclaw/turn` 能返回 reply。

### Step 3：实现 Bridge 插件

实现：

- 捕获 OpenClaw 私聊消息。
- 标准化 payload。
- POST 到 Backend。
- 使用 Backend 返回文本作为最终回复。
- 阻止默认 OpenClaw agent 重复回复。

验收：

- 微信用户发消息后，Backend 日志能看到请求。
- 用户能在微信收到 Backend 返回的 mock 回复。
- 不出现 OpenClaw 默认 agent 的第二条回复。

### Step 4：接入真实 LLM

实现：

- 默认 Soul。
- 短期记忆。
- LLM adapter。
- LLM 失败降级。

验收：

- 同一用户 3-5 轮对话上下文连续。
- 不同用户上下文不串线。

### Step 4.5：配置与管理最小接口

实现：

- 查询账号和 session。
- 查询用户用量。
- 修改某个 session 的 Soul。
- 拉黑或恢复用户。

验收：

- 管理者可以通过内部 HTTP 接口调整指定 session 的 Soul。
- 普通聊天用户不能修改其他 session 的配置。

不做：

- Day 1 不做完整 Web 管理后台。
- Day 1 不做多租户账号托管。
- Day 1 不开放普通用户自助扫码托管自己的微信号。

### Step 5：接入语音最小链路

实现：

- Bridge 能识别 voice message。
- Backend 能收到 media 信息。
- 先 mock ASR。
- 如果媒体可访问，再接真实 ASR。

验收：

- 语音消息能进入统一对话链路。

## 11. 主要风险

### 11.1 Bridge 无法 claim turn

表现：

- Backend 回复后，OpenClaw 默认 agent 也回复。

处理：

- 调整 Bridge 挂载点。
- 确认 `before_agent_reply` 是否支持 synthetic reply 或 silence。
- 必要时禁用默认 agent 或设置空 agent。
- 验证插件配置是否开启 conversation hook 访问权限。

### 11.1.1 Backend 超时导致错误回复

表现：

- Backend 超时后，OpenClaw 默认 agent 继续生成回复。
- 用户收到与 AI4ALL 人设、记忆不一致的内容。

处理：

- Bridge 对 Backend 设置 5-10 秒超时。
- 超时后 Bridge 返回固定降级回复或 silence。
- 不允许 fallback 到默认 agent。
- 后续如需长耗时回复，改为异步：先回复“我想一下”，再通过 OpenClaw 发送后续消息。

### 11.2 OpenClaw 上下文字段不足

表现：

- 拿不到稳定 `sender_id`、`chat_id`、`message_id`。

处理：

- 先记录完整 raw context。
- 从 metadata 中提取稳定字段。
- 如果没有 message_id，则用 channel + sender + timestamp + content hash 生成幂等键。

### 11.3 语音媒体不可访问

表现：

- Bridge 只能拿到媒体 ID，后端无法下载语音。

处理：

- Day 1 使用 mock ASR。
- 进一步确认 OpenClaw 是否有媒体下载接口。
- 必要时让 Bridge 负责读取本地媒体并转发给后端。
- 如果是 AMR/SILK 等格式，确认 ASR Provider 是否原生支持；不支持则增加 ffmpeg 转码。

### 11.4 本地和线上网络地址不同

表现：

- 本地可用，Docker/阿里云不可用。

处理：

- 所有 endpoint 通过环境变量配置。
- 同一 Docker Compose 网络内使用服务名访问。
- 本地 Docker 访问宿主机用 `host.docker.internal`。

### 11.5 多账号托管边界扩大

表现：

- 产品从“运营侧接入多个微信账号”变成“多个管理员自助托管多个微信号”。

处理：

- 增加 `tenants`、`managed_accounts`、`account_admins` 数据模型。
- 每个 AI4ALL 业务账号必须绑定 owner/tenant；OpenClaw/provider 侧账号通过 `channel_account_id` 或后续 `channel_bindings` 关联。
- Soul、限流、日志、会话都必须按 tenant/account 隔离。
- 增加 Web 管理后台和登录鉴权。
- 该能力不进入 Day 1。

## 12. 当前需要确认的问题

- OpenClaw 插件开发方式和本地加载方式：已通过本地插件安装验证。
- `before_agent_reply` 的真实 API 形态：已通过真实微信消息验证。
- Bridge 返回 synthetic reply 的具体代码写法：已通过真实微信回复验证。
- 禁用默认 agent 或将 Bridge 设置为唯一回复来源的具体配置方式。
- `openclaw-weixin` 传给 agent loop 的真实 context 字段：已通过 raw payload 查询验证。
- Bridge hook 中稳定的通道侧账号字段已显式命名为 `channel_account_id`；旧 `account_id` 仅作为 payload 兼容别名。未绑定 legacy 入站可 fallback 为 `ctx.sessionKey`，典型格式为 `agent:main:openclaw-weixin:<channel_account_id>:direct:<peer_id>`；绑定完成后应通过 `channel_account_id` / `openclaw_login_session_key` 路由到预创建 `acct_...`。
- 私聊 unknown sender 是否需要 pairing approval。
- 语音消息在 OpenClaw context 中的 media 表达方式。
- 语音媒体格式是否为 AMR/SILK/M4A，以及是否需要 ffmpeg 转码。
- OpenClaw 部署在 Docker 时，Bridge 插件如何配置后端地址。
