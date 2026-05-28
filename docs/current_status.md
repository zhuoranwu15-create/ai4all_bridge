# AI4ALL 微信个人 AI 陪伴服务当前状态

更新时间：2026-05-23

## 项目定位

AI4ALL 微信个人 AI 陪伴服务面向普通用户，提供手机号验证后一键扫码接入微信 OpenClawBot 通道的个人 AI 体验。用户不需要部署 OpenClaw、不需要配置模型或服务器，即可在微信私聊里使用由 AI4ALL Backend 驱动的个人 AI bot。

当前阶段不是通用办公效率助手，也不是心理咨询产品。产品第一定位是聊天陪伴，其次兼顾轻量个人助理。重点是：

- 日常聊天中的情感陪伴。
- 自然、温和、有持续感的微信私聊体验。
- 轻量生活助理能力，例如明确时间的一次性提醒、信息搜索、每日新闻或搞笑内容等垂类推送。
- 通过类 OpenClaw 架构，让更多普通用户以较低成本使用个人 AI。
- OpenClaw / `openclaw-weixin` 只承担微信通道和运行时 hook；AI4ALL Backend 承担产品体验和业务状态。
- 每个用户对应独立 AI4ALL Account，拥有独立 Soul、会话上下文、记忆、配置和用量。

简化目标链路：

```text
普通用户
-> Web/H5 手机号验证
-> 扫码接入微信 OpenClawBot 通道
-> AI4ALL Backend 创建或复用 acct_...
-> 用户在微信私聊中获得陪伴式对话和轻量助理能力
```

## 2026-05-18 身份模型修正

当前代码和早期文档里仍大量使用 `account_id`。这个字段在 AI4ALL Backend 内部应理解为历史命名下的业务隔离 ID，而不是 OpenClaw payload 原生 `account_id`。

中长期架构已经确认：

```text
ai4all_account_id
= AI4ALL 业务账号主键
= 未绑定 legacy 入站可由 OpenClaw session_key fallback
= Web onboarding 绑定完成后使用 Backend 预创建的 acct_...

OpenClaw account_id / provider account id
= 通道侧或机器人账号标识
= 不作为 AI4ALL 用户隔离主键
```

详细说明见 `docs/architecture_overview.md`、`docs/phase1_technical_design.md` 和 `docs/tech_design/identity_model_and_wechat_binding.md`。

当前代码通过 `app.identity.resolve_openclaw_identity()` 统一解析 OpenClaw 入站身份。DB 字段仍保留 `account_id` 旧命名，但其语义是 AI4ALL 业务账号 ID；OpenClaw 原生账号字段作为 `channel_account_id` 写入 metadata，并记录到 `channel_bindings` 表，用于排查通道和后续账号迁移。Bridge payload 已显式发送 `channel_account_id`，同时保留 legacy `account_id` 兼容旧后端。

## 2026-05-21 手机号 OTP + 阿里云验证码注册

Web 注册流程现在要求通过完整的 OTP 验证后才能创建账号。

```text
[1] 用户输入手机号 → 点击「获取验证码」→ 触发阿里云验证码弹窗（一点即过）
[2] captchaVerifyCallback 透传 captcha_verify_param → POST /web/sms/send-otp
[3] 后端校验验证码 + 手机号频率限额（默认每小时 5 条）→ 发送 6 位 OTP 短信
[4] 用户输入 OTP → POST /web/sms/verify-otp → 返回一次性 verified_token（10 分钟有效）
[5] 前端调用 POST /web/register-and-binding-intent（携带 otp_token）→ 原子消耗 token → 创建或复用平台用户和默认 AI4ALL Account → 生成微信登录二维码
```

已实现并验收的安全机制：

- 阿里云验证码 2.0（一点即过 popup 模式）拦截自动化脚本
- 手机号格式正则校验（`^1[3-9]\d{9}$`，仅接受大陆手机号）
- 每小时每号限额（默认 5 条）
- 错误 5 次后锁定当前 OTP 校验，需重新发送
- `secrets.randbelow` 密码学安全随机 OTP
- `consume_valid_verification_token` 原子 UPDATE，防止 token 并发重放
- 默认 AI4ALL Account 幂等复用，避免重复注册时反复创建账号
- SMS 发送失败立即清理验证记录
- 非 local/test 环境下缺少 Aliyun 凭据直接抛 `RuntimeError`（不允许生产环境静默跳过）
- local/test 环境无凭据时自动 mock，OTP 打印到服务日志

