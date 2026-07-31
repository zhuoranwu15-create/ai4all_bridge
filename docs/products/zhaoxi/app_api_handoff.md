# 朝夕相伴 App 端接入交接文档（Companion World 3.0）

> 面向：朝夕相伴移动端 App 开发者。
> 服务端状态：Companion World 3.0 已在生产全量激活（aliyun1 中心节点），本文档描述的所有能力均线上可用。
> 最后核对：2026-07-23，对着生产 `https://ai4company.top` 实测。
> **v1.5（会话与动态媒体 + 许愿创建居民）服务端已上线**（2026-07-30），契约见 §6.5 / §6.6。
> 生产已于 **2026-07-31** 配好 `MEDIA_URL_SIGNING_SECRET`，四个能力位现在全部下发 `true`，
> 媒体链路可直接联调。仍请一律以 `/app/config` 的能力位渲染入口，细节见 §9.1。

---

## 1. 一句话架构（App 开发必读）

- **App 只对接中心节点 aliyun1 的产品 API**，新客户端规范公网入口为
  `https://ai4company.top/api/v1/products/zhaoxi/`；既有 `https://ai4company.top/api/v1/`
  继续兼容并固定为朝夕 audience。
- Companion World 全部功能（世界、居民、Feed、信箱、访问、真人会话）只经朝夕客户端 API
  暴露，且只在中心节点挂载。
- **单节点约束（v1 重构现状，已知并接受）**：`/v1` 目前仅由 aliyun1 提供服务；aliyun2 是「厚节点」，只本地处理它归属微信账号的入站 turn，不服务 `/v1`。App 无需关心节点归属，永远只打中心域名。多节点接入 App 是后续「小重构」的事，当前不影响 App 开发。
- 微信渠道与 App 渠道是两条独立入站路径；App 的会话走 `/v1`，与微信 turn 互不干扰。

---

## 2. 接入基础

| 项 | 值 |
|---|---|
| 公网 Base URL（新客户端推荐） | `https://ai4company.top/api/v1/products/zhaoxi/` |
| 兼容 Base URL | `https://ai4company.top/api/v1/` |
| 交互式 API 文档（Swagger） | **未对外暴露**（2026-07-26 实测 `/api/docs` 返回官网 SPA）；契约以仓库提交的 [`openapi/app_v1.json`](openapi/app_v1.json) 为准（见 §2.3），或本地起服务访问 `/docs` |
| 鉴权方式 | 手机号 OTP 登录 → 30 天 session token（`Authorization: Bearer <access_token>`） |
| 内容长度上限 | 文本 4000 字符；语音转写音频 10 MB / 60s；v1.5 媒体：图片 8 MB（JPEG/PNG，单条动态 ≤4 张）、聊天语音 500 KB / 60s（全部见 `/app/config` `limits`，不要硬编码） |
| 时区 | 服务端统一 Asia/Shanghai（+08:00），所有时间字段带 `+08:00` 偏移 |

### 2.1 nginx 前缀说明

服务端将同一组朝夕路由挂到规范产品命名空间与 legacy `/v1`。新客户端一律使用规范前缀，
例如 `POST https://ai4company.top/api/v1/products/zhaoxi/auth/otp/send`；存量客户端可继续使用
`POST https://ai4company.top/api/v1/auth/otp/send`。Base URL 必须集中配置，不能在业务代码中散落硬编码。

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

仓库提交了客户端契约的 OpenAPI 快照：[`openapi/app_v1.json`](openapi/app_v1.json)（58 条 `/v1` 路径 / 62 个操作，含 v1.5 媒体与许愿）。

- 服务端 CI 断言「实时导出 == 提交的 snapshot」，所以**改响应字段必须同步更新 snapshot**，否则后端 CI 直接红。客户端可以拿它做 breaking-change 检查或生成 DTO。
- 后端重新导出：`.venv/bin/python scripts/export_openapi.py`（`--check` 只校验）。
- **当前有主链路 24 个操作有真实响应 schema**：`/app/config`、`/me`、`worlds/home/bootstrap`、`resident-candidates`、`residents`、`residents/confirm`、`conversations`、`ai-conversations/{id}/messages|turn|read`，「我的」Tab 的 `me/profile-options`、`me/profile`、`me/account/deletion`、`notifications/preferences`(GET/PATCH)，世界 Feed 的 `worlds/home/feed`(GET)、`worlds/home/feed/posts`(POST)、`worlds/home/feed/posts/{id}`(DELETE)、`worlds/home/feed/posts/{id}/hide`(POST)，真人会话的 `human-conversations`(GET)、`human-conversations/report-options`(GET)，以及 v1.5 新增的 `media/uploads`(POST)、`media/{media_id}`(GET)、`worlds/home/resident-drafts/preview`(POST)。其余端点只冻结了路径与请求体，响应形状以本文档为准——这是分步交付的既定范围，不是遗漏。
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

