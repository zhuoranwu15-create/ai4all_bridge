# 朝夕相伴 App 正式版客户端 — 后端对接简要说明

> 面向：App 正式版客户端开发者。本文只讲「必须先知道的」；
> 完整端点清单、字段结构、错误码表见 [`app_api_handoff.md`](app_api_handoff.md)。
> 最后对生产 `https://ai4company.top` 实测核对：2026-07-31。

## 1. 接入基础

| 项 | 值 |
|---|---|
| 生产 Base URL（推荐） | `https://ai4company.top/api/v1/products/zhaoxi/` |
| 生产 Base URL（旧，仍可用） | `https://ai4company.top/api/v1/` |
| 鉴权 | 手机号 OTP → 30 天 session token，`Authorization: Bearer <access_token>` |
| 时区 | 服务端统一 Asia/Shanghai，时间字段带 `+08:00` |
| 文本上限 | 4000 字符（以 `/app/config` 的 `limits` 为准） |

两个前缀挂的是**同一套路由、同一套行为**。`/v1` 是微信时代遗留的兼容路径，
`/api/v1/products/zhaoxi` 是多产品重构后的规范命名空间（2026-07-25 上线）。
正式版建议只用规范前缀，并且 **base URL 必须做成配置项**，不要散落在各处硬编码。

