# 身份模型与微信绑定

> 本文是 AI4ALL 账号身份、微信通道绑定和 Web onboarding 绑定流程的主参考文档。
> 2026-05-20 之后，账号标识和绑定口径以本文、`docs/current_status.md` 和 `docs/mid_long_term_tech_plan.md` 为准；早期文档里的 `account_id` 需要结合上下文判断。

## 1. 当前结论

当前已落地的核心结论：

- AI4ALL 的业务隔离账号是 `ai4all_account_id`。
- 代码和 DB 里遗留的 `account_id` 字段暂时保留，语义上等同于 `ai4all_account_id`。
- 未绑定的 OpenClaw 入站消息仍 fallback 为 `ai4all_account_id = payload.session_key`，这是兼容路径。
- Web onboarding 绑定完成后，`ai4all_account_id` 是 Backend 预创建的 `acct_...`；入站消息通过 `channel_account_id` 或 `openclaw_login_session_key` 路由回这个预创建账号。
- OpenClaw / provider 侧账号 ID 不再叫业务 `account_id`，统一称为 `channel_account_id`。
- Bridge payload 会显式发送 `channel_account_id`，同时保留 legacy `account_id` 兼容旧接口。
- `binding_intent_id` 是一次性绑定流程 ID，不是用户身份、账号身份或长期通道身份。
- `channel_bindings` 只记录 AI4ALL Account 与通道身份的关系，不代表产品用户、订阅用户或 owner。

最需要避免的误解：

```text
OpenClaw payload account_id
!= AI4ALL 业务账号 ID

微信小程序 code2Session 返回的 session_key
!= OpenClaw ctx.sessionKey
```

## 2. 命名表

| 名称 | 当前含义 | 用途 |
| --- | --- | --- |
| `ai4all_account_id` | AI4ALL 业务隔离账号 ID | Soul、记忆、会话、限流、用量、配置隔离 |
| `account_id` | 代码/DB 历史字段 | 暂时作为 `ai4all_account_id` 的兼容别名 |
| `session_key` | OpenClaw 入站会话键 | 未绑定 fallback、会话排障、绑定路由辅助 |
| `channel_account_id` | OpenClaw / 微信通道侧账号或机器人账号 | 排障、通道绑定、未来账号迁移 |
| `binding_intent_id` | Backend 生成的一次性绑定流程 ID | 串联一次 QR 登录流程，完成后不作为长期身份 |
| `openclaw_login_session_key` | 本次 OpenClaw QR 登录的 session key | 调 OpenClaw wait、绑定后兜底路由 |
| `sender_id` | 通道侧 sender | 当前不作为业务主键 |
| `chat_id` | 通道侧 chat/conversation | 当前不作为业务主键 |
| `channel_binding` | AI4ALL Account 与通道身份的绑定记录 | 运营排查、后续迁移辅助 |
| `platform_user_id` | 未来产品用户 ID | 注册、登录、订阅、套餐、支付、owner 关系 |
| `openid` | 微信某个应用内的用户标识 | 小程序/公众号/网站应用内识别 |
| `unionid` | 微信开放平台同主体下跨应用用户标识 | 连接小程序、公众号、网站应用的推荐外部身份 |

## 3. 当前入站链路

### 3.1 Bridge 发送字段

`openclaw-bridge/index.js` 在 `before_agent_reply` hook 中组装 payload：

```json
{
  "channel": "openclaw-weixin",
  "channel_account_id": "openclaw_or_provider_account",
  "account_id": "openclaw_or_provider_account",
  "sender_id": "...",
  "chat_id": "...",
  "session_key": "agent:main:openclaw-weixin:<channel_account_id>:direct:<peer_id>",
  "message_id": "...",
  "text": "..."
}
```

这里的 `account_id` 只是兼容旧后端。新逻辑读 `channel_account_id`，不要把它当 AI4ALL 业务主键。

### 3.2 Backend 解析身份

入口：

- `/openclaw/turn`
- `/openclaw/debug-traces`

统一通过 `app.identity.resolve_openclaw_identity()` 解析：

