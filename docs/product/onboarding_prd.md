# 产品专题 PRD：注册与扫码接入

更新时间：2026-06-02

## 1. 目标

让普通用户通过 Web/H5 完成手机号验证，并一键扫码接入微信 OpenClawBot 通道，获得一个独立 AI4ALL Account。

## 2. 用户流程

1. 用户打开 Web/H5 onboarding 页面。
2. 用户输入大陆手机号。
3. 用户完成阿里云图形验证码。
4. 系统发送短信 OTP。
5. 用户输入 OTP，后端校验成功后返回一次性 `otp_token`。`otp_token` 代表“该手机号已通过验证”，只能被消费一次。
6. 前端调用 `/web/register-and-binding-intent`，携带手机号和 `otp_token`。
7. Backend 原子消耗 `otp_token`，按手机号创建或复用 `platform_user`。
8. Backend 为该 `platform_user` 创建或复用默认 AI4ALL Account。当前代码中的 `account.id` / `account_id` 语义上就是目标 `ai4all_account_id`。
9. Backend 创建一次扫码绑定意图 `binding_intent`。当前实现中 `binding_intent.id` 是绑定流程 ID，初始也作为 `openclaw_login_session_key` 传给 OpenClaw。
10. Backend 调 OpenClaw Gateway 发起微信 QR 登录。若 Gateway 返回新的 `sessionKey`，Backend 会用它更新 `binding_intent.openclaw_login_session_key`。
11. 前端展示二维码，用户用微信扫码。
12. Backend 等待 OpenClaw QR wait 结果，拿到通道侧账号标识，例如 `accountId` / `channel_account_id`，并将 `binding_intent` 标记为 completed。
13. Backend 根据 `binding_intent` 中保存的 `platform_user_id`、`account_id`、`openclaw_login_session_key` 和 wait 返回的 `channel_account_id` 写入 `channel_bindings`。
14. 后续真实微信私聊入站时，Backend 通过 `channel_account_id` 或 `openclaw_login_session_key` / `session_key` 解析到预创建的 AI4ALL Account。
15. 用户在微信私聊里使用个人 AI bot。

### 2.1 流程与 ID 关系图

```text
外部用户唯一入口
  phone
    |
    | SMS OTP verified
    v
platform_user
  id = pu_...
  phone = 138...
    |
    | Phase 1 普通入口一一对应
    v
AI4ALL Account
  account.id / account_id = ai4all_account_id = acct_...
  Soul / Context / Memory / Sessions / Messages / Usage / Entitlement
    |
    | owner relation
    v
account_owner_binding
  platform_user_id -> ai4all_account_id

扫码绑定过程
  binding_intent
    id = binding_intent_id
    platform_user_id = pu_...
    account_id = acct_...
    openclaw_login_session_key = 本次 QR 登录 session key
    |
    | OpenClaw Gateway QR login / wait
    v
channel identity
  channel_account_id = OpenClaw / 微信通道侧账号
  session_key = OpenClaw 入站会话键
  chat_id / sender_id = 通道路由和排障字段
    |
    | completed binding
    v
channel_binding
  ai4all_account_id = acct_...
  channel_account_id / session_key / chat_id / sender_id

真实微信私聊入站
  channel_account_id or openclaw_login_session_key or session_key
    -> identity resolver
    -> ai4all_account_id = acct_...
    -> account-level active session
```

口径说明：

- 外部来说，Phase 1 核心唯一标识是手机号。
- 内部来说，业务隔离核心唯一标识是 AI4ALL Account。
- 当前代码中的 `account.id` / `account_id` 语义上就是目标 `ai4all_account_id`。
- Phase 1 普通入口下，一个手机号对应一个 `platform_user`，一个 `platform_user` 对应一个默认 AI4ALL Account。
- 换手机号后如何与原 AI4ALL Account 重新绑定先搁置，后续通过客服/Admin 和单独设计完善。
- 对话 session 的核心边界按 AI4ALL Account，不按 OpenClaw `session_key`；`session_key` 是通道路由、兼容和排障字段。

## 3. 当前身份与绑定原则

Phase 1 以手机号作为外部用户唯一标识。用户通过短信 OTP 证明手机号控制权后，Backend 创建或复用 `platform_user`；AI4ALL Account、绑定关系、权益和客服处理都挂到该 `platform_user` 或其拥有的 AI4ALL Account 上。

当前不计划接入小程序、公众号或网站微信登录作为独立账号体系。即使未来接入微信登录，也应优先通过合规方式拿到并验证手机号，再映射到同一个 `platform_user`。微信 `openid` / `unionid` 可作为辅助风控、合并提示或排障信息，但不作为 Phase 1 的主用户身份。

这样设计的好处：

- 对普通用户解释成本低：手机号验证后扫码接入。
- 对多渠道更稳定：未来 H5、小程序、客服入口都可以回到同一个手机号身份。
- 对 AI4ALL Account 隔离更清晰：手机号负责 owner 身份，微信扫码负责 channel binding。

主要风险与约束：

