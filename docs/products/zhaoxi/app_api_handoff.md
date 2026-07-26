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
| 交互式 API 文档（Swagger） | **未对外暴露**（2026-07-26 实测 `/api/docs` 返回官网 SPA）；契约以仓库提交的 [`openapi/app_v1.json`](openapi/app_v1.json) 为准（见 §2.3），或本地起服务访问 `/docs` |
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

### 2.3 OpenAPI 契约 snapshot

仓库提交了客户端契约的 OpenAPI 快照：[`openapi/app_v1.json`](openapi/app_v1.json)（49 条 `/v1` 路径）。

- 服务端 CI 断言「实时导出 == 提交的 snapshot」，所以**改响应字段必须同步更新 snapshot**，否则后端 CI 直接红。客户端可以拿它做 breaking-change 检查或生成 DTO。
- 后端重新导出：`.venv/bin/python scripts/export_openapi.py`（`--check` 只校验）。
- **当前只有主链路 10 个端点有真实响应 schema**：`/app/config`、`/me`、`worlds/home/bootstrap`、`resident-candidates`、`residents`、`residents/confirm`、`conversations`、`ai-conversations/{id}/messages|turn|read`。其余端点只冻结了路径与请求体，响应形状以本文档为准——这是分步交付的既定范围，不是遗漏。
- 生成的 DTO 不替代客户端领域模型；本文档仍是落地口径与流程约定。

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
  "features": {
    "voice_input": false,
    "resident_world": true,
    "world_feed": true,
    "app_notifications": true,
    "resident_lifecycle": true,
    "mailbox": true,
    "world_visits": true,
    "human_chat_send": true
  },
  "limits": { "message_chars": 4000, "audio_bytes": 10485760, "audio_duration_ms": 60000 },
  "client_contract_version": "2026-07-26",
  "server_time": "2026-07-26T12:00:00+08:00",
  "minimum_supported_version": "0.1.0",
  "minimum_supported_version_by_platform": { "ios": "0.0.0", "android": "0.0.0" }
}
```

- `captcha`：阿里云验证码集成参数，App 端用 `scene_id` / `prefix` 初始化验证码 SDK。`configured=true` 表示服务端已配好。
- `features.*`：**能力开关，字段只加不改**。它回答的是「这个能力现在能不能用」，不暴露内部 flag 名或阈值。
  - 客户端必须区分三种状态：capability=`false`（明确关闭）、请求失败/5xx/非法响应（可恢复错误）、capability=`true`（走世界流程）。后两者不能当成关闭处理。
  - `resident_world=false` 时其余世界能力恒为 `false`。
  - `human_chat_send` 只表示**能不能发**真人消息；真人聊天的**读**随 `resident_world`。开读关写时不要整块隐藏历史。
  - `resident_lifecycle` 对应居民真正会下线的开关，不是只跑评估不落地的那个。
- `client_contract_version`：服务端 App 契约版本，改契约时上调。
- `minimum_supported_version` / `minimum_supported_version_by_platform`：低于此版本应提示强制升级；分平台字段先按 `0.0.0`（不拦）上线。
- 响应带 `Cache-Control: no-store`。

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

> `invite_code` 是**历史 referral 字段**，走注册返利逻辑，**不是**世界来访码。
> 世界来访码只在登录之后提交给 `POST /visits/redeem`。

### 3.5 `GET /v1/me`（Session 恢复入口）

```json
{
  "status": "ok",
  "platform_user": { "id": "...", "phone_masked": "138****0000" },
  "account": null,
  "world": { "id": "uni_...", "status": "active", "onboarding_state": "selecting" },
  "server_time": "2026-07-26T12:00:00+08:00"
}
```

- **`account` 允许为 `null`**：还没确认居民的用户就是这个状态，不是错误。`/me` 不再返回
  `account_not_ready`(409)，App 被杀进程/切后台/换设备后都能凭 Session 恢复到未完成的引导。
- `401` 只表示 Session 失效，是唯一应该触发重新登录的状态。
- `world` 在能力关闭或用户尚未 bootstrap 时为 `null`。**`/me` 只读不建**——建世界仍然只由
  `POST /worlds/home/bootstrap` 负责。
- `account.status == "disabled"` 仍返回 403。
- 响应带 `Cache-Control: no-store`。

---

## 4. 身份模型与「新用户走世界引导」（P1 已开启）

生产已开 `COMPANION_WORLD_P1_ENABLED=true`，身份模型如下：

- 登录主体是 **platform_user（手机号）**，不是微信账号。
- **老用户**（手机号命中既有 platform_user，例如曾用该号绑定过的用户）：`account` 返回既有账号，`welcome_message` 非空。
- **新用户**（`is_new_user=true` 且零绑定）：`account` 返回 **`null`**，`welcome_message` 为 `null`。这是**刻意设计**——新用户不预建默认运行账号。

> **⚠️ 不要再用 `account == null` 或 `is_new_user` 判断世界引导状态。** 它们只用于兼容旧认证响应。
> `resident_world` 能力开启时，**所有已登录用户**（新老一律）都调用幂等的
> `POST /worlds/home/bootstrap`，并以 `world.onboarding_state` 决定进引导页还是主页。
>
> 微信老用户同样进选择角色页（免注册、直接登录），其微信侧既有角色会被**带入世界**并出现在
> bootstrap 的 `existing_residents` 里 —— 已经在世界里、不可被叉掉、占 1–10 名额。
> 因此老用户即使叉掉全部 4 位预设候选，也能以空 `selections` 完成确认。

---

## 5. 世界引导流程（account == null 时）

```
POST /v1/worlds/home/bootstrap          → 创建/获取家园世界 + 返回候选居民
GET  /v1/worlds/home/resident-candidates → （可重新拉取候选）
POST /v1/worlds/home/residents/confirm  → 确认选定的居民，正式入住
GET  /v1/worlds/home/residents          → 列出已入住居民（含会话 id）