```text
openclaw_session_key_account_id = clean(payload.session_key)
fallback = chat_id -> sender_id -> "unknown"
channel_account_id = payload.channel_account_id or payload.account_id
```

随后 `/openclaw/turn` 通过 `resolve_account_id_for_inbound_channel_identity()` 得到真正用于业务隔离的 `ai4all_account_id`：

```text
1. 如果 channel_account_id 命中 completed binding_intent，则使用绑定的预创建 account_id。
2. 如果 channel_account_id 等于某个 completed binding_intent.openclaw_login_session_key，则使用绑定的预创建 account_id。
3. 如果 session_key 等于某个 completed binding_intent.openclaw_login_session_key，则使用绑定的预创建 account_id。
4. 否则 fallback 为 session_key。
```

这意味着绑定前的 legacy 账号仍可工作；绑定后，业务数据会进入用户注册阶段创建的 `acct_...`。

解析结果会写入 response metadata 和 debug trace metadata：

```json
{
  "ai4all_account_id": "...",
  "account_id": "...",
  "session_key": "...",
  "channel": "openclaw-weixin",
  "channel_account_id": "...",
  "sender_id": "...",
  "chat_id": "..."
}
```

当绑定路由把 OpenClaw session key 映射到预创建账号时，metadata 会额外保留：

```json
{
  "openclaw_session_key_account_id": "agent:main:openclaw-weixin:<channel_account_id>:direct:<peer_id>"
}
```

### 3.3 DB 写入路径

`/openclaw/turn` 当前会做：

1. `resolve_openclaw_identity()`
2. `resolve_account_id_for_inbound_channel_identity(...)`
3. `get_or_create_session(account_id=resolved_account_id, session_key=identity.session_key, ...)`
4. `upsert_channel_binding(account_id=resolved_account_id, channel_account_id=identity.channel_account_id, ...)`
5. 按 `account_id` 写 messages、daily_usage、profiles、debug_traces
6. 按 `account_id` 读取 user profile / agent context / memory

注意这里的 DB `account_id` 是历史字段，语义上是 `ai4all_account_id`。

## 4. 当前隔离不变量

后续研发必须遵守：

- 所有 Soul、profile、memory、usage、skills、套餐权益读取都必须经过 AI4ALL 业务账号 ID。
- 不允许直接用 OpenClaw payload 原生 `account_id` 做业务隔离。
- 不允许把不同 `ai4all_account_id` 的记忆、会话、profile 混用。
- `channel_bindings` 是通道层绑定，不是产品用户绑定。
- 新增代码如果需要账号 ID，应优先命名为 `ai4all_account_id`；只有兼容旧 DB/API 时才叫 `account_id`。

## 5. 微信注册用户如何绑定到 AI4ALL Account

### 5.1 关键判断

用户提出的设想里有一个核心假设需要修正：

> 用户先注册服务号或小程序，我们拿到一个 `session_key`，之后用户扫码登录 OpenClaw 后，用相同 `session_key` 绑定。

这个假设不成立。

微信小程序 `code2Session` 返回的 `session_key` 是小程序登录会话密钥，用来校验和解密小程序开放数据。它不是稳定用户 ID，也不是 OpenClaw 的 `ctx.sessionKey`。OpenClaw 的 `session_key` 是我们当前从 OpenClaw 消息上下文拿到的通道会话键，两者没有天然相等关系。

因此，注册和订阅侧应该使用微信官方身份：

- 小程序内：`openid`，如满足条件可拿 `unionid`。
- 公众号网页授权：`openid`，如满足条件可拿 `unionid`。
- 网站应用微信扫码登录：可拿对应应用下的 `openid`，同开放平台下可用 `unionid` 贯通。

推荐用 `unionid` 做跨小程序、公众号、网站应用的外部用户标识；如果暂时没有 `unionid`，就先用具体应用内的 `openid`，但要为后续合并预留。

### 5.2 推荐产品账号模型

建议把“产品用户”和“AI bot 账号”分开：

