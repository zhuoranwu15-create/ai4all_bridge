# 朝夕相伴 App 端接入交接文档（Companion World 3.0）

> 面向：朝夕相伴移动端 App 开发者。
> 服务端状态：Companion World 3.0 已在生产全量激活（aliyun1 中心节点），本文档描述的所有能力均线上可用。
> 最后核对：2026-07-23，对着生产 `https://ai4company.top` 实测。

---

## 1. 一句话架构（App 开发必读）

- **App 只对接中心节点 aliyun1 的 `/v1` API**，公网入口 `https://ai4company.top/api/v1/`。
- Companion World 全部功能（世界、居民、Feed、信箱、访问、真人会话）**只经 `/v1` 客户端 API 暴露**，且只在中心节点挂载。
- **单节点约束（v1 重构现状，已知并接受）**：`/v1` 目前仅由 aliyun1 提供服务；aliyun2 是「厚节点」，只本地处理它归属微信账号的入站 turn，不服务 `/v1`。App 无需关心节点归属，永远只打中心域名。多节点接入 App 是后续「小重构」的事，当前不影响 App 开发。
- 微信渠道与 App 渠道是两条独立入站路径；App 的会话走 `/v1`，与微信 turn 互不干扰。

---

## 2. 接入基础

| 项 | 值 |
|---|---|
| 公网 Base URL | `https://ai4company.top/api/v1/` |
| 交互式 API 文档（Swagger） | `https://ai4company.top/api/docs`（如未开放对外，本地对着服务 `/docs`） |
| 鉴权方式 | 手机号 OTP 登录 → 30 天 session token（`Authorization: Bearer <access_token>`） |
| 内容长度上限 | 文本 4000 字符；音频 10 MB / 60s（见 `/app/config` `limits`） |
| 时区 | 服务端统一 Asia/Shanghai（+08:00），所有时间字段带 `+08:00` 偏移 |

### 2.1 nginx 前缀说明

公网 `/api/v1/...` 会原样转发到后端，后端中间件剥掉 `/api` 前缀后匹配路由的 `/v1` 前缀。**App 请求路径一律带 `/api/v1`**，例如 `POST https://ai4company.top/api/v1/auth/otp/send`。

### 2.2 两种响应约定（重要）

服务端有两套响应外壳，App 需分别处理：

**A. 账户 / 会话 / 聊天类（`/app/config`、`/auth/*`、`/me`、`/chat/*`、`/audio/*`）** — 扁平结构：

```json
{ "status": "ok", "...业务字段...": "..." }
```

**B. 世界类（`/worlds/*`、`/conversations`、`/ai-conversations/*`、`/mailbox/*`、`/visits/*`、`/world/invites`、`/notifications`、`/human-conversations/*`）** — 统一信封：

```json
{
  "code": "ok",
  "request_id": "<每次请求唯一>",
  "server_time": "2026-07-23T14:05:00+08:00",
  "data": { "...业务数据..." }
}
```

失败时（B 类）：`code` 为错误码（见 §7），无 `data`，`message` 为 `null`。HTTP 状态码同时反映语义（401/404/409/429 等）。

---

## 3. 登录鉴权流程

```
GET  /v1/app/config                → 拿 captcha 配置 + 功能开关 + 限额
POST /v1/auth/otp/send             → 发送短信验证码（需先过阿里云行为验证码）
POST /v1/auth/otp/verify           → 校验 OTP，拿一次性 verified_token
POST /v1/auth/session              → 用 verified_token 建立 30 天 session
   ↳ 拿到 access_token，后续所有请求带 Authorization: Bearer <access_token>
DELETE /v1/auth/session/current    → 登出（吊销当前 token）
```

### 3.1 `GET /v1/app/config`（无需鉴权）

线上实测返回：

```json
{
  "captcha": { "provider": "aliyun", "scene_id": "6ez3x2ne", "prefix": "18if8u", "configured": true },
  "features": { "voice_input": false },
  "limits": { "message_chars": 4000, "audio_bytes": 10485760, "audio_duration_ms": 60000 },
  "minimum_supported_version": "0.1.0"
}
```

