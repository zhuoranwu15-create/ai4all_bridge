# Reminder Debug UI Implementation Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A standalone debug page at `/ui/reminder_debug.html` that lets a developer manually test reminder creation via chat (LLM tool-use path) and directly inspect/edit/cancel reminders in the DB.

**Architecture:** Two files changed — a new static HTML page and 3 new routes added to `app/main.py`. No new Python modules. Follows the exact same pattern as `app/static/onboarding_debug.html`.

**Tech Stack:** Vanilla HTML/CSS/JS (no frameworks), FastAPI routes, existing `app/db.py` reminder functions.

---

## Layout

Two-column layout. Left column (300px fixed): account selector + chat interface. Right column (flex): reminder list for the selected account.

```
┌────────────────────────────────────────────────────────────┐
│  Reminder Debug Panel                            DEV ONLY  │
├─────────────────────┬──────────────────────────────────────┤
│ 账号选择             │  提醒列表                            │
│ [输入框 + 加载列表]  │  [刷新按钮]                         │
│                     │  ┌─────────────────────────────────┐ │
│ ─────────────────── │  │ pending  2026-06-01 10:00       │ │
│ 聊天区              │  │ 每天提醒我喝水  recur: daily     │ │
│ [消息气泡历史]       │  │                [编辑] [取消]    │ │
│                     │  ├─────────────────────────────────┤ │
│ [输入框] [发送]      │  │ sent     2026-06-05 09:00       │ │
│                     │  │ 记得开会                        │ │
│                     │  └─────────────────────────────────┘ │
└─────────────────────┴──────────────────────────────────────┘
```

## Backend API (3 new routes in `app/main.py`)

All routes require `Authorization: Bearer {admin_token}`.

### `GET /debug/reminders/{account_id}`

Returns all reminders for the account across all statuses, newest first.

```json
{
  "account_id": "sk-acc-foo",
  "reminders": [
    {
      "id": "rem-abc",
      "text": "每天提醒我喝水",
      "due_at": "2026-06-01 10:00:00",
      "status": "pending",
      "recur_rule": "daily",
      "sent_count": 0,
      "last_sent_at": null,
      "created_at": "2026-05-30 09:00:00"
    }
  ]
}
```

Implementation: calls `list_reminders_for_account(account_id=account_id, limit=100)` (no status filter — returns all).

### `PATCH /debug/reminders/{reminder_id}`

Updates `text`, `due_at`, and/or `recur_rule` on a pending reminder. Only works when `status == "pending"`.

Request body (all fields optional):
```json
{
  "text": "新内容",
  "due_at": "2026-06-02 09:00:00",
  "recur_rule": "weekly:1",
  "clear_recur_rule": false
}
```

Response:
```json
{ "status": "ok", "reminder": { ...updated reminder fields... } }
```

Returns 400 if the reminder is not in `pending` status. Calls `update_reminder()` from `app/db.py`.

### `DELETE /debug/reminders/{reminder_id}`

Cancels a pending reminder (sets `status = "cancelled"`).

Response:
```json
{ "status": "ok", "reminder": { ...cancelled reminder fields... } }
```

Returns 400 if the reminder is not in `pending` status. Calls `cancel_reminder()` from `app/db.py`.

## Frontend (`app/static/reminder_debug.html`)

Single file, ~400 lines, inline CSS + vanilla JS. Follows `onboarding_debug.html` conventions exactly.

### Token storage
Tokens stored in `localStorage` under keys `rdb_admin_token` and `rdb_bridge_token`. Displayed as masked inputs at the top of the page.

### Left column — account + chat

1. Text input for `account_id` (the `sk-` prefixed ai4all account ID). "加载" button fetches `/admin/accounts` to populate a `<select>` dropdown.
2. "加载账号" refreshes the dropdown.
3. Chat area: scrollable `<div>` of message bubbles (user = right, assistant = left).
4. Textarea + "发送" button: POSTs to `/openclaw/turn` with `Authorization: Bearer {bridge_token}`. On success, appends both messages to chat and calls `refreshReminders()`.
5. The turn payload derives `session_key`, `channel_account_id`, `sender_id`, `chat_id` from the selected account_id by stripping the `sk-` prefix (same convention as other tests).

### Right column — reminder list

1. "刷新" button calls `GET /debug/reminders/{account_id}` and re-renders the list.
2. Each reminder card shows: status badge (colored), `due_at`, `text`, `recur_rule` (if set), `sent_count`.
3. **`pending` reminders** show two buttons:
   - **编辑**: expands an inline edit form with inputs for `text`, `due_at`, `recur_rule` and a "清除循环" checkbox. "保存" calls `PATCH /debug/reminders/{id}` then refreshes.
   - **取消**: confirms with `confirm()` then calls `DELETE /debug/reminders/{id}` then refreshes.
4. **Other statuses** (`sent`, `failed`, `cancelled`, `sending`): card is read-only; shows `error` field if present.
5. Empty state: "暂无提醒" placeholder.

### Status badge colors
- `pending` → blue
- `sending` → orange
- `sent` → green
- `failed` → red
- `cancelled` → gray

### Auto-refresh
After every successful chat send, `refreshReminders()` is called automatically. No polling.

## File Changes

| File | Action |
|------|--------|
| `app/static/reminder_debug.html` | Create (~400 lines) |
| `app/main.py` | Add 3 routes: GET/PATCH/DELETE under `/debug/reminders/` |

## Not in scope

- Manual dispatch trigger (可以后续加)
- Pagination (limit=100 is enough for debug)
- Recurring recur_rule validation in the UI (backend already validates)