新增模块：`app/sms.py`、`app/captcha.py`
新增数据表：`phone_verifications`
新增配置：见 `.env.example` 中 `ALIYUN_*` 和 `OTP_*` 块

当前前端仍是静态 `onboarding.html`，Aliyun Captcha `SceneId` / `prefix` 需要和控制台配置手工保持一致；后续应改为运行时配置注入，避免多环境漂移。

详见设计文档：`docs/archive/superpowers/specs/2026-05-21-phone-otp-captcha-design.md`

## 2026-05-20 Web Onboarding 进展

已新增并真实验证最小 Web 注册和扫码绑定链路：

```text
/ui/onboarding.html
-> 阿里云验证码 + POST /web/sms/send-otp
-> POST /web/sms/verify-otp
-> POST /web/register-and-binding-intent（携带 otp_token）
-> Backend 创建或复用 platform_user + 默认 AI4ALL Account + binding_intent
-> Backend 调 OpenClaw Gateway web.login.start
-> 前端展示 qr_data_url 并轮询状态
-> Backend 后台调 web.login.wait
-> 成功后写入 binding_intents + channel_bindings
-> /openclaw/turn 根据 channel_account_id/openclaw_login_session_key 路由到预创建 AI4ALL Account
```

当前本机 OpenClaw CLI/Gateway 设备已经批准 `operator.pairing` 和 `operator.admin` scope。权限批准后发现官方 `@tencent-weixin/openclaw-weixin@2.4.3` 虽然实现了 `loginWithQrStart` / `loginWithQrWait`，但没有声明 OpenClaw host 用于发现 provider 的 `gatewayMethods`，导致 `web.login.start` 返回 `web login provider is not available`。

已在官方插件源码工作区和本机运行时安装包补充 `gatewayMethods: ["web.login.start", "web.login.wait"]`，并重启 Gateway。现在 `openclaw gateway call web.login.start` 已可返回 `qrDataUrl`、`message`、`sessionKey`。补丁维护说明见 `docs/tech_design/openclaw_weixin_gateway_qr_patch.md`。

真实扫码绑定已完成一次本机验证。为避免长期文档沉淀本机临时账号标识，这里只保留结构化结果：

```text
platform_user(phone=用户输入手机号)
-> 预创建 AI4ALL Account: acct_...
-> binding_intent: bind_...
-> QR wait 返回 raw channel_account_id: example@im.bot
-> OpenClaw runtime 使用 normalized channel_account_id: example-im-bot
-> binding_intents.status = completed
-> channel_bindings 记录通道账号与本次 openclaw_login_session_key
```

注意：OpenClaw QR wait 返回 raw id（`@im.bot`），Bridge 入站上下文常见 normalized id（`-im-bot`）。Backend 已兼容这两种形式，避免扫码完成后第一条真实消息落回 session_key 派生账号。

扫码后的真实微信消息也已完成验收：用户在微信发送 `你好` 后，Bridge 将消息转发到 `http://127.0.0.1:8012/openclaw/turn`，Backend 将 normalized `channel_account_id` 路由到预创建的 `acct_...`，并通过 DeepSeek 生成回复。

## 当前事实模型

这里需要明确区分官方 OpenClaw 能力和 AI4ALL 要补齐的能力。

### 官方 OpenClaw / openclaw-weixin

官方 `openclaw-weixin` 支持在同一个 OpenClaw 实例中连接多个微信账号，并提供一定的 session 隔离配置。

但 OpenClaw 原生的 `soul.md`、长期记忆、部分全局配置更偏实例级共享。多个微信账号共用一套 Soul/记忆配置，不符合 AI4ALL 对“每个用户拥有自己的个人 AI 陪伴/助理”的需求。

这也是 AI4ALL Bridge 需要存在的核心原因之一：保留 OpenClaw 的微信连接能力，把用户、Soul、记忆和业务配置转移到 AI4ALL Backend 按账号隔离管理。