```text
platform_users
- id
- unionid
- miniapp_openid
- official_account_openid
- website_openid
- phone
- status

subscriptions
- id
- platform_user_id
- plan
- status
- starts_at
- expires_at

ai4all_accounts
- id / ai4all_account_id
- status
- display_name

account_owner_bindings
- platform_user_id
- ai4all_account_id
- binding_method
- status
- verified_at

binding_intents
- id / binding_intent_id
- platform_user_id
- ai4all_account_id
- openclaw_login_session_key
- status
- expires_at
- completed_at
- raw_result_json

channel_bindings
- ai4all_account_id
- channel
- channel_account_id
- session_key
- sender_id
- chat_id
- raw_identity_json
```

当前代码已补齐最小版 `platform_users`、`subscriptions`、`account_owner_bindings`、`binding_intents` 和 `channel_bindings`。这些表先服务 Web onboarding 验证链路，后续做微信登录、支付、套餐权益和解绑迁移时继续扩展字段，不需要推翻当前模型。

## 6. 当前 Web Onboarding 绑定流程

### 6.1 已实现的自动二维码绑定

当前 Web 端入口是 `/ui/onboarding.html`。用户在 Web 页面输入大陆手机号，完成阿里云图形验证码和短信 OTP 后，Backend 才允许创建或复用 `platform_user`。用户随后创建智能体，Backend 自动调用 OpenClaw Gateway 的 QR 登录能力，前端直接展示二维码。用户扫码后，Backend 等待 OpenClaw 返回登录结果并完成绑定，不需要用户手动输入绑定码。

这里最关键的设计点是：

```text
binding_intent_id
= AI4ALL Backend 生成的一次性绑定流程 ID

openclaw_login_session_key
= 本次传给 OpenClaw QR 登录能力的 accountId
= OpenClaw 返回/确认的本次 QR 登录 sessionKey
```

当前实现创建 `binding_intent` 时会先把 `openclaw_login_session_key` 初始化为同一个 `binding_intent_id`，再把它传给 OpenClaw `web.login.start`。如果 OpenClaw 返回了新的 `sessionKey`，Backend 会以返回值更新 `openclaw_login_session_key`。二维码本身不需要直接携带这个 ID；OpenClaw 会在本地/网关登录流程中用 session key 索引这次 QR 登录状态。后端后续用 `openclaw_login_session_key` 调 OpenClaw wait 能力拿到扫码结果。

```text
用户打开小程序 / H5
-> 用户注册 / 登录
-> Backend 创建 platform_user
-> 用户选择套餐 / 创建订阅
-> 用户创建智能体
-> Backend 创建 ai4all_account_id
-> Backend 创建 binding_intent_id，例如 bind_001
-> Backend 调 OpenClaw Gateway web.login.start(accountId=bind_001)
-> OpenClaw 返回 qrDataUrl + sessionKey
-> Backend 保存 openclaw_login_session_key = sessionKey，status=qr_created
-> 前端展示 qrDataUrl 并轮询 binding_intent 状态
-> Backend 后台任务调 OpenClaw Gateway web.login.wait(accountId=openclaw_login_session_key)
-> OpenClaw 返回 connected + accountId
-> Backend 将 accountId 规范为 channel_account_id
-> Backend 更新 binding_intents.status=completed
-> Backend 创建 channel_bindings(ai4all_account_id, channel_account_id, openclaw_login_session_key, ...)
-> 后续入站消息按 channel_account_id 或 openclaw_login_session_key 路由回预创建的 ai4all_account_id
```

小程序 / 公众号版本上线后，前半段会替换成：

```text
用户打开小程序 / H5
-> 微信官方登录
-> Backend 获得 openid / unionid
-> 创建 platform_user
-> 用户选择套餐 / 完成订阅
-> 用户创建智能体
-> Backend 创建 ai4all_account_id
-> Backend 创建 binding_intent_id，例如 bind_001
-> 继续执行同一套 QR 绑定流程
```

伪代码：

