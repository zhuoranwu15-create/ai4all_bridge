# AI4ALL Weixin Bot

AI4ALL Weixin Bot 面向普通用户提供微信里的个人 AI 陪伴与轻量助理服务。用户通过手机号验证和微信扫码接入微信 OpenClawBot 通道，后续在微信私聊中使用由 AI4ALL Backend 驱动的个人 AI bot。

产品第一定位是聊天陪伴，同时逐步补齐明确提醒、信息搜索、每日新闻、搞笑内容等轻量个人助理能力。OpenClaw / `openclaw-weixin` 在这里是微信通道基础设施，AI4ALL Backend 承担用户注册、账号隔离、Soul、记忆、提醒、模型调用和运营管理。

## 文档

- [文档导航](docs/README.md)
- [项目启动文档](start.md)
- [产品需求文档](docs/prd.md)
- [产品专题 PRD](docs/product/README.md)
- [当前状态](docs/current_status.md)
- [用户使用说明](docs/guides/user_guide.md)
- [后台管理说明](docs/guides/admin_guide.md)
- [后续规划](docs/roadmap.md)
- [总体架构 / 框架设计](docs/architecture_overview.md)
- [Phase 1 详细技术设计](docs/phase1_technical_design.md)
- [Phase 1 需求追踪矩阵](docs/phase1_traceability_matrix.md)
- [下一步开发步骤](docs/next_dev_steps.md)
- [OpenClaw Bridge 技术设计](docs/tech_design/openclaw_bridge_design.md)
- [主动消息与提醒设计](docs/tech_design/proactive_messaging_design.md)

## Local Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Web onboarding:

```text
http://127.0.0.1:8000/ui/onboarding.html
```

Registration now requires a mainland China mobile number, Aliyun Captcha, SMS
OTP verification, and a one-time `otp_token`. The current onboarding page calls
`/web/register-and-binding-intent` after OTP verification, so the backend creates
or reuses the platform user and default AI4ALL Account, then returns the WeChat
QR login intent directly. In `APP_ENV=local` or `APP_ENV=test`, empty Aliyun
SMS/Captcha credentials enable mock mode; in non-local environments, missing
Aliyun credentials fail closed.

For real SMS/Captcha, fill the `ALIYUN_*` block in `.env`. The current static
`onboarding.html` also contains the Aliyun Captcha `SceneId` and `prefix`; keep
those values aligned with the Aliyun console until frontend runtime config
injection is added.

Mock OpenClaw turn:

```bash
python scripts/send_mock_turn.py --text "你好"
```

Fixed message id for dedupe testing:

```bash
python scripts/send_mock_turn.py --text "你好" --message-id fixed-1
python scripts/send_mock_turn.py --text "你好" --message-id fixed-1
```

## LLM

The backend uses an OpenAI-compatible `/chat/completions` API when `LLM_API_KEY`
is set. If `LLM_API_KEY` is empty, it keeps the local mock fallback.

```bash
cp .env.example .env
# DeepSeek example:
# LLM_BASE_URL=https://api.deepseek.com
# LLM_MODEL=deepseek-chat
# LLM_API_KEY=...
```

## Storage

SQLite is used for the first version. The default database path is:

```text
data/ai4all.sqlite3
```

Backend 按 AI4ALL 业务账号隔离上下文。当前代码里的 `account_id` 是历史命名，语义上应理解为 `ai4all_account_id`；未绑定 legacy 入站可 fallback 为 OpenClaw `session_key`，Web onboarding 绑定完成后会路由到 Backend 预创建的 `acct_...`。不要把它等同于 OpenClaw payload 原生 `account_id`。身份与架构边界见 [总体架构 / 框架设计](docs/architecture_overview.md)、[Phase 1 详细技术设计](docs/phase1_technical_design.md) 和 [身份模型与微信绑定](docs/tech_design/identity_model_and_wechat_binding.md)。

## OpenClaw Bridge

Install or update the local Bridge plugin:

```bash
openclaw plugins install ./openclaw-bridge --force
openclaw gateway restart
```

Make sure the Bridge backend URL matches the FastAPI port you are using. The
default docs use `8000`; the 2026-05-20 Web onboarding verification ran on
`8012`:

```bash
openclaw config set plugins.entries.ai4all-openclaw-bridge.config.backendUrl http://127.0.0.1:8012
openclaw gateway restart
```

Check plugin and channel status:

```bash
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

Expected plugin output includes:

```text
Typed hooks:
before_agent_reply
```

For local orchestration comparison, enable shadow trace only for test accounts.
Those accounts will let OpenClaw run its native agent, rewrite the outbound
content back to AI4ALL's reply, and store the OpenClaw prompt trace in the
backend:

```json5
{
  plugins: {
    entries: {
      "ai4all-openclaw-bridge": {
        hooks: {
          allowConversationAccess: true
        },
        config: {
          shadowTraceAccountIds: "acct_example"
        }
      }
    }
  }
}
```

Set the backend side too:

```bash
DEBUG_TRACE_ACCOUNT_IDS=acct_example
```

## WeChat E2E Check

1. Start the backend on the same port configured in the Bridge (`8000` by default; `8012` in the current Web onboarding verification).
2. Install/update the Bridge plugin.
3. Restart OpenClaw Gateway.
4. Confirm `openclaw-weixin` is running.
5. Send a private WeChat message from the OpenClaw-connected WeChat account.
6. Confirm the WeChat reply comes from AI4ALL Backend.

## Debug APIs

These endpoints are intended for local development only.

```bash
curl http://127.0.0.1:8000/debug/sessions
curl 'http://127.0.0.1:8000/debug/messages?session_id=1'
curl 'http://127.0.0.1:8000/debug/messages/raw?limit=5'
curl 'http://127.0.0.1:8000/debug/traces?account_id=acct_example'
curl http://127.0.0.1:8000/debug/accounts/acct_example/user-profile
curl -X POST http://127.0.0.1:8000/debug/sessions/1/reset
curl http://127.0.0.1:8000/debug/sessions/1/profile
curl -X POST http://127.0.0.1:8000/debug/sessions/1/profile \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'
```

## Admin APIs

Admin endpoints require `Authorization: Bearer $ADMIN_TOKEN`.

```bash
export ADMIN_TOKEN=dev-admin-token

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/openclaw-weixin/contacts

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/contacts/1

curl -X PATCH http://127.0.0.1:8000/admin/contacts/1/profile \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'

curl -X POST http://127.0.0.1:8000/admin/contacts/1/disable \
  -H "Authorization: Bearer $ADMIN_TOKEN"

curl -X POST http://127.0.0.1:8000/admin/contacts/1/enable \
  -H "Authorization: Bearer $ADMIN_TOKEN"

curl -X POST http://127.0.0.1:8000/admin/sessions/1/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

The current `contacts` endpoints are compatibility APIs from the early data model.
The target model is account-level management: each connected WeChat account has
its own Soul, session history, memory, and configuration.

## Troubleshooting

Backend logs are printed by uvicorn.

OpenClaw logs:

```bash
tail -120 ~/.openclaw/logs/gateway.log
tail -120 ~/.openclaw/logs/gateway.err.log
tail -120 ~/.openclaw/tmp/openclaw-501/openclaw-$(date +%F).log
```

Common checks:

```bash
curl http://127.0.0.1:8000/health
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

If WeChat replies with the default OpenClaw assistant instead of AI4ALL, check:

- Bridge plugin is installed and enabled.
- `before_agent_reply` appears in runtime inspect.
- Backend URL in the Bridge environment/config points to the reachable backend.
- Backend logs show `POST /openclaw/turn`.