- 手机号可能换绑、注销或被运营商回收，因此需要清晰告知“重新注册 + 重新扫码”的规则，并提供误绑/解绑的客服处理路径。
- 同一个微信通道账号不能静默绑定到另一个手机号，否则会造成账号串线。
- Phase 1 不允许一个 `platform_user` 拥有多个 AI4ALL Account；同一手机号重复 onboarding 时始终复用默认 AI4ALL Account。
- 换手机号或手机号回收后，不做旧账号自动迁移；用户需要用新手机号重新注册，并重新扫码完成手机号、微信登录 session key 与新 AI4ALL Account 的绑定。
- 仅用手机号无法天然识别“同一个微信用户换手机号”或“同一个手机号绑定多个微信”的复杂场景；Phase 1 以重新注册和重新扫码为准，客服/Admin 只处理误绑、解绑和异常修复。
- 存储手机号涉及隐私合规，正式开放前必须补充隐私政策、数据保留和删除流程。

## 4. 功能需求

- 用户必须通过手机号 OTP 后才能创建或复用产品用户。
- local/test 环境可 mock 短信和验证码；非 local/test 环境缺少凭据必须失败关闭。
- `/web/register-and-binding-intent` 应原子消耗 OTP token，防止并发重放。
- 默认 AI4ALL Account 应幂等复用，避免重复注册时反复创建账号。
- Phase 1 不允许一个 `platform_user` 拥有多个 AI4ALL Account；普通入口和接口都应复用默认账号。
- Phase 1 以手机号作为 `platform_user` 唯一外部身份；微信扫码结果只作为通道绑定身份。
- 二维码状态至少需要覆盖首次绑定主路径：`created`、`qr_created`、`completed`、`failed`。
- `expired`、`cancelled`、`wait_failed`、`already_connected` 等异常状态需要保留记录能力和排障可见性，但不作为当前内测首发阻塞项。
- OpenClaw QR wait 返回 raw id 与 Bridge 入站 normalized id 时，Backend 应能通过 alias lookup 对齐。
- 同一 `channel_account_id` 已绑定到其他 AI4ALL Account 的重复绑定策略暂缓，不作为当前内测首发阻塞项。原因是部分状态和控制在 OpenClaw 通道层，Backend 不能完全可靠地判断或接管。
- 当前优先保证首次绑定成功后，真实微信私聊能稳定路由到预创建的 AI4ALL Account，并持续使用不串线。

## 5. 当前内测绑定策略

- 当前内测首发只承诺首次绑定链路：手机号 OTP 通过后创建或复用默认 AI4ALL Account，扫码完成后写入 channel binding，后续微信消息稳定进入该账号。
- 重复绑定、同一微信绑定多个手机号、同一手机号更换微信、`already_connected` / `binded_redirect` 等策略先记录为后续专题处理，不在当前开发窗口继续展开。
- 如 OpenClaw 通道层返回已连接或复用状态，Backend 先以“不串线、可排障、可人工处理”为底线，不在产品侧承诺自动迁移。
- 用户主动解绑需要区分 AI4ALL 业务解绑和 OpenClaw runtime 解绑。
- 用户换手机号或手机号被回收后，不做旧账号找回或自动迁移；用户需要使用新手机号重新注册，并重新扫码生成新的绑定关系。
- 同一手机号绑定多个微信、同一微信账号尝试绑定多个手机号等异常场景，内测阶段优先通过客服/Admin 观察和人工处理，不绕过“手机号重新注册 + 微信重新扫码”的主流程。
- Admin 需要能查看绑定链路和通道状态，便于排查首次绑定失败、扫码后消息路由失败和后续人工修复。

## 6. 体验要求

- Onboarding 文案应解释“扫码接入微信 AI bot”，避免暴露过多 OpenClaw、Gateway、插件等实现细节。
- 失败时给出用户可理解的提示，例如验证码失败、OTP 过期、二维码过期、扫码超时。
- 不要求普通用户理解 `platform_user`、`ai4all_account_id`、`channel_account_id` 等内部概念。

## 7. 运营需求

- Admin 能看到平台用户、AI4ALL Account、binding intent 和 channel binding 的关系。
- Admin 能查看最近一次绑定状态、错误、过期时间和原始返回。
- Admin 能排查入站消息当前解析出的 `ai4all_account_id`、`session_key` 和 `channel_account_id`。
- Admin 能按手机号、AI4ALL Account、binding intent、OpenClaw login session key、channel account id 检索绑定链路。

## 8. 验收标准

- 用户可以通过手机号 OTP 完成注册。
- OTP 通过后系统创建或复用 `platform_user` 和默认 AI4ALL Account。
- 用户扫码后 `binding_intents.status=completed`，并写入 `channel_bindings`。
- 扫码后的真实微信私聊消息能路由到预创建 `acct_...`。
- 首次绑定后的真实微信私聊能稳定使用，且不会串到其他 AI4ALL Account。
- 同一手机号重复 onboarding 时复用同一个 `platform_user` 和默认 AI4ALL Account。
- 一个 `platform_user` 不能通过普通产品入口创建多个 AI4ALL Account。
- 换手机号场景走新手机号重新注册和重新扫码绑定，不自动继承旧手机号账号数据。

## 9. 待确认

- 重复绑定、解绑、换微信号的正式产品话术和技术控制边界。
- 同一手机号绑定多个微信账号、同一微信账号尝试绑定多个手机号时的具体处理话术。
- OpenClaw 通道层 `already_connected`、运行时复用和账号退出能力的正式接管方式。