```text
binding_intent = create_binding_intent(
  platform_user_id,
  ai4all_account_id,
)

qr = openclaw.loginWithQrStart(
  channel="openclaw-weixin",
  accountId=binding_intent.openclaw_login_session_key,
)

binding_intent.openclaw_login_session_key = qr.sessionKey
binding_intent.status = "qr_created"

result = openclaw.loginWithQrWait(
  channel="openclaw-weixin",
  accountId=binding_intent.openclaw_login_session_key,
)

if result.connected:
  channel_account_id = normalize(result.accountId)
  bind_owner(platform_user_id, ai4all_account_id)
  bind_channel(ai4all_account_id, channel_account_id)
  binding_intent.status = "completed"
```

当前代码入口：

- `POST /web/sms/send-otp`：校验阿里云图形验证码并发送短信 OTP。
- `POST /web/sms/verify-otp`：校验 OTP 并返回一次性 `verified_token`。
- `POST /web/register`：携带 `otp_token` 后创建或复用 `platform_user`。
- `POST /web/agents`：创建预生成的 AI4ALL Account、profile、owner binding 和订阅。
- `POST /web/binding-intents`：创建 `binding_intent`，自动调用 OpenClaw Gateway `web.login.start`，保存 `qr_data_url`，调度后台等待任务。
- `GET /web/binding-intents/{id}`：前端轮询绑定状态。
- `/openclaw/turn`：入站消息优先用已完成的 `binding_intent.channel_account_id` 或 `openclaw_login_session_key` 路由到预创建的 AI4ALL Account。

已经验证的事实：

- `openclaw channels login --channel openclaw-weixin --account bind-test-001 --verbose` 会把 `bind-test-001` 作为本次 OpenClaw QR 登录的 `sessionKey`。
- 已经连接过当前 OpenClaw 的微信扫码后，会返回 `binded_redirect`，不会新增账号，也不会破坏现有账号。
- 已登录账号的消息 payload 中可以拿到稳定的 `channel_account_id`，例如 `example-im-bot`。
- OpenClaw Gateway RPC `health` 可访问。
- `web.login.start` / `web.login.wait` 在 OpenClaw 源码中对应 `loginWithQrStart` / `loginWithQrWait`，返回 `qrDataUrl/sessionKey` 和 `connected/accountId`。
- AI4ALL 自动化测试已经覆盖二维码生成状态、等待完成绑定、以及后续入站消息路由到预创建 AI4ALL Account。
- 2026-05-20 已完成一次真实 Web 扫码绑定；2026-05-21 已补齐手机号 OTP + 阿里云图形验证码注册：验证后的 `platform_user`、预创建 `acct_...`、`binding_intent`、OpenClaw QR wait 返回的微信通道账号和 `channel_bindings` 均已对齐。
- OpenClaw QR wait 返回的微信 bot id 可能是 raw 形式（例如 `example@im.bot`），而 Bridge 入站上下文可能使用 normalized 形式（例如 `example-im-bot`）。Backend 绑定 lookup 已兼容这两种形式。
- 2026-05-20 已验收扫码后的真实微信消息：微信发送 `你好` 后，normalized `channel_account_id` 成功路由到预创建 `acct_...`，并由 Backend 生成回复。

真实运行前置条件和仍需验证：

- 当前本机 OpenClaw CLI/Gateway 设备已经批准 `operator.pairing` 和 `operator.admin` scope。权限批准后发现官方 `@tencent-weixin/openclaw-weixin@2.4.3` 缺少 `gatewayMethods` provider discovery 声明，导致 `web.login.start` 返回 `web login provider is not available`。
- 已在插件源码工作区和本机运行时安装包补充 `gatewayMethods: ["web.login.start", "web.login.wait"]`，重启 Gateway 后，`web.login.start` 已能返回 `qrDataUrl`、`message`、`sessionKey`。补丁维护说明见 `docs/openclaw-weixin-gateway-qr-patch.md`。
- OpenClaw Gateway 重启、QR 过期、用户取消、重复扫码时，`binding_intents` 状态如何恢复。

这个目标方案的优点：