- `captcha`：阿里云验证码集成参数，App 端用 `scene_id` / `prefix` 初始化验证码 SDK。`configured=true` 表示服务端已配好。
- `features.voice_input`：语音输入当前**关闭**（`/audio/transcriptions` 端点存在，但产品开关未开）。
- `minimum_supported_version`：低于此版本应提示强制升级。

### 3.2 `POST /v1/auth/otp/send`

请求体：手机号 + 阿里云验证码校验参数。返回 `{ "status": "ok" }` 或错误。

### 3.3 `POST /v1/auth/otp/verify`

请求体：手机号 + 用户输入的 OTP。返回：

```json
{ "status": "...", "verified_token": "<一次性令牌>" }
```

### 3.4 `POST /v1/auth/session`

请求体：

```json
{
  "phone": "138xxxx0000",
  "verified_token": "<上一步返回>",
  "invite_code": null,     // 可选：世界访问邀请码
  "campaign_code": null    // 可选：营销活码
}
```

返回：

```json
{
  "status": "ok",
  "access_token": "<Bearer token，30 天有效>",
  "expires_at": "2026-08-22T14:05:00+08:00",
  "is_new_user": true,
  "platform_user": { "id": "...", "phone_masked": "138****0000" },
  "account": null,
  "welcome_message": null
}
```

---

## 4. 身份模型与「新用户走世界引导」（P1 已开启）

生产已开 `COMPANION_WORLD_P1_ENABLED=true`，身份模型如下：

- 登录主体是 **platform_user（手机号）**，不是微信账号。
- **老用户**（手机号命中既有 platform_user，例如曾用该号绑定过的用户）：`account` 返回既有账号，`welcome_message` 非空，App 可直接进聊天。
- **新用户**（`is_new_user=true` 且零绑定）：`account` 返回 **`null`**，`welcome_message` 为 `null`。这是**刻意设计**——新用户不预建默认运行账号，而是**引导进入世界 bootstrap 流程**（见 §5）。

> App 端判定：`account == null` → 进入「世界初始化 / 选居民」引导页；`account != null` → 直接进主聊天。

---

## 5. 世界引导流程（account == null 时）

```
POST /v1/worlds/home/bootstrap          → 创建/获取家园世界 + 返回候选居民
GET  /v1/worlds/home/resident-candidates → （可重新拉取候选）
POST /v1/worlds/home/residents/confirm  → 确认选定的居民，正式入住
GET  /v1/worlds/home/residents          → 列出已入住居民（含会话 id）
```

### 5.1 `POST /v1/worlds/home/bootstrap`

`data`：

```json
{
  "world": { "id": "...", "status": "...", "onboarding_state": "..." },
  "candidates": [ { /* 见下方候选结构 */ } ]
}
```

### 5.2 候选居民结构（`_candidate_data`）

```json
{
  "template_id": "tmpl_ops_v1_1",
  "template_version": "v1",
  "name": "林小满",
  "avatar_ref": "https://ai4company.top/companion_world/avatars/linxiaoman.png",
  "summary": "温柔的倾听者……",
  "tags": ["温柔", "共情", "治愈"],
  "origin": "...",
  "status": "..."
}
```

> **刻意不返回** `persona_seed_json` 与内部 resident/runtime id。App 只按 `template_id` 提交选择。

**首发 4 位官方候选**（生产 preset v1）：

| template_id | 名字 | 标签 | 头像 |
|---|---|---|---|
| `tmpl_ops_v1_1` | 林小满 | 温柔/共情/治愈 | `…/avatars/linxiaoman.png` |
| `tmpl_ops_v1_2` | 陆星野 | 活泼/好奇/元气 | `…/avatars/luxingye.png` |
| `tmpl_ops_v1_3` | 沈川 | 沉稳/理性/可靠 | `…/avatars/shenchuan.png` |
| `tmpl_ops_v1_4` | 阿糖 | 幽默/俏皮/轻松 | `…/avatars/atang.png` |

### 5.3 `POST /v1/worlds/home/residents/confirm`

请求体：

```json
{ "selections": [ { "template_id": "tmpl_ops_v1_1", "display_name": "小满" } ] }
```

