# AI4ALL Weixin Bot — Dev Notes for Claude

## Server

Default port is **8180** (not 8000).

Start the backend:
```bash
uvicorn app.main:app --reload --port 8180
```

Base URL for local testing: `http://localhost:8180`

## Auth Headers

- OpenClaw bridge turns: `Authorization: Bearer dev-secret`
- Admin / debug endpoints: `Authorization: Bearer dev-admin-token`

## Known Test Accounts

From `data/ai4all.sqlite3` (as of 2026-05-29):

| account_id | display_name | messages |
|---|---|---|
| `86f866663cf9-im-bot` | — | 32 (most history) |
| `local` | — | 24 |
| `acct_24b39e32656f4705a3cd57553d185b2a` | test1 | 22 |

Use `86f866663cf9-im-bot` as the primary test account.

## Onboarding Debug UI

Open in browser: **http://localhost:8180/ui/onboarding_debug.html**

Features: create test user, chat simulation, reset, jump to any step, live state + prompt preview.

## Onboarding Debug API Endpoints

```bash
# View state
curl -s http://localhost:8180/debug/accounts/{account_id}/onboarding \
  -H "Authorization: Bearer dev-admin-token" | jq .

# Reset to pending (wipes SOUL/IDENTITY/USER files)
curl -s -X POST http://localhost:8180/debug/accounts/{account_id}/onboarding/reset \
  -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"clear_context_files": true}' | jq .

# Jump to a specific step
curl -s -X PATCH http://localhost:8180/debug/accounts/{account_id}/onboarding/state \
  -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"state": "step2_sent"}' | jq .
```

Valid states: `pending` / `step1_sent` / `step2_sent` / `step3_sent` / `complete` / `timed_out`

## Database

Path: `data/ai4all.sqlite3`

Quick inspect:
```bash
sqlite3 data/ai4all.sqlite3 "SELECT id, display_name FROM accounts;"
```