- 不依赖小程序 `session_key` 和 OpenClaw `session_key` 相等。
- 不要求用户在聊天里手动输入绑定码。
- `binding_intent_id` 由 Backend 生成并保存，可以串联 `platform_user_id`、`ai4all_account_id` 和 OpenClaw QR 登录 session。
- 扫码成功后拿到真实 `channel_account_id`，再把通道身份绑定到 AI4ALL 智能体。
- 用户注册、套餐订阅、智能体配置、通道登录可以形成同一个后台审计链路。

### 6.2 兜底流程：一次性绑定码

如果正式环境里 OpenClaw QR start/wait 能力不可用，或某些用户无法完成扫码确认，可以退回到一次性绑定码：

```text
用户打开小程序 / H5
-> 微信登录
-> Backend 获得 openid / unionid
-> 创建 platform_user
-> 用户选择套餐 / 完成订阅
-> Backend 生成一次性绑定码
-> 用户把自己的微信账号扫码登录到 OpenClaw
-> 用户在 AI bot 会话中发送：#绑定 123456
-> /openclaw/turn 解析出 ai4all_account_id
-> Backend 校验绑定码
-> 创建 account_owner_bindings(platform_user_id, ai4all_account_id)
-> 后续该 AI bot 使用该用户的订阅、套餐、skills、配置
```

绑定码要求：

- 短有效期，例如 10 分钟。
- 单次使用。
- 绑定失败限频。
- 绑定成功后写 audit log。
- 允许解绑，但解绑需要产品用户重新登录确认。

### 6.3 运营后台人工绑定

适合内测阶段：

```text
用户先注册 / 付款
-> 运营在后台看到 platform_user
-> 用户扫码登录 OpenClaw 后发第一条消息
-> 后台出现新的 ai4all_account_id + channel_binding
-> 运营人工选择该账号并绑定到 platform_user
```

优点是实现最快。缺点是规模化差，容易人工误绑。

### 6.4 OpenClaw 登录事件增强

如果后续 OpenClaw 或 Bridge 能拿到“微信账号扫码登录成功”的可靠事件，可以优化成：

```text
OpenClaw 登录成功
-> Bridge 上报 channel_account_id / session_key / device info
-> Backend 创建 pending_ai4all_account
-> 用户在小程序输入或扫描该 pending account 的绑定码
-> Backend 完成 platform_user -> ai4all_account_id 绑定
```

即使有登录事件，也仍然需要一次 proof-of-control。因为 OpenClaw 通道身份不能自动推出用户在小程序/公众号里的 `openid` 或 `unionid`。

### 6.5 公众号 / 网站应用微信登录

如果我们建设公众号、服务号 H5 或网站应用微信扫码登录，推荐把它们和小程序绑定到同一个微信开放平台账号下，然后用 `unionid` 做产品用户归并。

但这只能解决“同一个人在我们不同官方应用里的身份统一”，不能自动解决“这个人扫码登录到 OpenClaw 的个人微信账号是谁”。OpenClaw 侧仍需要绑定码、扫码确认或人工审核来建立 `platform_user -> ai4all_account_id` 的关系。

## 7. 订阅与权益如何落到 bot 账号

订阅不建议直接挂在 `channel_account_id` 上，也不建议直接挂在 OpenClaw `session_key` 上。

推荐：

```text
subscription belongs to platform_user
platform_user owns one or more ai4all_account_id
ai4all_account_id consumes subscription entitlement
```

请求处理时：

1. `/openclaw/turn` 解析 `ai4all_account_id`。
2. 查询 `account_owner_bindings` 得到 `platform_user_id`。
3. 查询 `subscriptions` 得到套餐和有效期。
4. 生成 effective config：daily limit、rpm、skills、model tier、debug trace、memory quota。
5. 按 `ai4all_account_id` 继续隔离会话和记忆。

这样可以支持：

- 一个产品用户绑定一个 AI bot。
- 一个产品用户未来绑定多个 bot。
- 换微信号 / 重登 OpenClaw 时做迁移。
- 用户退订后保留记忆但降级权益。

## 8. 解绑口径

解绑要分清两层：

```text
AI4ALL 绑定解绑
= 解除 platform_user / ai4all_account_id / channel_account_id 的业务关系

OpenClaw 微信登录解绑
= 让 OpenClaw 运行时不再持有某个微信通道账号的登录态
```