### AI4ALL 目标模型

目标链路是：

```text
普通用户注册 / 手机号验证
-> 扫码接入微信 OpenClawBot 通道
-> Backend 创建或复用 AI4ALL Account
-> 每个 AI4ALL Account 拥有自己的 AI bot 会话
-> AI4ALL Backend 按账号隔离 Soul、记忆、会话、配置和用量
```

当前不采用“一个服务微信号服务多个外部微信用户”的客服号模型。

在当前微信插件约束下，用户在微信里看到的是自己的 bot 会话。这个 bot 不作为群聊机器人使用，也不作为一个公共客服号同时和多个外部用户聊天。OpenClaw 是通道基础设施，不是用户侧产品身份。

### 已验证能力

当前已经验证的是：

```text
一个 OpenClaw 实例
-> 两个已登录个人微信账号
-> 私聊文本消息进入 AI4ALL Backend
-> Bridge 从 sessionKey 派生 AI4ALL 业务账号 ID
-> Backend 按 AI4ALL 业务账号 + session_key 隔离上下文
-> 每个 AI4ALL 业务账号自动拥有独立 user_profile.md
-> Backend 调用 DeepSeek
-> 回复回到微信
```

此前已用两个个人微信账号验证多账号私聊隔离；具体本机通道账号 ID 不再作为当前状态文档的一部分。

未绑定 legacy 入站时，Backend 内部历史命名的 `account_id` 可 fallback 为 OpenClaw sessionKey。典型格式：

```text
agent:main:openclaw-weixin:<account_id>:direct:<peer_id>
```

## 已完成内容

### 微信端到端链路

真实微信链路已经跑通：

```text
微信私聊消息
-> openclaw-weixin
-> OpenClaw Gateway
-> ai4all-openclaw-bridge 插件
-> AI4ALL Backend
-> DeepSeek / OpenAI-compatible LLM
-> 回复回到微信
```

当前使用官方 OpenClaw 和官方 `openclaw-weixin`，没有 fork 两个官方仓库。

### Bridge 插件

已实现 `ai4all-openclaw-bridge`：

- 注册 OpenClaw `before_agent_reply` hook。
- 将私聊文本消息转发到 AI4ALL Backend。
- 使用 `AI4ALL_BRIDGE_SECRET` 做 Bearer 鉴权。
- 将 Backend 返回的回复作为 synthetic reply 交回 OpenClaw。
- Backend 异常时返回友好兜底文案。
- 原始 `ctx` / `event` 已随消息保存，便于排查 OpenClaw payload。
- 从 `ctx.sessionKey` 派生 AI4ALL 业务账号 ID。
- 在 payload 中发送 `channel_account_id`，并保留 legacy `account_id` 兼容别名。
- 在 raw 中记录 `ai4all_bridge.account_candidates`、`resolved_account_id` 和 `channel_account_id`。

### Backend

已实现 FastAPI Backend：

- `GET /health`
- `POST /openclaw/turn`
- SQLite 存储。
- OpenAI-compatible LLM 调用。
- DeepSeek 真实 token/model 已验证。
- 未配置 `LLM_API_KEY` 时走本地 mock fallback。
- 每个 AI4ALL 业务账号自动创建并读取独立 `user_profile.md`。

### 数据隔离

Backend 当前按以下维度隔离上下文：

```text
AI4ALL 业务账号 ID + session_key
```

当前数据表：

- `accounts`：接入的微信/OpenClaw 账号。
- `contacts`：历史命名，当前可理解为账号下的会话参与方记录，后续可能重命名或弱化。
- `sessions`：会话。
- `profiles`：风格、Prompt、偏好配置。
- `messages`：收发消息历史、耗时、错误。
- `data/user_profiles/<account_id>/user_profile.md`：账号级 Soul 和简化长期记忆。这里的 `<account_id>` 是当前代码历史命名，语义上对应 AI4ALL 业务账号 ID。

`accounts` 已是当前账号隔离的核心实体；`contacts` 仍是早期兼容命名。

### 管理能力

当前还没有 Web 后台页面。

已经实现的是基于 Token 鉴权的 Admin API：