# 自建角色（两步，见 §5.6）
GET  /v1/worlds/home/resident-options            → 受控取值表（关系/性格/头像）
POST /v1/worlds/home/resident-drafts/preview     → 预览人设，拿 draft_token
POST /v1/worlds/home/residents                   → 用 draft_token 落地
```

### 5.1 `POST /v1/worlds/home/bootstrap`

`data`：

```json
{
  "world": { "id": "...", "status": "...", "onboarding_state": "..." },
  "candidates": [ { /* 见下方候选结构 */ } ],
  "existing_residents": [ { /* 与 /worlds/home/residents 同结构 */ } ]
}
```

- `existing_residents`：**已经在这个世界里**的居民。对微信老用户，这里是被带入的既有角色
  （`origin="legacy"`）；纯新用户为空数组。它们不在 `candidates` 里，客户端不应提供「叉掉」操作。
- 微信侧从未起过名字的角色，展示名回落为 `来自微信的Bot`。
- 幂等：重复调用不重复创建世界、候选或带入居民；`confirmed` 状态不会退回 `selecting`。

### 5.2 候选居民结构（`_candidate_data`）

```json
{
  "template_id": "tmpl_ops_v1_1",
  "template_version": "v1",
  "name": "林小满",
  "avatar_ref": "https://ai4company.top/companion_world/avatars/linxiaoman.png",
  "summary": "温柔的倾听者……",
  "long_summary": "……角色预览页用的长介绍，可能为 null",
  "tags": ["温柔", "共情", "治愈"],
  "origin": "...",
  "status": "...",
  "persona_key": "linxiaoman",
  "suggested_display_name": "小满",
  "naming_version": "np_v1",
  "naming_status": "ready"
}
```

> **刻意不返回** `persona_seed_json` 与内部 resident/runtime id。App 只按 `template_id` 提交选择。

命名字段（NAME-001 / CAND-001）：

- `suggested_display_name` 是**服务端已快照**的建议实例名，同一 world + 候选永远返回同值，
  重复 bootstrap、换设备、重装都不变，也不随服务端选名算法升级而变化。
- `naming_status`：`ready` = 已有建议名；`unavailable` = 运营尚未给该模板配名池
  （此时 `suggested_display_name` 与 `naming_version` 均为 `null`）。**命名不可用不会让
  bootstrap 失败**，候选照常下发，客户端按契约回落到自己的本地兜底名池。
- `naming_version` 是名池版本号，仅供排查；客户端不应据此重新选名。
- `persona_key` 跨模板版本稳定，用于客户端在模板换版后仍认出「同一个人设」；
  `template_id` / `template_version` 会随内容变更而更换，`persona_key` 不会。
- `long_summary` 为角色预览页的长介绍，运营未录入时为 `null`（`sample_dialogue` 在 P2）。

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

`display_name` 可省略。省略时服务端依次回落：候选的 `suggested_display_name` →
模板工作名（`name`）。传入时会过展示名白名单（拒表情、控制字符、超长），
不合法返回 `display_name_invalid`(400)；沿用服务端值时不受该限制。

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

### 5.6 自建角色（两步：预览 → 落地）

自建角色**不接受**裸自由文本人设。客户端先拉受控取值表，用户在选择器里填结构化设定，
服务端渲染出人设并回一份预览；用户确认后再用 `draft_token` 落地。
草稿有效期 **30 分钟**，且**单次消费**。

**第 0 步 `GET /v1/worlds/home/resident-options`** —— 取值表以服务端为准，**客户端不要硬编码枚举**：

```json
{
  "relationship_types": [ { "key": "friend", "label": "朋友", "requires_label": false },
                          { "key": "custom", "label": "自定义关系", "requires_label": true } ],
  "personality_traits": [ { "key": "gentle", "label": "温柔" } ],
  "personality_trait_limits": { "min": 1, "max": 3 },
  "avatars": [ { "key": "linxiaoman", "avatar_ref": "https://…/linxiaoman.png" } ],
  "limits": { "display_name_chars": 20, "relationship_label_chars": 20, "style_note_chars": 500 }
}
```

**第 1 步 `POST /v1/worlds/home/resident-drafts/preview`**：

```json
{
  "name": "小满",
  "avatar_key": "linxiaoman",
  "relationship_type": "sibling",
  "relationship_label": null,
  "personality_traits": ["gentle", "empathetic"],
  "style_note": "说话温和，喜欢先听我说完"
}
```

- `avatar_key` / `relationship_type` / `personality_traits` 只能取上一步下发的 key；自造值 400。
- `relationship_type="custom"` 时必须给 `relationship_label`，其余关系忽略该字段。
- `style_note`、`relationship_label`、`name` 是自由文本，**在此处一次性过安全审查**：
  有风险则**自动改写**（返回值即最终值，不会额外提示「内容已被修改」），命中红线整体拒绝。

`data`：

```json
{
  "draft_id": "...",
  "draft_token": "<一次性令牌>",
  "expires_at": "2026-07-26T12:30:00+08:00",
  "name": "小满",
  "avatar_ref": "https://…/linxiaoman.png",
  "relationship_display": "兄弟姐妹",
  "tags": ["温柔", "共情"],
  "normalized_summary": "小满，你的兄弟姐妹，温柔、共情。说话温和，喜欢先听我说完",
  "ai_identity_notice": "TA 是你世界里的一位 AI 居民……"
}
```

> `name` 与 `normalized_summary` 是**审查后**的文本。若与用户输入不同，直接按返回值展示。
> `ai_identity_notice` 建议在预览页固定展示。

**第 2 步 `POST /v1/worlds/home/residents`**（同一端点也用于加入预设模板）：

```json
{ "draft_token": "<上一步的 token>", "client_request_id": "<客户端幂等键>" }
```

- 二选一：`template_id`（加入预设角色）**或** `draft_token`（自建）。同时给或都不给 → 422。
- `draft_token` 必须配 `client_request_id`；同一 `client_request_id` 重放返回**同一结果 200**
  （断网重试/重复点击不会产生第二位居民）。换一个 `client_request_id` 重放同一 token →
  `resident_draft_consumed`(409)。
- `data`：selecting 阶段返回 `{ "candidate": { /* 候选结构 */ } }`（等确认时一并入住）；
  confirmed 阶段直接激活，返回 `{ "resident": { /* 居民结构 */ } }`。
- selecting 阶段最多只能有 **1 个**自建候选，超出 `custom_candidate_limit_exceeded`(409)。

---

## 6. 会话与世界内容

### 6.1 与居民对话

```
GET  /v1/conversations                              → 会话列表
GET  /v1/ai-conversations/{conversation_id}/messages → 历史消息
POST /v1/ai-conversations/{conversation_id}/turn     → 发一条消息，拿 AI 回复
POST /v1/ai-conversations/{conversation_id}/read     → 标记已读到某条消息
```

**会话列表项**（`data.items[]`，2026-07-26 起字段冻结）：

```json
{
  "conversation_id": "conv_...",
  "resident_id": "res_...",
  "resident_name": "小满",
  "resident_avatar_ref": "https://.../avatar.png",
  "resident_status": "active",
  "state": "active",
  "last_preview": "我在的",
  "last_message_at": "2026-07-26T14:05:00+08:00",
  "sort_time": "2026-07-26T14:05:00+08:00",
  "unread": 2,
  "can_send": true,
  "read_only_reason": null
}
```

- **`sort_time` 是排序与分页的锚，`last_message_at` 是最近一条可见消息的时间**，两者不同：还没聊过的居民 `last_message_at` 与 `last_preview` 均为 `null`（服务端不伪造消息时间），但 `sort_time` 始终有值。列表按 `sort_time` 倒序，客户端请直接沿用服务端顺序。
- `can_send=false` 时不要发起 turn，用 `read_only_reason`（目前仅 `resident_offline`）决定 UI 文案；硬发会拿到 `conversation_read_only`（409）。请按**错误码**而非文案分支。
- `unread` = 该会话中 id 大于已读游标的 **AI 消息**条数；用户自己发的不计。
- 预览与未读只统计 App 内的会话消息，微信渠道的历史不会串进来。

**`POST .../read`** 请求体 `{ "last_message_id": 123 }`（`last_message_id` 取 `/messages` 返回项的数值 `id`，必须 ≥ 1）。`data`：

```json
{ "conversation_id": "conv_...", "last_read_message_id": 123, "unread": 0 }
```

- 幂等：重复上报同一个 id 结果不变。游标**只前进不回退**，且会向该会话真实最新一条消息收敛——传一个很大的数不会把之后到达的消息也标成已读。
- 标记已读**不改变** `sort_time`，列表不会因此跳序。
- 越权与不存在同样返回 `conversation_not_found`（404）。

`POST .../turn` 请求体：

```json
{ "client_message_id": "<客户端幂等键>", "text": "你好" }
```

`data`（2026-07-26 起形状冻结）：

```json
{
  "reply": { "text": "……", "message_id": "..." },
  "no_reply": false,
  "deduplicated": false
}
```

- **`client_message_id` 必须由客户端生成且在会话内唯一**，用于幂等去重（网络重试不会产生重复回复，`deduplicated=true` 表示命中去重）。
- **重放返回的 `reply.message_id` 与首次完全一致**，客户端据此判定是同一条消息，不要新建气泡。
- `no_reply=true` 表示这一轮 AI 选择不回复（正常业务态，非错误），此时 **`reply` 整体为 `null`**；不会出现「有 `reply` 对象但 `text` 为 `null`」的中间态。只要 `reply` 非 `null`，`text` 就一定非空。
- 并发保护：同一会话若上一条 turn 未完成，返回 `turn_in_progress`（409）。

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
| `feature_disabled` | 404 | **能力被关闭**（与资源不存在分开；请求前先看 `/app/config` capability） |
| `not_found` | 404 | 资源不存在 |
| `account_id_not_accepted` | 400 | 请求体不允许携带 account_id/universe_id 等内部字段 |
| `invalid_request` | 422 | 参数校验失败 |
| `world_not_ready` / `world_disabled` | 409 / 403 | 世界未就绪 / 被禁用 |
| `resident_capacity_exceeded` / `resident_selection_invalid` / `resident_already_exists` | 409 / 400 / 409 | 居民容量/选择/重复 |
| `avatar_key_invalid` / `relationship_type_invalid` / `relationship_label_required` | 400 | 自建角色受控取值非法（先拉 `/worlds/home/resident-options`） |
| `personality_trait_invalid` / `personality_trait_count_invalid` | 400 | 性格标签不在白名单 / 数量不在 1–3 |
| `display_name_invalid` | 400 | 展示名含表情、控制字符或超长 |
| `custom_candidate_limit_exceeded` | 409 | selecting 阶段自建候选已有 1 个 |
| `resident_draft_not_found` / `resident_draft_expired` / `resident_draft_consumed` | 404 / 409 / 409 | 草稿不存在（含越权）/ 已过期（30 分钟）/ 已被消费（重新预览） |
| `content_rejected` | 422 | 自由文本命中安全红线，改写救不回；提示用户换一种写法 |
| `content_review_unavailable` | 503 | 安全审查暂不可用，**可重试**；不会放行未审查文本 |
| `conversation_not_found` / `conversation_read_only` / `turn_in_progress` | 404 / 409 / 409 | 会话不存在 / 只读 / 正在处理 |
| `rate_limited` | 429 | 触发限流 |
| `account_disabled` | 403 | 账号被停用 |
| `invalid_cursor` | 400 | Feed 游标非法 |
| `letter_not_found` / `letter_not_open` / `letter_expired` | 404 / 409 / 409 | 信箱来信状态 |
| `invalid_invite_code` / `invite_expired` / `visit_*` | 400 / 409 / … | 邀请与访问相关 |

> 完整表见 `app/products/zhaoxi/api/companion_world.py` 的 `_ERROR_STATUS`。App 应基于 `code`（而非文案）做分支，未知 `code` 按对应 HTTP 状态兜底。

主链路 10 个端点的 401/403/404/409/422/429 已在 OpenAPI snapshot 里声明为错误信封
（`WorldErrorEnvelope`），可直接据此生成错误分支；具体 `code` 取值仍以上表为准。

---

## 8. `/v1` 全量端点清单（52 条）

**鉴权 / 账户**
- `GET /v1/app/config`
- `POST /v1/auth/otp/send`、`POST /v1/auth/otp/verify`
- `POST /v1/auth/session`、`DELETE /v1/auth/session/current`
- `GET /v1/me`

**主聊天（默认账号，非世界）**
- `GET /v1/chat/messages`、`POST /v1/chat/turn`
- `POST /v1/audio/transcriptions`（语音转写，功能开关当前关闭）

> 2026-07-26 起这两个 legacy 端点与世界端对齐：`/chat/messages` 的 `created_at` 显式带
> `+08:00`（此前是无时区的裸时间串，客户端如按本地时区解析过需回归一次）；`/chat/turn` 的
> `metadata` 增 `message_id`，且**重放返回与首次相同的 `message_id`**。请求体与其余字段不变。

**世界 / 家园**
- `POST /v1/worlds/home/bootstrap`
- `GET /v1/worlds/home/resident-candidates`、`GET /v1/worlds/home/resident-options`
- `POST /v1/worlds/home/resident-drafts/preview`
- `GET /v1/worlds/home/residents`、`POST /v1/worlds/home/residents`、`POST /v1/worlds/home/residents/confirm`
- `GET /v1/worlds/home/feed`、`POST /v1/worlds/home/feed/posts`

**世界内会话**
- `GET /v1/conversations`
- `GET /v1/ai-conversations/{id}/messages`、`POST /v1/ai-conversations/{id}/turn`
- `POST /v1/ai-conversations/{id}/read`

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
6. 机器可读契约用仓库里的 [`openapi/app_v1.json`](openapi/app_v1.json)（见 §2.3，线上未开放 Swagger）；本文档为落地口径与流程约定。
7. 正式版客户端开发的精简入口见 [`app_client_brief.md`](app_client_brief.md)。