`data`：`{ "residents": [ { /* 见下方居民结构 */ } ] }`

### 5.4 居民结构（`_resident_data`）

```json
{
  "resident_id": "...",
  "name": "林小满",
  "avatar_ref": "https://ai4company.top/companion_world/avatars/linxiaoman.png",
  "status": "...",
  "origin": "...",
  "conversation_id": "<用于发起对话>",
  "conversation_state": "active"
}
```

> **不暴露 runtime account id**。App 用 `conversation_id` 发起 turn。

### 5.5 头像处理

`avatar_ref` 是**透传字符串**（当前为完整 URL）。App 直接把它当图片地址加载即可，不要假设其内部格式，未来可能变为相对 ref。官方头像托管在 `https://ai4company.top/companion_world/avatars/{linxiaoman,luxingye,shenchuan,atang}.png`。

---

## 6. 会话与世界内容

### 6.1 与居民对话

```
GET  /v1/conversations                              → 会话列表
GET  /v1/ai-conversations/{conversation_id}/messages → 历史消息
POST /v1/ai-conversations/{conversation_id}/turn     → 发一条消息，拿 AI 回复
```

`POST .../turn` 请求体：

```json
{ "client_message_id": "<客户端幂等键>", "text": "你好" }
```

`data`：

```json
{
  "reply": { "text": "……", "message_id": "..." },
  "no_reply": false,
  "deduplicated": false
}
```

- **`client_message_id` 必须由客户端生成且在会话内唯一**，用于幂等去重（网络重试不会产生重复回复，`deduplicated=true` 表示命中去重）。
- 并发保护：同一会话若上一条 turn 未完成，返回 `turn_in_progress`（409）。
- `no_reply=true` 表示这一轮 AI 选择不回复（正常业务态，非错误）。

### 6.2 家园 Feed

```
GET  /v1/worlds/home/feed?cursor=<游标>&limit=20   → 分页拉取 Feed（limit 1-50，默认 20）
POST /v1/worlds/home/feed/posts                    → 用户发帖
```

`data`：`{ "items": [ /* Feed 项 */ ], "next_cursor": "<游标或 null>" }`。用 `next_cursor` 向后翻页，为 `null` 表示到底。

Feed 项结构含 `post_id / author{type,resident_id,name,avatar_ref} / content{type:"text",text} / post_type / source / published_at`。

**Feed 生成时间窗（Asia/Shanghai）**：早间 `07:00–11:00`、晚间 `18:00–23:00`。居民自动发帖由中心调度器在窗口内产生；窗口外一般无新 AI Feed。

### 6.3 其他世界能力（均已激活，端点见 §8）

- **信箱** `/mailbox/*`：角色来信，接受/婉拒/延后/已读、未读数。
- **访问 / 邀请** `/visits/*`、`/world/invites`：世界互访、生成/核销邀请码。
- **通知** `/notifications`：未读数、标记已读。
- **真人会话** `/human-conversations/*`：真人对真人聊天、举报/拉黑/隐藏。

---

## 7. 错误码（B 类信封 `code` → HTTP 状态）

| code | HTTP | 含义 |
|---|---|---|
| `unauthorized` | 401 | 无/失效 token |
| `not_found` | 404 | 功能未开或资源不存在 |
| `account_id_not_accepted` | 400 | 请求体不允许携带 account_id/universe_id 等内部字段 |
| `invalid_request` | 422 | 参数校验失败 |
| `world_not_ready` / `world_disabled` | 409 / 403 | 世界未就绪 / 被禁用 |
| `resident_capacity_exceeded` / `resident_selection_invalid` / `resident_already_exists` | 409 / 400 / 409 | 居民容量/选择/重复 |
| `conversation_not_found` / `conversation_read_only` / `turn_in_progress` | 404 / 409 / 409 | 会话不存在 / 只读 / 正在处理 |
| `rate_limited` | 429 | 触发限流 |
| `account_disabled` | 403 | 账号被停用 |
| `invalid_cursor` | 400 | Feed 游标非法 |
| `letter_not_found` / `letter_not_open` / `letter_expired` | 404 / 409 / 409 | 信箱来信状态 |
| `invalid_invite_code` / `invite_expired` / `visit_*` | 400 / 409 / … | 邀请与访问相关 |