> Swagger/OpenAPI **未对外暴露**（`/api/docs` 返回的是官网 SPA）。机器可读契约用后端仓库
> 提交的 [`openapi/app_v1.json`](openapi/app_v1.json)：服务端 CI 断言它与实现一致，可以拿来
> 生成 DTO 或做 breaking-change 检查。响应 schema 覆盖会随契约批次扩展，不在本简版手工维护
> 数量或端点清单；当前覆盖边界以 [`app_api_handoff.md` §2.3](app_api_handoff.md#23-openapi-契约-snapshot)
> 和 CI 契约测试为准。

## 2. 登录链路

```
GET    {base}/app/config              # 无需鉴权：captcha 参数 + 功能开关 + 限额 + 最低版本
POST   {base}/auth/otp/send           # {"phone","captcha_verify_param"} 先过阿里云行为验证码
POST   {base}/auth/otp/verify         # {"phone","code"} → {"verified_token"} 一次性
POST   {base}/auth/session            # {"phone","verified_token","invite_code?","campaign_code?"}
                                      # → access_token(30天) / expires_at / is_new_user / account
DELETE {base}/auth/session/current    # 登出，吊销当前 token
GET    {base}/me                      # 复活会话时校验 token 并拿账号信息
```

- `app/config.minimum_supported_version` 低于该值应做强制升级提示。
- `app/config.features.voice_input`：M1 口径是**打开**，但生效时点取决于服务端 ASR 配置。
  UI 一律按开关渲染、不要写死——服务端配好后它自己翻 `true`，客户端无需发版。
- 认证类接口响应带 `Cache-Control: no-store`，客户端不要缓存。

## 3. 关键分叉：`account == null`

生产已开 `COMPANION_WORLD_P1_ENABLED`，所以登录后有两种用户：

- `account != null`（老用户，多为微信侧已有账号）→ 直接进主聊天，走
  `GET {base}/chat/messages` / `POST {base}/chat/turn`。
- `account == null`（新用户）→ **这是刻意设计，不是错误**。新用户不预建默认账号，
  必须先走世界引导：
  `POST {base}/worlds/home/bootstrap` → 选居民 →
  `POST {base}/worlds/home/residents/confirm` → 拿 `conversation_id` →
  `POST {base}/ai-conversations/{conversation_id}/turn` 聊天。

正式版两条路径都要实现。判定只看 `account` 是否为 null，不要用 `is_new_user` 代替。

## 4. 两套响应外壳（必须分别处理）

**A 类** — `/app/config`、`/auth/*`、`/me`、`/chat/*`、`/audio/*`：扁平结构，
成功 `{"status":"ok", ...}`，失败 `{"detail":"..."}` + HTTP 状态码。

**B 类** — 世界相关（`/worlds/*`、`/conversations`、`/ai-conversations/*`、`/mailbox/*`、
`/visits/*`、`/world/invites`、`/notifications`、`/human-conversations/*`）：统一信封

```json
{"code":"ok","request_id":"req_...","server_time":"2026-07-26T10:04:32+08:00","data":{...}}
```

失败时 `code` 是错误码、无 `data`。**分支一律基于 `code`，不要匹配文案**；未知 `code`
按 HTTP 状态兜底。`request_id` 建议随客户端日志一起记录，排查时直接给后端。

## 5. 发消息的四条硬约定

1. **幂等键必传**：`client_message_id` 由客户端生成、会话内唯一，
   格式 `^[A-Za-z0-9_-]{8,64}$`（建议 UUID 去掉短横线）。网络重试用同一个 key，
   服务端会返回同一条回复并标 `deduplicated=true`，不会重复扣量或重复回复。
   重放返回的 `reply.message_id` 与首次**完全一致**，据此认出是同一条消息，不要新建气泡。
2. **409 `turn_in_progress`**：同一账号/会话上一轮未完成时并发发送会被拒。
   UI 应在等待回复期间禁用发送，收到 409 按「上一条还在处理」提示，不要自动重试。
3. **429 `rate_limited`**：服务端账号级滑动窗口限流（当前生产 10 次 / 30 秒）。
   收到后按退避重试，并把服务端返回的提示文案展示给用户。
4. **`no_reply=true` 是正常业务态**：这一轮 AI 选择不回复，不是错误，不要报错弹窗。
   此时 `reply` **整体为 `null`**；只要 `reply` 非 null，`reply.text` 就一定非空，
   不存在「有对象但 text 为 null」的中间态。

其他常见状态：`403 account_disabled`（账号停用，应登出并提示）、
`401`（token 失效，走重新登录）。

会话列表（`GET {base}/conversations`）按服务端返回顺序展示即可：`sort_time` 是排序锚，
`last_message_at` 是最近一条消息时间（没聊过为 `null`，不要拿 `sort_time` 顶替显示）。
`can_send=false` 时禁用输入框、按 `read_only_reason` 出文案。未读用 `unread`，读完调
`POST {base}/ai-conversations/{id}/read` 上报 `last_message_id`（幂等，只前进）。

## 6. 其余能力只保留接入红线

信箱、访问/邀请、站内通知、真人会话、媒体、图文 Feed 与「我的」Tab 已在生产开启；异步许愿
代码默认关闭，完成 m0058/worker 联调后再开启，
但客户端仍必须按 `/app/config` 的能力位渲染。端点、字段、状态机和错误码只在
[`app_api_handoff.md`](app_api_handoff.md) §3、§6–§9 维护，本简版不再复制清单。

接入时额外记住三条红线：

- 图片与语音**一律两步**：`POST {base}/media/uploads` 拿 `media_id` → 再挂到
  `ai-conversations/{id}/turn`、`human-conversations/{id}/messages` 的 `media_ref`
  或 `worlds/home/feed/posts` 的 `media_refs`（≤4 张）。
- 读媒体用返回的**短 TTL 签名 URL**，**不带 `Authorization`、不要持久化**；`url=null` 渲染占位。
  该 URL 是 **origin 相对路径**（`/v1/media/…?scope=&exp=&sig=`）：拼 `https://ai4company.top` 即可，
  **不要拼 base URL**（base 带 `/api/v1/products/zhaoxi` 前缀，拼出来取不到图）。
- 图片只收 JPEG/PNG，iOS HEIC 必须先转码；消息按 `content` 判别联合渲染，老字段只作兼容。
- 账号注销立即生效且不可撤销，客户端必须二次确认、清空本地缓存并回登录页；通知 `quiet` 只影响
  将来的 AI 主动通知，不覆盖真人会话。

## 7. 已知约束

- `/v1` 客户端 API 只由中心节点 aliyun1 提供；aliyun2 只处理微信入站。
  App 永远只打中心域名，无需关心节点归属。
- 功能门控以服务端 flag 为准：某能力返回 404 `not_found` 时，先确认后端开关，
  客户端应能优雅降级而不是崩溃。

## 8. 联调顺序建议

1. `GET {base}/app/config` 验证连通与 captcha 参数。
2. 跑完整登录链路拿 `access_token`。
3. 用**新手机号**验证 `account == null` → bootstrap → confirm → turn 全流程。
4. 用**已有微信账号的手机号**验证 `account != null` → `/chat/turn` 全流程。
5. 断网重发验证幂等；并发发送验证 409；连发验证 429。
6. 「我的」Tab：改昵称/头像 → `/me` 回读一致；开 `quiet` 后确认新通知不再进箱、老通知仍在。
7. **注销放在最后一步做**（会清空该测试号的全部数据）：`confirm:true` → 确认返回后旧
   token 立即 401 → 同一手机号重新登录 → 拿到 `account == null` 的全新空世界。