线上实测返回（2026-07-31 只读核验；以下仅截取能力位与限额结构，客户端仍必须逐次按真实响应渲染）：

```json
{
  "captcha": { "provider": "aliyun", "scene_id": "6ez3x2ne", "prefix": "18if8u", "configured": true },
  "features": {
    "voice_input": true,
    "resident_world": true,
    "world_feed": true,
    "app_notifications": true,
    "resident_lifecycle": true,
    "mailbox": true,
    "world_visits": true,
    "human_chat_send": true,
    "chat_image_message": true,
    "chat_voice_message": true,
    "feed_image_post": true,
    "resident_wish_create": true
  },
  "limits": {
    "message_chars": 4000, "audio_bytes": 10485760, "audio_duration_ms": 60000,
    "image_bytes_max": 8388608, "image_count_max": 4,
    "voice_bytes_max": 512000, "voice_duration_ms_max": 60000,
    "wish_text_chars": 500, "wish_daily_max": 10
  },
  "client_contract_version": "2026-07-30",
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
  - `voice_input`（CHAT-04）：M1 产品口径是**打开**，但该位由生产 ASR 配置驱动，上面这份
    实测响应是配置生效前抓的。客户端按位渲染即可，不要硬编码——服务端配好 key 后它会翻成
    `true`，无需客户端发版。
  - v1.5 四位（`chat_image_message` / `chat_voice_message` / `feed_image_post` /
    `resident_wish_create`）：**分别独立门控**，可以只开图不开语音。为 `false` 时对应入口
    必须隐藏或置灰；硬发会拿到 `media_disabled`(404) 或 `feature_disabled`(404)。
    它们同样受 `resident_world` 总闸约束（总闸关时恒为 `false`）。
- `limits.*`：**恒下发，与能力位无关**——客户端拿它做上传前本地校验，不要硬编码常量。
  `image_count_max` 是单条动态的图片张数上限（聊天图片恒为单张）。
  `wish_daily_max` ≤ 0 表示服务端不设日额度。
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
  "platform_user": {
    "id": "...",
    "phone_masked": "138****0000",
    "display_name": "小满",
    "avatar_key": "user_03",
    "avatar_ref": "https://.../companion_world/avatars/user/user_03.png"
  },
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
- `platform_user.display_name` / `avatar_key` / `avatar_ref` 未设置时为 `null`，客户端自行
  兜底展示（见 §3.6）。

### 3.6 「我的」Tab：Profile / 注销 / 通知偏好

三组端点都是**真人级**设置（锚在 platform_user），与居民/account 无关。

**Profile（A 类扁平）**

- `GET /v1/me/profile-options` → `{"status":"ok","avatars":[{"key":"user_01","avatar_ref":"…"}],
  "limits":{"nickname_chars":20,"nickname_min_chars":1}}`。头像库由服务端下发，**客户端不要
  硬编码枚举**；数组为空时隐藏头像选择器。
- `PATCH /v1/me/profile`，body `{"display_name"?, "avatar_key"?}`。两个字段都可单独提交，
  省略/`null` 表示「本次不改」（不是清空）；两个都不传 → `422 profile_update_empty`。
  成功返回 `{"status":"ok","platform_user":{…}}`，形状与 `/me` 的 `platform_user` 一致。
- 昵称错误码：`nickname_length_invalid`、`nickname_charset_invalid`、`content_rejected`(422)、
  `content_review_unavailable`(503，可重试)。头像 key 不在表内 → `avatar_key_invalid`(422)，
  **不会回落默认头像**。
- 昵称会展示给来访的真实好友，因此和自建角色名走同一条内容审查链路；改写后的结果即最终值。

**账号注销（A 类扁平）**

- 只有一个端点：`POST /v1/me/account/deletion`，body `{"confirm": true, "reason_code"?}`。
  **注销立即生效、不可撤销**（Q14 已拍板），没有冷静期，因此也没有查询/撤销接口。
- `confirm` 必传且必须为 `true`，否则 `422 deletion_not_confirmed`；`reason_code` 是受控
  取值（`not_useful` / `privacy_concern` / `too_expensive` / `switching` / `other`），不在表
  内 → `422 reason_code_invalid`。**参数校验一定发生在清除之前**，422 时数据完好无损。
- 成功返回 `{"status":"ok","request":{"request_id","status":"executed","reason_code",
  "executed_at"}}`，`executed_at` 带 `+08:00`。
- 返回后该真人**全部设备的登录态已失效**：客户端必须就地清 token 回登录页，继续用旧
  token 会拿到 `401`。
- 清除范围：聊天原文与会话、账号级记忆（L1/L2、dreaming）、世界级共享记忆（L3）、
  账号 profile 文件、提醒/承诺、站内通知；居民全部遣散、世界回到引导前状态。
  **手机号不被占用**——用同一手机号重新登录即得到一个全新的空世界。
- 刻意保留：与第三方的真人会话/来访/邀请记录（删我方副本等于删对方的聊天记录）与
  财务审计流水。二次确认弹窗由客户端负责，服务端只认 `confirm`。

**通知偏好（B 类信封）**

- `GET /v1/notifications/preferences` → `data: {"quiet_level":"standard","available_levels":["standard","quiet"]}`。
  从未设置过时返回默认值，读不写库。
- `PATCH /v1/notifications/preferences`，body `{"quiet_level":"quiet"}`，幂等。取值不在
  `available_levels` 内 → `422 quiet_level_invalid`。
- `quiet` 只压制**将来**的 AI 主动通知；**已在箱内的通知不回收**，`unread_count` 不会因为
  改偏好而变化。切回 `standard` 后立即恢复投递。
- 与 `/v1/notifications` 一样需要 `COMPANION_WORLD_APP_INBOX_ENABLED`，否则 `404 feature_disabled`。
- **作用域仅限 AI 主动通知，不覆盖真人会话**（M5-NOTIFY-001）。服务端目前既没有真人消息
  Push 通道，也没有会话级静音字段/端点，因此 `quiet_level` 不能被解释成「真人会话免打扰」。
  产品已选择[方案 1：明确后置](../../backlog/products/zhaoxi/companion_world_app_followups.md)：
  在投递面、默认策略、红点和免打扰范围冻结前，不提供只在单设备生效的本地静音开关，也不增加
  没有服务端行为的静音 DTO。


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
- `long_summary` 为角色预览页的长介绍，运营未录入时为 `null`；是否增加
  `sample_dialogue` 仍是[产品后续项](../../backlog/products/zhaoxi/companion_world_app_followups.md)，客户端当前不得依赖该字段。

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
{ "client_message_id": "<客户端幂等键>", "text": "你好", "media_ref": null }
```

