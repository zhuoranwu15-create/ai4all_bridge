# Admin Guide

This guide covers the current token-based admin interface.

## Admin Auth

Admin APIs require:

```http
Authorization: Bearer <ADMIN_TOKEN>
```

Set the token in `.env`:

```bash
ADMIN_TOKEN=replace-with-a-strong-token
```

## List Accounts

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts
```

Accounts represent WeChat/OpenClaw entry accounts.

## List Contacts Under An Account

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/openclaw-weixin/contacts
```

Contacts represent chat users under one account.

## View Contact

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/contacts/1
```

## Update Contact Notes Or Status

```bash
curl -X PATCH http://127.0.0.1:8000/admin/contacts/1 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"notes":"internal test user"}'
```

Allowed status values:

- `active`
- `disabled`

## Disable Or Enable A Contact

```bash
curl -X POST http://127.0.0.1:8000/admin/contacts/1/disable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

```bash
curl -X POST http://127.0.0.1:8000/admin/contacts/1/enable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Disabled contacts are ignored by `/openclaw/turn`; no LLM call is made.

## Update User Profile

```bash
curl -X PATCH http://127.0.0.1:8000/admin/contacts/1/profile \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'
```

Supported fields:

- `display_name`
- `style`
- `system_prompt`
- `preferences`

Profile priority currently affects the LLM prompt at user/contact level.

## List Sessions

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/sessions
```

## View Session Detail

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/sessions/1
```

Returns:

- session metadata
- profile
- recent messages

## Reset Session

```bash
curl -X POST http://127.0.0.1:8000/admin/sessions/1/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

This deletes stored messages for the session.

## Local Debug APIs

`/debug/*` endpoints remain available for local development. They do not use `ADMIN_TOKEN` yet and should not be exposed publicly.

## Operational Checks

```bash
curl http://127.0.0.1:8000/health
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

Backend logs are printed by uvicorn.

OpenClaw logs:

```bash
tail -120 ~/.openclaw/logs/gateway.log
tail -120 ~/.openclaw/logs/gateway.err.log
tail -120 ~/.openclaw/tmp/openclaw-501/openclaw-$(date +%F).log
```
