# 后台管理说明

当前已有一个轻量 Web 后台，也保留 Admin API。Admin 最高权限使用 `ADMIN_TOKEN`，普通后台用户使用 `ADMIN_STAFF_TOKEN`。

Web 后台入口：

```text
http://127.0.0.1:8000/ui/
```

Web onboarding 入口：

```text
http://127.0.0.1:8000/ui/onboarding.html
```

## Admin 鉴权

所有 `/admin/*` 和 `/debug/*` 接口都需要请求头：

```http
Authorization: Bearer <ADMIN_TOKEN>
```

本地 `.env` 中配置：

```bash
ADMIN_TOKEN=replace-with-a-strong-token
ADMIN_STAFF_TOKEN=replace-with-a-staff-token
```

默认开发值见仓库根目录 `.env.example`：

```bash
ADMIN_TOKEN=dev-admin-token
ADMIN_STAFF_TOKEN=
```

`ADMIN_TOKEN` 映射为 `role=admin`，可审批临时明文授权并直接调用明文接口；`ADMIN_STAFF_TOKEN` 映射为 `role=staff`，默认只能看脱敏视图，需要申请并获批临时明文授权后才可查看指定账号、指定资源的明文。

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
阿里云图形验证码 -> 短信 OTP -> 一次性 otp_token -> /web/register-and-binding-intent -> 二维码
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

## 主动提醒 Scheduler

开发期手动测试优先使用 Proactive Debug 后台：

```text
http://127.0.0.1:8000/ui/proactive_debug.html
```

该页面可以选择账号、开启 proactive state、模拟入站、让 pending reminder 到期、运行 scheduler、生成/提升/清理 account check draft，并在单账号 `Run Proactive Check` 后展示内容邀请是否生成及原因。

查看或更新某个账号的 proactive state：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/acct_example/proactive-state

curl -X PATCH \
  http://127.0.0.1:8000/admin/accounts/acct_example/proactive-state \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "next_scan_at": "2000-01-01 00:00:00",
    "metadata": {
      "source": "manual-test",
      "account_check_candidate": {
        "id": "manual-1",
        "text": "记得关注一下事情 B。",
        "source": "manual-test"
      }
    }
  }'
```

`next_scan_at` 和 `cooldown_until` 支持 ISO datetime 或 `YYYY-MM-DD HH:MM:SS`，会规范化为 `YYYY-MM-DD HH:MM:SS` 存入 SQLite。

手动触发隐藏 LLM 账号主动检查候选生成：

```bash
curl -X POST \
  http://127.0.0.1:8000/admin/accounts/acct_example/proactive-check-candidate-draft \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

这个接口只会把结果写入 `metadata.account_check_candidate_draft`。它不会写入会触发发送的 `metadata.account_check_candidate`，也不会主动发微信。若 LLM 置信度低于 `PROACTIVE_ACCOUNT_CHECK_MIN_CONFIDENCE`，会返回 no-op 并清理旧 draft。

人工确认 draft 可发送后，再把它提升为 active candidate：

```bash
curl -X POST \
  http://127.0.0.1:8000/admin/accounts/acct_example/proactive-check-candidate-draft/promote \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

promote 会把 `metadata.account_check_candidate_draft` 移到 `metadata.account_check_candidate`，并写入 `account_check_candidate_promoted_at`。只有提升后的 `account_check_candidate` 才会被 scheduler 的账号主动检查视为可发送候选。

如果 draft 不合适，可以直接清理：

```bash
curl -X DELETE \
  http://127.0.0.1:8000/admin/accounts/acct_example/proactive-check-candidate-draft \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

清理只删除 draft，不会删除已经存在的 active `account_check_candidate`。

手动触发单账号账号主动检查，并查看是否生成内容邀请候选：

```bash
curl -X POST \
  http://127.0.0.1:8000/admin/accounts/acct_example/proactive-check/run-once \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

返回体中的 `account_check` 包含本次候选发送决策和执行结果；`display.content_invitation_generated` 为 `true` 时会展示本次生成的内容邀请候选，为 `false` 时 `display.reason` 会说明未生成原因。

查看 scheduler 配置和运行状态：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/proactive/scheduler
```

查看 hidden extractor 写入的 follow-up commitments：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/acct_example/commitments
```

取消不应发送的 commitment：

```bash
curl -X POST \
  http://127.0.0.1:8000/admin/commitments/com_example/cancel \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

手动触发一次 proactive run。当前会依次处理 due reminders、due commitments，再扫描 due proactive accounts。账号主动检查会调用非 LLM 决策函数：没有候选事项时返回 `no_candidate` 并跳过发送；如果 state metadata 里显式放入 `account_check_candidate`，且 route、quiet hours、daily limit 等策略通过，会通过 outbound ledger / Gateway 发送微信消息。

```bash
curl -X POST \
  'http://127.0.0.1:8000/admin/proactive/scheduler/run-once?limit=20' \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

账号主动检查发送成功后，系统会写入 `last_proactive_sent_at`，并把 active `metadata.account_check_candidate` 移到 `metadata.account_check_last_sent_candidate`，避免同一个候选被下一轮重复发送。

阶段性验证账号主动检查主动触达的最短路径：

1. 确认目标账号已经有真实微信入站消息和 `channel_bindings` route。
2. `PATCH /admin/accounts/{account_id}/proactive-state`，设置 `enabled=true`、`next_scan_at` 为过去时间。
3. `POST /admin/accounts/{account_id}/proactive-check-candidate-draft` 生成隐藏 LLM draft。
4. `GET /admin/accounts/{account_id}/proactive-state` 检查 `metadata.account_check_candidate_draft`。
5. `POST /admin/accounts/{account_id}/proactive-check-candidate-draft/promote` 人工确认提升。
6. `POST /admin/accounts/{account_id}/proactive-check/run-once` 调试单账号；或 `POST /admin/proactive/scheduler/run-once?limit=20` 触发 worker 同款扫描。
7. 若未命中 quiet hours / daily limit / route 缺失，应在微信收到主动消息，并在 `outbound_messages` 看到 `sent`。

最终联调 hidden commitment 的路径：

1. 确认目标账号有真实微信入站消息和可用 `channel_bindings`。
2. 开启该账号 proactive state：`enabled=true`。
3. 通过微信进行一轮普通聊天，内容中包含明确的未来后续事项；显式“提醒我”仍会走 reminder，不会重复抽取 commitment。
4. `GET /admin/accounts/{account_id}/commitments` 检查是否写入 `pending` commitment。
5. 到期后调用 `POST /admin/proactive/scheduler/run-once?limit=20`，或启动独立 worker。
6. 若未命中 quiet hours / daily limit / route 缺失，应在微信收到 commitment 主动消息，并看到 `outbound_messages.source=commitment`。

本地或生产也可以用独立 worker 跑循环：

```bash
.venv/bin/python scripts/run_proactive_scheduler.py
```

默认 `PROACTIVE_SCHEDULER_ENABLED=false`，FastAPI 不会自动启动 in-process scheduler。若要在 FastAPI 内启动，必须保证 `uvicorn --workers 1`；否则多个 worker 会重复扫描 due reminder / due account。账号主动检查间隔由 `PROACTIVE_ACCOUNT_CHECK_INTERVAL_SECONDS` 控制。

## 本地 Debug API

`/debug/*` 接口仍保留给本地开发使用。它们同样需要 Admin/Staff Token，不应该暴露到公网。Debug 页面和 API 约定见 [调试指南](../debugging.md)。

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
