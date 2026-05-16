# AI4ALL Bridge Current Status

Last updated: 2026-05-16

## Product Positioning

AI4ALL Bridge is a WeChat-facing personal AI companion and life assistant service.

The current direction is not a general productivity assistant and not a psychology/counseling product. It focuses on:

- Emotional companionship in everyday conversation.
- Lightweight life assistant scenarios.
- Lower-cost access to a personal AI experience through an OpenClaw-like architecture.
- One operated WeChat account serving multiple private chat users first.

## Completed

### End-to-End WeChat Flow

The real WeChat path has been verified:

```text
WeChat private message
-> openclaw-weixin
-> OpenClaw Gateway
-> ai4all-openclaw-bridge plugin
-> AI4ALL Backend
-> DeepSeek/OpenAI-compatible LLM
-> AI4ALL reply returned to WeChat
```

The project uses official OpenClaw and official `openclaw-weixin`; neither repository is forked.

### Bridge Plugin

Implemented `ai4all-openclaw-bridge`:

- Registers OpenClaw `before_agent_reply` hook.
- Forwards private text turns to AI4ALL Backend.
- Uses bearer auth with `AI4ALL_BRIDGE_SECRET`.
- Returns synthetic replies back through OpenClaw.
- Handles backend failures with a friendly fallback reply.

### Backend

Implemented FastAPI backend:

- `GET /health`
- `POST /openclaw/turn`
- SQLite storage.
- OpenAI-compatible LLM call.
- DeepSeek verified with real token/model.
- Mock fallback when `LLM_API_KEY` is not configured.

### Multi-User Data Isolation

The backend now separates users by:

```text
account_id + session_key
```

Current data model:

- `accounts`: WeChat/OpenClaw entry accounts.
- `contacts`: chat users under an account.
- `sessions`: conversation sessions.
- `profiles`: user-level style, prompt, preferences.
- `messages`: inbound/outbound message history, latency, errors.

### Admin Management

Implemented token-protected Admin API:

- Account listing.
- Contact listing/detail.
- Contact notes.
- Contact enable/disable.
- Profile updates.
- Session listing/detail.
- Session reset.

Disabled contacts are ignored by `/openclaw/turn` and do not trigger LLM calls.

### Observability

Implemented:

- Message persistence.
- Duplicate message handling by `message_id`.
- Turn latency metadata.
- Error recording on outbound messages.
- Debug APIs for local development.

## Verified

- WeChat connected to OpenClaw.
- WeChat message reaches AI4ALL Backend.
- AI4ALL Backend returns replies to WeChat.
- DeepSeek real model replies work.
- SQLite schema auto-creates and migrates current fields.
- `message_id` dedupe works.
- Admin API auth returns `401` without token.
- Contact disable blocks replies.
- Contact enable restores replies.

## Current Limitations

- Text private chat is the main verified path.
- Voice is still in Phase 1 scope, but ASR is not implemented yet.
- No group chat.
- No image/multimodal support.
- No Web Admin UI yet.
- No production-grade auth/login; Admin API uses a shared token.
- SQLite is used locally; PostgreSQL should be considered for production.
- Only one operated WeChat account has been verified, though the model supports multiple `account_id`s.

## Current Runtime Assumptions

- OpenClaw runs locally.
- AI4ALL Backend runs at `http://127.0.0.1:8000`.
- Bridge plugin points to this backend.
- DeepSeek is configured through `.env`.
- Local database is `data/ai4all.sqlite3`.