- **`media_ref`（v1.5 新增，可选）**：先走 `POST /v1/media/uploads` 拿到的 `media_id`，发图片或语音时带上，
  `text` 此时是 caption（可空）。契约与红线见 §6.5。
- `/messages` 的每条消息除老字段外还有 **`content` 判别联合**（`text` / `image` / `audio`），新客户端只读 `content`，见 §6.5.3。

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
GET    /v1/worlds/home/feed?cursor=<游标>&limit=20        → 分页拉取 Feed（limit 1-50，默认 20）
POST   /v1/worlds/home/feed/posts                         → 用户发帖
DELETE /v1/worlds/home/feed/posts/{post_id}               → 主人删除自己的动态
POST   /v1/worlds/home/feed/posts/{post_id}/hide          → 主人隐藏 AI 居民动态
```

`data`：`{ "items": [ /* Feed 项 */ ], "next_cursor": "<游标或 null>" }`。用 `next_cursor` 向后翻页，为 `null` 表示到底。

Feed 项结构含 `post_id / author{type,resident_id,name,avatar_ref} / content / post_type / source / published_at`。这四个操作的响应形状均已进 OpenAPI snapshot，可直接生成 DTO。

`content` 是按 `type` 判别的联合，**v1.5 起有两支**：

```json
{ "type": "text",  "text": "……" }
{ "type": "image", "text": "正文（可空）", "images": [ { "media_id": "med_…", "url": "https://…", "width": 1080, "height": 1440 } ] }
```

正文属于整条动态而不属于某张图；`images` 是有序列表（主人发布时的排版顺序，≤4 张）。
发图文动态见 §6.5.4，`url` 的语义与占位规则见 §6.5.2。

**Feed 生成时间窗（Asia/Shanghai）**：早间 `07:00–11:00`、晚间 `18:00–23:00`。居民自动发帖由中心调度器在窗口内产生；窗口外一般无新 AI Feed。

#### 6.2.1 主人管理自己世界的动态（2026-07-28 新增）

两个操作都只作用于**当前 Session 主人自己的 home world**，请求不接受 `world_id`、`owner_id`、
`account_id`、下架原因等任何字段（带了会被 400/422 拒绝）。响应共用一个信封 `data`：

```json
{ "post_id": "post_...", "status": "deleted", "replayed": false }
```

- **`status`**：删除自己的动态是 `"deleted"`，隐藏 AI 动态是 `"hidden"`。
- **`replayed`**：`true` 表示本次是重放，未产生新的状态变更。**重复请求恒为 200，不会 5xx**，
  客户端可以安全重试。
- 两条路由都返回 `Cache-Control: no-store`，都受 `world_feed` capability 门控（关闭时 404
  `feature_disabled`）。

**按作者类型分流，不能互换**：

| 操作 | 只能作用于 | 用错对象时 |
|---|---|---|
| `DELETE .../posts/{id}` | `author.type == "human"` 且作者是自己 | 404 `post_not_found` |
| `POST .../posts/{id}/hide` | `author.type == "resident"` | 404 `post_not_found` |

客户端据此渲染入口：**只在自己的文字动态上显示「删除」，只在 AI 居民动态上显示「隐藏」**。

其余约定：

- **不存在、跨 owner、不可见、post_id 格式非法统一返回 404 `post_not_found`**，不区分「没有」
  和「不是你的」，避免资源枚举。
- **隐藏是服务端权威状态，不是单设备偏好**：主人和所有有效访客（`GET /v1/visits/{id}/feed`）
  后续都读不到该条。删除同理。操作成功后请让 home Feed 失效并以服务端回读结果为准。
- **游标不受影响**：Feed 用 keyset 游标，删/隐一条不会导致翻页重复或错页，旧 `next_cursor`
  可以继续用。
- **离别动态（`post_type == "farewell"`）不允许隐藏**，返回 409 `post_not_hideable`。它同时是
  世界状态的信号，隐藏会影响整个 Feed 的可读性。客户端不要在离别动态上显示「隐藏」。
- **隐藏不影响居民**：不修改居民生命周期、会话、记忆或离开状态。
- **M2 不提供「取消隐藏」**，客户端不要做本地撤销。

### 6.3 其他世界能力（均已激活，端点见 §8）

- **信箱** `/mailbox/*`：角色来信，接受/婉拒/延后/已读、未读数。
- **访问 / 邀请** `/visits/*`、`/world/invites`：世界互访、生成/核销邀请码。
- **通知** `/notifications`：未读数、标记已读。
- **真人会话** `/human-conversations/*`：真人对真人聊天、举报/拉黑/隐藏，见 §6.4。

### 6.4 真人会话列表与举报契约（2026-07-28 新增）

**`GET /v1/human-conversations`** 的每个 item 补齐了以下字段，客户端不必再自行 join
`/visits` 或伪造未读 badge：

| 字段 | 说明 |
|---|---|
| `last_preview` | 最近一条消息正文，服务端已截断到 120 字并把换行/连续空白折叠成单空格；无消息时为 `null` |
| `unread_count` | **精确值不封顶**，「99+」由客户端展示层决定。只数对方发的消息，自己发的永远不计 |
| `can_send` | 能否发送。为 `false` 时看 `read_only_reason` |
| `read_only_reason` | `can_send=true` 时为 `null`；否则见下表。**按码分支，不要按文案** |
| `expires_at` | 对应 visit 的绝对结束时间（`+08:00`），非 active visit 时可能为 `null` |

`read_only_reason` 取值：

| 值 | 含义 | 是否可恢复 |
|---|---|---|
| `counterpart_blocked` | 任一方已拉黑对方 | 否 |
| `visit_ended` | visit 已到期/离开/撤销/拒绝/取消，或已不存在 | 否 |
| `conversation_ended` | visit 仍在但会话已转只读 | 否 |
| `feature_disabled` | `human_chat_send` 关闭 | **是**，开关打开即恢复 |

判定顺序是「先终态、后开关」：一个已经结束的会话即使赶上开关关闭，也报 `visit_ended`
而不是 `feature_disabled`，避免客户端先显示「功能未开放」、开关打开后又跳成「已结束」。

**未读计算改用消息序号而非时间戳。** 此前 read marker 与消息 `created_at` 都是秒精度，
与标记已读同一秒到达的消息会被判成已读并**永久漏计**。现在 `POST /read` 在同一个写事务内
快照当前最大消息序号，游标只前进不回退。`POST /read` **仍然不接受任何请求体**，客户端无需
改动；`last_read_at` 保留但只用于展示。

**`GET /v1/human-conversations/report-options`**（新增）返回版本化举报原因表：

```json
{"code":"ok","data":{"version":1,"options":[
  {"reason_code":"spam","label":"垃圾广告","details_required":false},
  {"reason_code":"other","label":"其他","details_required":true}
]}}
```

- `options` 顺序即展示顺序，兜底的 `other` 永远在最后。客户端**直接用 `label`**，不要自行翻译或发明分类。
- `version` 只在码集合或语义变化时递增，可据此缓存；纯文案微调不动它。
- `details_required=true` 的码，`POST /report` 不带 `details`（或只给空白）会返回 **422 `invalid_request`** —— 契约与服务端校验是同一份表，不存在「说必填却不校验」。
- 该端点随**读**门控开放：`human_chat_send=false` 时仍可拉取并举报，只有发送被关闭。

### 6.5 会话与动态媒体（v1.5 新增）

图片与语音**一律两步**：先上传拿 `media_id`，再把它挂到消息或动态上。没有"一次请求带文件发消息"的接口——
上传是三条写入路径（AI 会话、真人会话、图文动态）共用的地基。

```
POST /v1/media/uploads                 → 上传，拿 media_id + 一条短 TTL 读 URL
GET  https://ai4company.top/api/v1/media/{media_id}?scope=&exp=&sig=
                                             → 取字节流（不带 Authorization，签名即凭据）
```

#### 6.5.1 上传

`multipart/form-data`：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | 二进制内容 |
| `kind` | 是 | `image` \| `voice` |
| `duration_ms` | 语音必传 | 客户端声明的时长（与 `/audio/transcriptions` 同口径），服务端只做上限校验 |

`data`：

```json
{
  "media_id": "med_…", "kind": "image", "mime": "image/jpeg",
  "bytes": 204800, "width": 1080, "height": 1440, "duration_ms": null,
  "transcript": null,
  "url": "https://ai4company.top/api/v1/media/med_…?scope=pu:…&exp=…&sig=…",
  "url_expires_at": "2026-07-30T14:20:00+08:00",
  "expires_at": "2026-07-30T16:05:00+08:00"
}
```

- **格式白名单**：图片只收 **JPEG / PNG**，HEIC / GIF / WEBP 一律 `media_kind_unsupported`(415)。
  iOS 拍照默认 HEIC，**客户端必须先本地转码**。语音收 aac / m4a / mp3 / wav / ogg / webm。
- **服务端不信任客户端声明**：图片一律重编码并剥离 EXIF/GPS，语音按魔数复核容器。
  因此**宽高、`mime`、`bytes` 一律以响应为准**，不要用本地读到的值。
- 语音原文件不会为 ASR 改格式：M4A/AAC 仍按原字节存储和播放。生产使用豆包大模型录音文件
  识别极速版时，服务端仅生成一次临时 16kHz 单声道 WAV；成功则 `transcript` 为文本，供应商、
  鉴权、转码或超时失败则为 `null`，但上传仍返回成功。
- **`url` 与 `expires_at` 是两件事**：`url_expires_at` 是这条签名地址什么时候失效（默认 15 分钟），
  `expires_at` 是这份**还没被引用**的媒体什么时候被回收（默认 2 小时）。
- **两小时内必须用掉**：超时后引用会拿到 `media_ref_expired`(409)，需要重新上传。
  草稿箱久放、退后台再回来发图，都要考虑重传。
- `transcript` 只在 `kind=voice` 且同步转写成功时非空；**转写失败不阻塞上传与发送**，为 `null` 就当没有。
- 上传限流 30 次/分钟（超出 `rate_limited` 429）。
- 门控：图片上传只要 `chat_image_message` 或 `feed_image_post` 任一为 `true` 就可用；语音上传看 `chat_voice_message`。
  都关时返回 `media_disabled`(404)。

#### 6.5.2 读 URL 的规则（最容易踩的一节）

- 所有返回媒体的接口（上传、消息列表、Feed、访客 Feed）都**现签**一条短 TTL URL。
- **`url` 是客户端可直接加载的完整 HTTPS URL**，生产 canonical path 为
  `/api/v1/media/{media_id}`；客户端不要再拼 origin、API base 或产品 namespace。
- 旧版已经下发的 `/v1/media/{media_id}` 相对地址仍可访问，但只作兼容，不再作为新响应契约。
- **不要持久化、不要跨会话复用、不要写进本地库**。过期就重新拉一次列表/详情拿新签名。
- `GET /api/v1/media/{media_id}` **不读 `Authorization`**，签名三元组（`scope` / `exp` / `sig`）就是唯一凭据；
  原样使用返回的 URL，不要自己拼参数。
- **`url` 为 `null` 表示服务端此刻签不出**（部署缺 secret，或访客的拜访已结束）：
  **按占位图渲染，不要降级成文本、不要当成消息损坏**。
- 访客视角（`GET /v1/visits/{visit_id}/feed`）拿到的是按 visit scope 签的 URL，**拜访一结束立即失效**。
- 任何读取失败（签名参数缺失/格式错误、签名不符、过期、scope 失效、资源缺失）统一收敛成
  `media_access_denied`(403)，不区分原因（防枚举）。

#### 6.5.3 会话里的媒体消息

发送（AI 会话与真人会话同形，只差路径）：

```
POST /v1/ai-conversations/{conversation_id}/turn
POST /v1/human-conversations/{conversation_id}/messages
{ "client_message_id": "<幂等键>", "text": "（可空的 caption）", "media_ref": "med_…" }
```

- 单条消息**最多一份媒体**；`text` 与 `media_ref` 不能同时为空（都空是 `media_content_required` 422）。
- 幂等键语义不变：重放返回与首次相同的消息，不要新建气泡。

读取时每条消息新增 `content` 判别联合（`GET .../messages`，AI 与真人会话同一套形状）：

```json
{ "type": "text",  "text": "……" }
{ "type": "image", "media_id": "med_…", "url": "https://…", "width": 1080, "height": 1440, "text": "caption" }
{ "type": "audio", "media_id": "med_…", "url": "https://…", "duration_ms": 4200, "transcript": "转写文本或 null", "text": "" }
```

- **老字段 `message_type` / `text` 已 deprecated**：服务端保证 `message_type == content.type`
  （历史库内的 `voice` 统一投影成 `audio`）、`text == content.text`。**新客户端只读 `content`。**
- v1.5 之前的存量消息全部投影成 `text` 支，不需要客户端做版本分支。
- `content.text` 只包含**用户自己写的 caption**，绝不含服务端生成的图片描述。
- `audio.transcript` 供"长按转文字"，为 `null` 时隐藏该入口即可。
- **AI 会话里的图片会真的被居民"看到"、语音会被转写后进上下文**，所以回复会针对内容本身，
  不是"收到一张图"的套话。

#### 6.5.4 图文动态

```
POST /v1/worlds/home/feed/posts
{ "client_request_id": "<幂等键>", "text": "正文（可空）", "media_refs": ["med_a", "med_b"] }
```

- `media_refs` 有序、**最多 4 张**（以 `limits.image_count_max` 为准），只支持图片。
- `text` 与 `media_refs` 不能同时为空；**超 4 张或图文全空在参数层就是 422 `invalid_request`**，
  不会返回 `media_count_exceeded` / `media_content_required`（那两个码只在服务端领域层做防御）。
- 动态发布路径上需要分支的码只有：`media_disabled`(404) / `media_ref_invalid`(409) /
  `media_ref_expired`(409) / `invalid_request`(422) / `idempotency_conflict`(409)。
- 读回见 §6.2 的 `content.image` 支。

#### 6.5.5 审核口径：先发后审、仅红线

- 图片发出去**立即可见**，机审在后台补做，客户端**不需要**做"审核中"状态。
- 命中红线时：**图文动态会被终态下架**（主人与所有访客的 Feed 里都读不到，与主人自己删除同效果）；
  **会话图片只落审核结论、当前不撤回消息**（撤回能力排在 v1.6）。
- 机审能力未配置时图片一律放过，不影响任何客户端可见行为。

#### 6.5.6 与注销的关系

`POST /v1/me/account/deletion` 现在会连带删除该用户的媒体库行与磁盘文件、以及自己世界的全部动态。
客户端若缓存过图片或动态，**注销成功后要一并清本地缓存**，否则会残留已删内容。

### 6.6 许愿创建居民（v1.5 新增）

自建角色的**第三条入口**：用户用一句自然语言许愿，服务端把它翻译成受控取值，再走既有的
「预览 → 确认」两步。能力位 `resident_wish_create`，关闭时 `feature_disabled`(404)。

```
POST /v1/worlds/home/resident-drafts/preview   { "wish_text": "…", "client_request_id": "…" }
POST /v1/worlds/home/residents                 { "draft_token": "…", "client_request_id": "…" }
```

- **与表单路径互斥**：带了 `wish_text` 就不能再带 `name` / `relationship_type` / `personality_traits`
  等结构化字段（否则 422）；许愿路径的 `client_request_id` **必填**，表单路径**不接受**该字段。
- **出参与表单路径逐字段同形**（`ResidentDraftPreviewData`：`draft_id / draft_token / expires_at /
  name / relationship_display / tags / normalized_summary / ai_identity_notice / avatar_ref`），
  所以**预览卡片 UI 零改动**，只需多一个"写愿望"的输入入口。
- **所见即所存**：预览里的字段就是最终落进居民人设的值；自由文本原文不落库、不直通人设。
- `wish_text` ≤ `limits.wish_text_chars`（500 字）；日额度 `limits.wish_daily_max`（默认 10，≤0 表示不限）。
- **幂等**：同一 `client_request_id` 重放预览返回逐字段等值的同一份草稿，不会重复消耗额度、也不会重复调模型。
- 第二步确认与既有自建路径完全一致：`draft_token` 一次性、30 分钟过期
  （`resident_draft_expired` / `resident_draft_consumed` 见 §7）。
- 专有错误码：`wish_text_rejected`(422，愿望命中安全护栏，提示换一种写法)、
  `wish_rate_limited`(429，超日额度)、`wish_generation_failed`(503，模型不可用，**可重试**)。
  额度与幂等都在调模型**之前**判，重试和超额不会白花一次生成。

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
| `post_not_found` | 404 | 动态不存在、不属于你、作者类型不符或 id 非法（统一码，防枚举） |
| `post_not_hideable` | 409 | 该动态不允许隐藏（当前只有离别动态 `post_type='farewell'`） |
| `letter_not_found` / `letter_not_open` / `letter_expired` | 404 / 409 / 409 | 信箱来信状态 |
| `invalid_invite_code` / `invite_expired` / `visit_*` | 400 / 409 / … | 邀请与访问相关 |
| `idempotency_conflict` | 409 | 同一幂等键配不同内容（客户端复用了幂等键） |
| `media_disabled` | 404 | 对应媒体能力位关闭（先看 `/app/config` `features`） |
| `media_ref_invalid` | 409 | `media_ref` 不存在、不属于你，或已被别的消息引用 |
| `media_ref_expired` | 409 | 未被引用的媒体超 2 小时已回收，需重新上传 |
| `media_kind_unsupported` | 415 | `kind` 非法，或格式不在白名单（HEIC/GIF/WEBP） |
| `media_decode_failed` | 422 | 文件打不开（含伪装扩展名） |
| `media_too_large` | 413 | 超 `image_bytes_max` / `voice_bytes_max` |
| `media_duration_exceeded` | 413 | 超 `voice_duration_ms_max` |
| `media_content_required` | 422 | 消息的 `text` 与 `media_ref` 都为空；或上传了空文件 |
| `media_count_exceeded` | 422 | 媒体张数超限（动态路径由参数层拦成 `invalid_request`，客户端一般见不到） |
| `media_access_denied` | 403 | 读媒体的签名无效/过期，或 scope 已失效（拜访结束）。统一码，不区分原因 |
| `media_signing_unavailable` | 503 | 服务端签名密钥未配置（部署问题），**可重试** |
| `wish_text_rejected` | 422 | 愿望文本命中安全护栏，提示换一种写法 |
| `wish_rate_limited` | 429 | 许愿超日额度（`limits.wish_daily_max`） |
| `wish_generation_failed` | 503 | 愿望翻译所用模型暂不可用，**可重试**，不消耗额度 |

> 完整表见 `app/products/zhaoxi/api/companion_world.py` 的 `_ERROR_STATUS`。App 应基于 `code`（而非文案）做分支，未知 `code` 按对应 HTTP 状态兜底。

主链路 21 个操作的 401/403/404/409/422/429 已在 OpenAPI snapshot 里声明为错误信封
（`WorldErrorEnvelope`），可直接据此生成错误分支；具体 `code` 取值仍以上表为准。

---

## 8. `/v1` 全量端点清单（62 条）

**鉴权 / 账户**
- `GET /v1/app/config`
- `POST /v1/auth/otp/send`、`POST /v1/auth/otp/verify`
- `POST /v1/auth/session`、`DELETE /v1/auth/session/current`
- `GET /v1/me`
- `GET /v1/me/profile-options`、`PATCH /v1/me/profile`
- `POST /v1/me/account/deletion`（立即注销，不可撤销）

**主聊天（默认账号，非世界）**
- `GET /v1/chat/messages`、`POST /v1/chat/turn`
- `POST /v1/audio/transcriptions`（语音转写；能力位由所选 ASR provider 的完整凭据决定）

> 2026-07-26 起这两个 legacy 端点与世界端对齐：`/chat/messages` 的 `created_at` 显式带
> `+08:00`（此前是无时区的裸时间串，客户端如按本地时区解析过需回归一次）；`/chat/turn` 的
> `metadata` 增 `message_id`，且**重放返回与首次相同的 `message_id`**。请求体与其余字段不变。

**世界 / 家园**
- `POST /v1/worlds/home/bootstrap`
- `GET /v1/worlds/home/resident-candidates`、`GET /v1/worlds/home/resident-options`
- `POST /v1/worlds/home/resident-drafts/preview`
- `GET /v1/worlds/home/residents`、`POST /v1/worlds/home/residents`、`POST /v1/worlds/home/residents/confirm`
- `GET /v1/worlds/home/feed`、`POST /v1/worlds/home/feed/posts`
- `DELETE /v1/worlds/home/feed/posts/{id}`、`POST /v1/worlds/home/feed/posts/{id}/hide`

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
- `GET /v1/notifications/preferences`、`PATCH /v1/notifications/preferences`

**媒体（v1.5）**
- `POST /v1/media/uploads`（图片 / 语音上传，multipart）
- `GET /api/v1/media/{media_id}?scope=&exp=&sig=`（公网签名读，不带 `Authorization`）

**真人会话**
- `GET /v1/human-conversations`
- `GET /v1/human-conversations/report-options`
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

### 9.1 v1.5 能力的联调前置（媒体 + 许愿）

服务端代码已上线，四个 flag **默认打开**（2026-07-30 起改为「代码默认开、`.env` 显式写
`false` 才关」）。真正决定媒体三位可见性的是**签名密钥是否配置**：

| flag（默认 true） | 能力位 | 还需要什么 |
|---|---|---|
| `COMPANION_WORLD_CHAT_IMAGE_ENABLED` | `chat_image_message` | `MEDIA_URL_SIGNING_SECRET` |
| `COMPANION_WORLD_CHAT_VOICE_ENABLED` | `chat_voice_message` | `MEDIA_URL_SIGNING_SECRET` |
| `COMPANION_WORLD_FEED_IMAGE_ENABLED` | `feed_image_post` | `MEDIA_URL_SIGNING_SECRET` |
| `COMPANION_WORLD_RESIDENT_WISH_ENABLED` | `resident_wish_create` | 无（已可用） |

`MEDIA_URL_SIGNING_SECRET` 留空时媒体链路整体视为未就绪：三个媒体能力位一律下发 `false`，
`POST /media/uploads` 返回 `media_disabled`——**不会**出现「能力位是 `true` 却拿不到读 URL」的
半开状态，客户端照能力位渲染即可。配好密钥并重启后三位自动转 `true`，客户端无需发版。

**生产状态（2026-07-31）**：密钥已配置，四位全部为 `true`，媒体上传与读取均可用。

图片机审是独立的运维配置（见
[`ops/platform/image_moderation_setup.md`](../../ops/platform/image_moderation_setup.md)），
**未配置不阻塞任何客户端功能**，只是图片一律放过不送审。

即使生产后续临时回收任一能力位，客户端也必须保证对应值为 `false` 时入口不出现。

---

## 10. 联调建议

1. 先打 `GET /api/v1/products/zhaoxi/app/config` 确认连通与 captcha 参数。
2. 走完整登录链路拿到 `access_token`，之后所有请求带 `Authorization: Bearer <token>`。
3. 新号验证 `account == null` → bootstrap → confirm → 用返回的 `conversation_id` 发 turn。
4. 所有写操作（turn、发帖等）带客户端幂等键，验证断网重试不产生重复。
5. 遇到 4xx，按 §7 的 `code` 做分支，不要依赖文案。
6. 机器可读契约用仓库里的 [`openapi/app_v1.json`](openapi/app_v1.json)（见 §2.3，线上未开放 Swagger）；本文档为落地口径与流程约定。
7. 正式版客户端开发的精简入口见 [`app_client_brief.md`](app_client_brief.md)。
8. 媒体链路分三步验：`POST /media/uploads` → 直接 `GET` 返回的 `url`（**不带 `Authorization`**，
   期望拿到字节流）→ 再把 `media_id` 挂到 turn / 动态上回读。重点回归两件事：URL 过期后重新拉列表
   能拿到新签名，以及 `url=null` 时渲染成占位而不是空白或报错。