- 查看账号。
- 查看会话。
- 查看消息。
- 修改当前兼容用户记录的备注和状态。
- 修改 profile。
- 重置会话。

现有 Admin API 仍带有 `contacts` 命名，这是早期客服号模型留下的命名，后续需要调整为账号级管理。

### 可观测性

已实现：

- 消息持久化。
- 基于 `message_id` 的去重。
- turn 级耗时 `latency_ms`。
- outbound message 记录错误信息。
- 本地开发用 debug API。
- raw payload 查询接口。
- 账号级 `user_profile.md` 查看接口。

## 已验证事项

- 微信已连接 OpenClaw。
- 一个 OpenClaw 实例已同时接入两个个人微信账号。
- 微信消息可以进入 AI4ALL Backend。
- AI4ALL Backend 可以回复到微信。
- Bridge 已能从 `sessionKey` 派生稳定的 AI4ALL 业务账号 ID。
- 两个账号分别生成独立 `user_profile.md`。
- DeepSeek 真实模型调用可用。
- SQLite schema 可以自动创建和迁移当前字段。
- `message_id` 去重可用。
- Admin API 不带 token 返回 `401`。
- 禁用当前兼容用户记录后不回复。
- 重新启用后恢复回复。
- Web onboarding 可以用手机号创建 `platform_user`、创建 AI4ALL Account、生成 OpenClaw 登录二维码，并在真实微信扫码后把通道账号绑定到预创建的 `acct_...`。
- 扫码后的真实微信消息可以通过 `channel_account_id` alias lookup 路由到预创建 `acct_...` 并回复微信。
- Web 注册要求完整 OTP 验证：阿里云验证码（一点即过）→ 短信 OTP → 一次性 verified_token → 注册；已在真实手机号上完整验收。

## 当前限制

- 已验证主路径是两个微信账号的私聊文本。
- Web onboarding 的注册（含 OTP 验证）、扫码绑定和首条真实消息路由已完成本机验收。
- 主动消息/提醒的最新推进基准见 `docs/tech_design/proactive_messaging_design.md`；当前机制代码已经基本闭环，包含 Gateway `send` 文本封装、真实微信主动发送 smoke test、`outbound_messages` ledger、主动发送每日上限、quiet hours、`reminders` 表、due reminder dispatcher、高确定性显式提醒识别、系统级 proactive scheduler、`proactive_account_state`、heartbeat draft/promote/send、hidden commitment 抽取和 due commitment dispatch。代码边界已整理：`/openclaw/turn` 业务逻辑在 `app.turn_service`，proactive 域代码在 `app/proactive/` package，旧顶层 proactive 兼容模块已删除。尚未完成真实微信端到端联调、架构梳理后的边界确认、周期性提醒、自然语言取消/更新提醒、用户级 timezone 或多实例 worker lease。
- 主动消息功能暂停继续扩展；下一阶段先整体梳理 AI4ALL 后端架构，再统一联调和调参。
- 语音仍属于 Phase 1 范围，但 ASR 尚未实现。
- 当前微信插件约束下不支持群聊 bot 模型。
- 暂不支持图片/多模态。
- 暂无 Web 管理后台，只有 Admin API。
- 暂无正式登录系统，Admin API 使用共享 token。
- 当前使用 SQLite，本地和早期测试够用；生产建议评估 PostgreSQL。
- 手机号格式仅支持大陆 11 位手机号（`1[3-9]XXXXXXXXX`），不支持其他地区格式。

## 当前运行假设

- OpenClaw 本地运行。
- AI4ALL Backend 本地默认可运行在 `http://127.0.0.1:8000`；当前 OpenClaw bridge 配置已指向 `8000`。
- Bridge 插件必须指向实际 Backend 地址；如果端口变化，Bridge 运行时配置也必须同步更新，否则会走“卡住了”的兜底回复。
- DeepSeek 通过 `.env` 配置。
- 本地数据库路径为 `data/ai4all.sqlite3`。
- 账号级 profile 路径为 `data/user_profiles/<account_id>/user_profile.md`，其中 `<account_id>` 为当前代码历史命名下的 AI4ALL 业务账号 ID。
