# AI4ALL Weixin Bot

## 文档

- [当前状态](docs/current_status.md)
- [用户使用说明](docs/user_guide.md)
- [后台管理说明](docs/admin_guide.md)
- [后续规划](docs/roadmap.md)
- [下一步开发步骤](docs/next_dev_steps.md)
- [OpenClaw Bridge 技术设计](docs/openclaw_bridge_tech_design.md)

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

Backend 按 `account_id + session_key` 隔离上下文。当前已验证一个 OpenClaw 实例同时接入两个个人微信账号，Bridge 会从 OpenClaw `sessionKey` 解析真实微信账号级 `account_id`，并为每个账号自动创建 `data/user_profiles/<account_id>/user_profile.md`。

## OpenClaw Bridge

Install or update the local Bridge plugin:

```bash
openclaw plugins install ./openclaw-bridge --force
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

## WeChat E2E Check

1. Start the backend on `127.0.0.1:8000`.
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
curl http://127.0.0.1:8000/debug/accounts/86f866663cf9-im-bot/user-profile
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
