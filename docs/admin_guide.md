# 后台管理说明

当前已有一个轻量 Web 后台，也保留 Admin API。两者都通过 `ADMIN_TOKEN` 鉴权。

Web 后台入口：

```text
http://127.0.0.1:8000/ui/
```

Web onboarding 入口：

```text
http://127.0.0.1:8000/ui/onboarding.html
```

## Admin 鉴权

所有 `/admin/*` 接口都需要请求头：

```http
Authorization: Bearer <ADMIN_TOKEN>
```

本地 `.env` 中配置：

```bash
ADMIN_TOKEN=replace-with-a-strong-token
```

## 当前管理模型

新的目标模型以 AI4ALL 业务账号为核心：

```text
AI4ALL Account = 业务隔离账号，未绑定 legacy 入站可由 OpenClaw session_key fallback，绑定后使用预创建 acct_...
Channel Account = OpenClaw / 微信通道侧账号或机器人账号
Channel Binding = AI4ALL Account 与 Channel Account / session_key 的绑定
session = AI4ALL Account 下的会话
profile = 该账号或会话对应的 Soul / 风格 / Prompt 配置
message = 收发消息记录
```

现有 DB/API 里仍有 `account_id` 旧命名。当前语义上应理解为 AI4ALL Account ID，不等同于 OpenClaw payload 原生 `account_id`。

## 注册与验证码配置

Web onboarding 的注册链路现在要求：

```text
阿里云图形验证码 -> 短信 OTP -> 一次性 otp_token -> /web/register
```

本地开发时，`APP_ENV=local` 且 Aliyun SMS/Captcha 配置为空会进入 mock 模式，OTP 会输出到 backend 日志。非 local/test 环境缺少 Aliyun 凭据会直接失败，不会静默跳过验证码或短信发送。

需要在 `.env` 中配置：

```bash
ALIYUN_ACCESS_KEY_ID=
ALIYUN_ACCESS_KEY_SECRET=
ALIYUN_SMS_SIGN_NAME=
ALIYUN_SMS_TEMPLATE_CODE=
ALIYUN_SMS_MAX_PER_PHONE_PER_HOUR=5
ALIYUN_CAPTCHA_SCENE_ID=
ALIYUN_CAPTCHA_PREFIX=
OTP_EXPIRES_MINUTES=10
OTP_TOKEN_EXPIRES_MINUTES=10
```

当前静态 `onboarding.html` 中也存在 Aliyun Captcha 的 `SceneId` / `prefix`，上线或切换环境时需要与控制台配置保持一致；后续应改为由后端或构建流程注入。

## 查看账号

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts
```

这里返回的是 AI4ALL Account。账号详情接口会包含 `channel_bindings`，用于查看对应的 Channel Account ID、session key、sender/chat 等通道身份。

## 查看兼容用户记录

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/openclaw-weixin/contacts
```

这个接口暂时保留用于查看现有数据。它不代表最终产品里的核心管理对象。

## 查看兼容用户详情

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/contacts/1
```

## 修改备注或状态

```bash
curl -X PATCH http://127.0.0.1:8000/admin/contacts/1 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"notes":"内部测试账号"}'
```

当前支持的状态：

- `active`
- `disabled`

## 禁用或启用

禁用：

```bash
curl -X POST http://127.0.0.1:8000/admin/contacts/1/disable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

启用：

```bash
curl -X POST http://127.0.0.1:8000/admin/contacts/1/enable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

被禁用的记录再次发消息时，Backend 会返回 `status=disabled` 和 `no_reply=true`，不会调用 LLM。

## 修改 Profile

```bash
curl -X PATCH http://127.0.0.1:8000/admin/contacts/1/profile \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'
```

当前支持字段：

- `display_name`
- `style`
- `system_prompt`
- `preferences`

Profile 当前会影响 LLM Prompt。后续会调整为更明确的账号级 Soul / Prompt 配置。

## 查看会话

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/sessions
```

## 查看会话详情

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/sessions/1
```

返回内容包括：

- 会话信息。
- Profile。
- 最近消息。

## 重置会话

```bash
curl -X POST http://127.0.0.1:8000/admin/sessions/1/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

这会删除该 session 下已保存的消息。

## 本地 Debug API

`/debug/*` 接口仍保留给本地开发使用。它们当前不使用 `ADMIN_TOKEN`，不应该暴露到公网。

## 查看 raw payload

用于排查 OpenClaw 传入的原始上下文字段：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  'http://127.0.0.1:8000/admin/messages/raw?limit=5'

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/messages/27/raw
```

## 查看账号级 user_profile.md

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/acct_example/user-profile
```

文件路径：

```text
data/user_profiles/<account_id>/user_profile.md
```

这里的 `<account_id>` 是当前代码历史命名，语义上是 AI4ALL Account ID。

## 常用排查命令

```bash
curl http://127.0.0.1:8000/health
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

Backend 日志由 uvicorn 输出。

OpenClaw 日志：

```bash
tail -120 ~/.openclaw/logs/gateway.log
tail -120 ~/.openclaw/logs/gateway.err.log
tail -120 ~/.openclaw/tmp/openclaw-501/openclaw-$(date +%F).log
```
