# 下一步开发步骤：Web Onboarding 绑定收口

更新时间：2026-05-21

## 当前已完成

已经走通并验收：

```text
Web 输入手机号
-> 阿里云验证码 + 短信 OTP
-> 一次性 otp_token 注册
-> 创建或复用 platform_user
-> 创建 AI4ALL Account: acct_...
-> 创建 binding_intent: bind_...
-> Backend 调 OpenClaw Gateway web.login.start
-> Web 展示二维码并轮询 binding_intent
-> Backend 调 web.login.wait
-> 微信扫码完成
-> Backend 写入 completed binding_intent + channel_bindings
-> 微信发送真实消息
-> /openclaw/turn 通过 channel_account_id alias lookup 路由到预创建 acct_...
-> Backend 生成回复并回到微信
```

OpenClaw QR wait 返回的通道账号可能是 raw 形式，例如 `example@im.bot`；Bridge 入站上下文可能使用 normalized 形式，例如 `example-im-bot`。Backend 已兼容这两种形式。

本地运行时要点：

- Backend 默认开发端口可以是 `8000`。
- 本轮 Web onboarding 验证使用 `8012`。
- Bridge 的 Backend URL 必须和 FastAPI 实际端口一致。
- `openclaw-weixin` 当前需要 `gatewayMethods: ["web.login.start", "web.login.wait"]` 补丁；维护说明见 `docs/openclaw-weixin-gateway-qr-patch.md`。
- Aliyun SMS/Captcha 在 local/test 可 mock；非 local/test 缺少凭据会失败关闭。
- 当前 `onboarding.html` 的 Aliyun Captcha `SceneId` / `prefix` 仍需手工和控制台保持一致。

## Step 0：前端验证码配置收口

当前 backend 配置已经放在 `.env` / `app.config`，但静态 `onboarding.html` 不能直接读取后端配置，因此 `SceneId` / `prefix` 仍存在多环境漂移风险。

建议新增一个只返回公开前端配置的接口或构建期注入：

- `GET /web/config` 返回 `captcha_scene_id`、`captcha_prefix` 等非敏感配置。
- `onboarding.html` 初始化验证码前先读取配置。
- 确认阿里云 Captcha SDK 使用的接入模式和官方文档一致。
- 禁止把 AccessKey、短信模板密钥等服务端敏感配置暴露给前端。

## Step 1：重复绑定策略

需要先定义产品行为，再实现代码：

- 同一个 `channel_account_id` 已绑定到同一个 `ai4all_account_id`：前端展示已绑定，避免重复创建绑定记录。
- 同一个 `channel_account_id` 已绑定到另一个 `ai4all_account_id`：建议默认拒绝，并要求先解绑。
- OpenClaw 返回 `already_connected` / `binded_redirect`：不要静默绑定到新账号，除非能确认这是用户主动迁移。
- 同一个产品用户是否允许多个 AI bot：需要和套餐/订阅策略一起定义。

## Step 2：正式解绑能力

解绑要分两层：

```text
AI4ALL 业务解绑
= 停用或关闭 platform_user / ai4all_account_id / channel_account_id 的绑定关系

OpenClaw 运行时解绑
= 让 OpenClaw 不再持有某个微信通道账号登录态
```

不要把删除 `openclaw-weixin` 插件作为解绑方式。插件删除会影响运行时和本地补丁，不是准确的账号解绑动作。

建议新增：

- 产品侧解绑 API：校验产品用户登录态后停用 `account_owner_bindings` / `channel_bindings`。
- Admin 解绑 API：用于内测和客服处理误绑。
- OpenClaw 运行时解绑入口：封装账号退出/移除能力，避免手工清理运行时文件。
- 解绑审计：记录操作者、时间、`ai4all_account_id`、`channel_account_id` 和原因。

## Step 3：Binding Intent 异常状态

当前主路径已跑通，下一步补齐异常状态：

- `expired`：二维码过期或用户长时间未扫码。
- `cancelled`：用户在 Web 端取消绑定。
- `wait_failed`：OpenClaw Gateway 调用失败或超时。
- `already_connected`：扫码账号已在当前 OpenClaw 运行时登录。
- `replaced`：用户放弃旧 intent，重新生成二维码。

这些状态需要同时体现在：

- `binding_intents.status`
- Web 轮询响应
- 前端提示文案
- Admin 排障视图

## Step 4：运营后台视图

最小可用视图应能串起这几张表：

- `platform_users`
- `subscriptions`
- `accounts`
- `account_owner_bindings`
- `binding_intents`
- `channel_bindings`

重点展示：

- 一个产品用户拥有哪些 AI4ALL Accounts。
- 每个 AI4ALL Account 当前绑定了哪个微信通道账号。
- 最近一次 `binding_intent` 的状态、错误、过期时间和原始返回。
- 入站消息当前解析出的 `ai4all_account_id`、`openclaw_session_key_account_id`、`channel_account_id`。

## Step 5：权益和配置生效

绑定链路稳定后，再把运行权益接入请求链路：

```text
/openclaw/turn
-> ai4all_account_id
-> account_owner_bindings
-> platform_user
-> subscriptions
-> effective config
```

effective config 应覆盖：

- daily limit / rpm
- model tier
- skills 开关
- debug trace 白名单
- memory quota

## Step 6：补丁产品化

当前 `openclaw-weixin` 的 `gatewayMethods` 是本地补丁。后续需要选择一种正式方案：

- 给上游提交 PR。
- 固定已验证插件版本并在安装后自动校验。
- 在启动前检查 provider discovery 是否能发现 `web.login.start` / `web.login.wait`。
- 升级 OpenClaw 或插件后重新跑 QR start/wait 验收。

## 暂缓事项

- 小程序/H5 微信官方登录。
- 支付、自动续费和套餐后台。
- 多 bot 账号管理。
- 语音 ASR。
- 图片/多模态。