删除 `openclaw-weixin` 插件不是准确的解绑方式。插件删除会影响运行时插件和本机补丁，不应该作为产品或运维解绑动作。

后续正式实现应提供：

- 产品侧解绑：校验产品用户登录态后，停用或关闭对应 `account_owner_bindings` 和 `channel_bindings`。
- 运行时解绑：通过 OpenClaw 支持的账号/设备管理能力退出或移除指定微信通道账号。
- 审计记录：保留谁在什么时间解绑了哪个 `ai4all_account_id` 和 `channel_account_id`。
- 防误绑策略：解绑后再次扫码必须重新走 `binding_intent`。

当前本机开发如需重新扫码，应优先清理 OpenClaw 通道账号登录态或使用 OpenClaw 账号管理能力；不要通过删除整个微信插件来达到解绑目的。

## 9. 风险和待确认

- `unionid` 需要小程序、公众号、网站应用绑定在同一个微信开放平台账号下，并满足下发条件；不能假设所有场景必然返回。
- OpenClaw personal 微信登录不是微信官方 OAuth；它不能天然给出小程序 `openid` 或 `unionid`。
- 不能把小程序 `session_key` 存为长期业务身份；它是敏感会话密钥。
- 不能把 `binding_intent_id` 存为长期用户身份；它只是一次性绑定流程 ID，用完后应失效。
- 如果用户换手机、重登、OpenClaw sessionKey 格式变化，需要通过 `channel_bindings` 和 owner binding 做迁移策略。
- 如果一个用户误把绑定码发给别人，可能误绑；绑定码兜底路径需要短 TTL、单次使用、绑定后提醒、解绑能力和后台审计。
- 重复扫码、已登录账号再次扫码、OpenClaw 返回 `already_connected` / `binded_redirect` 时，产品策略需要明确：直接复用、拒绝绑定，还是要求先解绑。
- 当前“一个接入个人微信账号 = 一个 AI bot/account”的产品边界仍成立；如果未来改成公共服务号客服模型，身份模型要重做。

## 10. 推荐下一步

短期建议按这个顺序做：

1. 定义重复绑定策略：同一个 `channel_account_id` 已绑定时，是否允许绑定到新 `ai4all_account_id`、是否要求先解绑、前端如何展示 `already_connected`。
2. 实现正式解绑能力：产品侧解绑、OpenClaw 通道登录态解绑、后台审计和重新绑定流程。
3. 补齐 `binding_intents` 异常状态：expired、cancelled、wait_failed、gateway_restart 后的恢复策略。
4. 在 Admin/运营视图展示 `platform_users`、`account_owner_bindings`、`binding_intents`、`channel_bindings` 的关联链路。
5. 将限流和 skills 启用从 `accounts` 默认配置迁移到“账号配置 + 订阅权益”的 effective config。
6. 将 `openclaw-weixin` 的 `gatewayMethods` 补丁产品化：上游 PR、固定版本或安装后校验。
7. 后续再考虑小程序/H5 登录、支付、自动续费和多 bot 账号管理。

## 11. 参考资料

- 微信小程序 `auth.code2Session` 官方文档：`https://developers.weixin.qq.com/miniprogram/dev/api-backend/open-api/login/auth.code2Session.html`
- 微信小程序 UnionID 机制官方文档：`https://developers.weixin.qq.com/miniprogram/dev/framework/open-ability/union-id.html`
- 微信网页授权官方文档：`https://developers.weixin.qq.com/doc/offiaccount/OA_Web_Apps/Wechat_webpage_authorization.html`
- 微信开放平台网站应用微信登录官方文档：`https://developers.weixin.qq.com/doc/oplatform/Website_App/WeChat_Login/Wechat_Login.html`

说明：官方开发者站点在本地检索工具中无法直接打开正文，但文档路径和字段语义已通过可访问的微信开发文档镜像交叉核对。后续实际开发前，应由研发在浏览器中再次打开官方文档确认最新字段和下发条件。
