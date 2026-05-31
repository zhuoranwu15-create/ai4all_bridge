# AI4ALL Weixin Bot — Claude Dev Notes

WeChat 个人 AI 伴侣。每个微信账号拥有完全隔离的 Soul、会话和记忆。NOT a customer-service model.

## Running

```bash
.venv/bin/uvicorn app.main:app --reload --port 8180
```

`http://localhost:8180` · DB: `data/ai4all.sqlite3`

## Auth

| Caller | Header |
|---|---|
| OpenClaw bridge | `Authorization: Bearer dev-secret` |
| Admin / debug | `Authorization: Bearer dev-admin-token` |

## Tests

```bash
.venv/bin/pytest tests/ -v
.venv/bin/pytest tests/test_turn_service.py -v   # single file
```

No server needed — tests use in-memory SQLite.

## Dev Scripts

```bash
# Inject a turn directly, bypassing OpenClaw; default account: "local"
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "你好"

# Inspect the assembled system prompt for an account
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account 86f866663cf9-im-bot
```

Both scripts default to port **8000** — always pass `--url` when testing locally.

Primary test account: `86f866663cf9-im-bot` (has the most history in `data/ai4all.sqlite3`).

Full debugging reference (all endpoints, onboarding UI, traces, dreaming, DB queries): [`docs/debugging.md`](docs/debugging.md)

## Module Map

| Module | Role |
|---|---|
| `turn_service.py` | Entry point for every incoming message |
| `prompt_builder.py` | Assembles LLM context from all sources |
| `user_profiles.py` | Per-account context files (SOUL / IDENTITY / USER) |
| `session_lifecycle.py` | Conversation session rotation |
| `onboarding.py` | New-user onboarding flow |
| `dreaming.py` + `dreaming_scheduler.py` | Proactive outbound — heartbeats, reminders, commitments |
| `memory_writer.py` | Post-turn memory updates |
| `rate_limiter.py` | Daily / RPM quota enforcement |
| `openclaw_gateway.py` | Outbound calls back to OpenClaw |

## Dev Conventions

**Per-account isolation is the core invariant.** Any DB query or file write not scoped by `account_id` is a bug.

**`contacts` table is legacy naming.** In the current 1-account-1-user model, `contacts.sender_id ≈ account_id`. Admin routes still use `/admin/contacts/` — known debt, don't add new APIs on this pattern.

**Proactive scheduler runs as a separate process.** `dreaming_scheduler.py` is not part of the FastAPI app. Run via `scripts/run_proactive_scheduler.py`, or set `PROACTIVE_SCHEDULER_ENABLED=true` only with a single worker.

**Canonical DB** is `data/ai4all.sqlite3`. Ignore empty `ai4all.db` files at root and in `data/`.

**All config vars** are documented with inline comments in `.env.example`.

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
<!-- SPECKIT END -->
