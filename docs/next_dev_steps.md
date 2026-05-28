# 下一步开发步骤：架构梳理与主动消息联调

更新时间：2026-05-24

## 当前阶段判断

主动消息与提醒机制的代码开发已经基本完成，当前不继续追加新能力。已完成 reminder、heartbeat、hidden commitment、outbound ledger、scheduler、Admin API 和文档基准；自动化测试已通过，但真实微信端到端联调尚未完成。

总体/框架设计已经整理到 `docs/architecture_overview.md`，Phase 1 详细技术设计已经整理到 `docs/phase1_technical_design.md`。接下来按这两份文档收口模块边界，并做一次统一联调：

```text
普通聊天
-> hidden commitment 抽取 pending
-> scheduler due dispatch
-> outbound ledger / quiet hours / daily limit
-> OpenClaw Gateway send
-> 微信主动收到消息
```

## 当前已完成

已经走通并验收：

```text
Web 输入手机号
-> 阿里云验证码 + 短信 OTP
-> 一次性 otp_token 注册
-> 创建或复用 platform_user
-> 创建或复用默认 AI4ALL Account: acct_...
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
- `openclaw-weixin` 当前需要 `gatewayMethods: ["web.login.start", "web.login.wait"]` 补丁；维护说明见 `docs/tech_design/openclaw_weixin_gateway_qr_patch.md`。
- Aliyun SMS/Captcha 在 local/test 可 mock；非 local/test 缺少凭据会失败关闭。
- 当前 `onboarding.html` 的 Aliyun Captcha `SceneId` / `prefix` 仍需手工和控制台保持一致。
- 当前 Web 页面已去掉显式“创建智能体”步骤，OTP 验证后通过 `/web/register-and-binding-intent` 直接生成二维码。

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

## Step 7：主动消息与提醒 POC

最新开发基准见 `docs/tech_design/proactive_messaging_design.md`。

当前进展：

- Backend 通过 OpenClaw Gateway 通用 `send` RPC 主动发送微信文本，不直连 `openclaw-weixin` 的 `sendmessage` endpoint。
- 真实验证结论：`to_user_id` 使用 `channel_bindings.chat_id`，`accountId` 使用 `channel_bindings.channel_account_id`。
- 已完成 `outbound_messages` ledger、主动发送每日上限、quiet hours 和 Gateway 成功发送状态流转的最小实现。
- 已完成一次性 reminder MVP 的底层能力：`reminders` 表、due scan、原子 claim、调用 `app.proactive.messaging.send_proactive_text()`、成功/取消/失败回写。
- 已完成显式提醒识别：高确定性文本会在 `/openclaw/turn` 中写入 `reminders`，模糊提醒会要求补充具体日期和时间。
- 已完成系统级 proactive scheduler 第一阶段：admin run-once、可选 FastAPI loop、独立 worker 脚本；默认不自动启动，生产启用前必须选择单进程 FastAPI 或独立 worker。
- 已完成架构整理：`/openclaw/turn` 业务逻辑抽到 `app.turn_service`，proactive 相关代码收拢到 `app/proactive/` package；旧顶层 proactive 兼容模块已删除。
- 已新增 `proactive_account_state` 表和 `app.proactive.state`，作为未来 heartbeat / commitment cheap pre-filter 的账号级状态底座。
- 已新增 Admin API：`GET/PATCH /admin/accounts/{account_id}/proactive-state`，用于开发阶段查看、开启和调整账号级 proactive state。
- 已接入 due account scan：`ProactiveScheduler.run_once()` 会在 due reminder / due commitment dispatch 后扫描 due proactive accounts，原子 claim/mark scanned 后调用 `app.proactive.heartbeat.decide_heartbeat_action()` 和 `execute_heartbeat_decision()`；它不会过滤现有 reminder。
- 已完成用户级 heartbeat 非 LLM 决策与受控执行：默认 `no_candidate` no-op；state metadata 中有显式 `heartbeat_candidate` 且通过 route、quiet hours、daily limit 等策略时，会通过 outbound ledger / Gateway 发送，并回写 `last_proactive_sent_at`。
- 已完成隐藏 LLM 候选生成/筛选链路：`POST /admin/accounts/{account_id}/heartbeat-candidate-draft` 会用独立 prompt 生成 draft，只写 `metadata.heartbeat_candidate_draft`，不会触发发送。
- 已完成 draft 人工确认链路：`POST /admin/accounts/{account_id}/heartbeat-candidate-draft/promote` 会把 draft 提升为可发送的 `heartbeat_candidate`；`DELETE /admin/accounts/{account_id}/heartbeat-candidate-draft` 可清理不合适的 draft。
- heartbeat 发送成功后会把 active `heartbeat_candidate` 移到 `heartbeat_last_sent_candidate`，避免同一候选重复发送。
- 已完成 hidden commitment 第一版代码逻辑：普通聊天回复成功后后台独立 prompt 抽取 inferred follow-up，写入 `proactive_commitments.pending`；scheduler 到期后先处理 due commitments，再进入账号 heartbeat scan；发送仍走 outbound ledger、quiet hours、daily limit 和 Gateway。
- 已新增 Admin API：`GET /admin/accounts/{account_id}/commitments` 查看 commitment；`POST /admin/commitments/{commitment_id}/cancel` 取消不应发送的 commitment。
- 当前暂停继续扩展主动消息功能；下一步先做整体架构梳理和对齐。架构对齐后，再整体联调这条链路，并基于真实效果调 prompt、阈值和风控策略；之后再做周期性提醒、自然语言取消/更新提醒、用户级 timezone 和多实例 worker lease。

微信 bot 可测试节点：

- `你好`：正常聊天回归。
- `下午提醒我去趟派出所`：应要求补充具体日期和时间。
- `明天上午10点提醒我检查事情A`：应确认已设置提醒，并写入 `reminders.status=pending`。
- 到点主动推送：可调用 `POST /admin/proactive/scheduler/run-once` 触发一次 due reminder 扫描，或启动 `scripts/run_proactive_scheduler.py` 独立 worker。
- heartbeat 主动触达阶段性验证：真实账号发过微信消息后，开启 proactive state，调用 `POST /admin/accounts/{account_id}/heartbeat-candidate-draft` 生成 draft，人工 promote，再调用 `POST /admin/proactive/scheduler/run-once?limit=20`。若未命中 quiet hours / daily limit / route 缺失，应从微信收到主动消息。
- commitment 最终联调：开启 proactive state 后，通过微信普通聊天产生明确未来后续事项；用 `GET /admin/accounts/{account_id}/commitments` 确认 pending；到期后 `POST /admin/proactive/scheduler/run-once?limit=20`，应收到 `source=commitment` 的主动消息。

## 暂缓事项

- 小程序/H5 微信官方登录。
- 支付、自动续费和套餐后台。
- 多 bot 账号管理。
- 语音 ASR。
- 图片/多模态。