> 完整表见 `app/routers/companion_world.py` 的 `_ERROR_STATUS`。App 应基于 `code`（而非文案）做分支，未知 `code` 按对应 HTTP 状态兜底。

---

## 8. `/v1` 全量端点清单（49 条）

**鉴权 / 账户**
- `GET /v1/app/config`
- `POST /v1/auth/otp/send`、`POST /v1/auth/otp/verify`
- `POST /v1/auth/session`、`DELETE /v1/auth/session/current`
- `GET /v1/me`

**主聊天（默认账号，非世界）**
- `GET /v1/chat/messages`、`POST /v1/chat/turn`
- `POST /v1/audio/transcriptions`（语音转写，功能开关当前关闭）

**世界 / 家园**
- `POST /v1/worlds/home/bootstrap`
- `GET /v1/worlds/home/resident-candidates`
- `GET /v1/worlds/home/residents`、`POST /v1/worlds/home/residents`、`POST /v1/worlds/home/residents/confirm`
- `GET /v1/worlds/home/feed`、`POST /v1/worlds/home/feed/posts`

**世界内会话**
- `GET /v1/conversations`
- `GET /v1/ai-conversations/{id}/messages`、`POST /v1/ai-conversations/{id}/turn`

**信箱**
- `GET /v1/mailbox/letters`、`GET /v1/mailbox/letters/{id}`、`GET /v1/mailbox/unread-count`
- `POST /v1/mailbox/letters/{id}/read`、`/accept`、`/decline`、`/defer`

**访问 / 邀请**
- `GET /v1/visits`、`POST /v1/visits/redeem`
- `GET /v1/visits/{id}/feed`
- `POST /v1/visits/{id}/accept`、`/reject`、`/cancel`、`/leave`、`/revoke`、`/block`
- `POST /v1/world/invites`、`GET /v1/world/invites`、`DELETE /v1/world/invites/{id}`

**通知**
- `GET /v1/notifications`、`GET /v1/notifications/unread-count`
- `POST /v1/notifications/{id}/read`、`POST /v1/notifications/read-all`

**真人会话**
- `GET /v1/human-conversations`
- `GET /v1/human-conversations/{id}/messages`、`POST /v1/human-conversations/{id}/messages`
- `POST /v1/human-conversations/{id}/read`、`/report`、`/block`
- `DELETE /v1/human-conversations/{id}/entry`

---

## 9. 生产开关现状（截至 2026-07-23，aliyun1）

全部 Companion World 功能开关已开启：

| flag | 状态 |
|---|---|
| `COMPANION_WORLD_P1_ENABLED` | true（新用户走世界引导） |
| `COMPANION_WORLD_FEED_ENABLED` | true（早 07:00–11:00 / 晚 18:00–23:00） |
| `COMPANION_WORLD_APP_INBOX_ENABLED` | true |
| `COMPANION_WORLD_LIFECYCLE_EVALUATION_ENABLED` / `..._COMMIT_ENABLED` | true |
| `COMPANION_WORLD_MAILBOX_ENABLED` | true |
| `COMPANION_WORLD_VISITS_ENABLED` | true |
| `COMPANION_WORLD_HUMAN_CHAT_ENABLED` | true |

> 若某功能返回 `not_found`(404)，先确认对应 flag 是否被临时收回——功能门控以服务端 flag 为准。

---

## 10. 联调建议

1. 先打 `GET /api/v1/app/config` 确认连通与 captcha 参数。
2. 走完整登录链路拿到 `access_token`，之后所有请求带 `Authorization: Bearer <token>`。
3. 新号验证 `account == null` → bootstrap → confirm → 用返回的 `conversation_id` 发 turn。
4. 所有写操作（turn、发帖等）带客户端幂等键，验证断网重试不产生重复。
5. 遇到 4xx，按 §7 的 `code` 做分支，不要依赖文案。
6. 交互式契约以线上 Swagger `/docs` 为准；本文档为落地口径与流程约定。
