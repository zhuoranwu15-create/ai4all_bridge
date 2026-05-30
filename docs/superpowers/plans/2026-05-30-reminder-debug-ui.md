# Reminder Debug UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone debug page at `/ui/reminder_debug.html` that lets a developer test reminder creation via chat (LLM tool-use path) on the left and inspect/edit/cancel reminders in the DB on the right.

**Architecture:** Two files changed — 3 new admin routes added to `app/main.py`, and a new static HTML page `app/static/reminder_debug.html`. No new Python modules. Follows the exact same conventions as `app/static/onboarding_debug.html`.

**Tech Stack:** Vanilla HTML/CSS/JS, FastAPI routes, existing `app/db.py` reminder functions (`get_reminder`, `cancel_reminder`, `update_reminder`, `list_reminders_for_account`).

---

## File Map

| File | Action | Why |
|------|--------|-----|
| `app/main.py` | Modify — add 4 imports + 3 routes | Backend API for the debug page |
| `app/static/reminder_debug.html` | Create (~430 lines) | The debug UI |
| `tests/test_reminder_debug_routes.py` | Create | TDD for the 3 backend routes |

---

## Task 1: Backend routes

**Files:**
- Modify: `app/main.py` (lines 15–83 for imports; line 1025 for route insertion)
- Create: `tests/test_reminder_debug_routes.py`

### Background — existing patterns you must follow

`app/main.py` uses `Depends(verify_admin_auth)` for all `/debug/*` routes. Tests use `"Authorization": "Bearer test-admin"` (the `test_settings` fixture sets `admin_token = "test-admin"`). The `client` fixture (in `tests/conftest.py`) depends on `fresh_db`, which already patches `app.db.settings`, so tests can call db functions directly without an extra `patch`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reminder_debug_routes.py`:

```python
from app.db import cancel_reminder, create_reminder, get_or_create_session

ADMIN_HEADERS = {"Authorization": "Bearer test-admin"}


def _setup_account(account_id: str) -> None:
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="c",
        session_key=f"sk-{account_id}",
    )


def _create_reminder(account_id: str, reminder_id: str) -> dict:
    return create_reminder(
        reminder_id=reminder_id,
        account_id=account_id,
        channel="openclaw-weixin",
        channel_account_id="bot-1",
        to_user_id="user@wechat",
        session_key=f"sk-{account_id}",
        text="喝水提醒",
        due_at="2026-06-01 10:00:00",
    )


def test_debug_get_reminders_empty(client, fresh_db):
    _setup_account("rdb-empty")
    res = client.get("/debug/reminders/rdb-empty", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["account_id"] == "rdb-empty"
    assert data["reminders"] == []


def test_debug_get_reminders_returns_multiple_statuses(client, fresh_db):
    _setup_account("rdb-list")
    _create_reminder("rdb-list", "rem-p")
    _create_reminder("rdb-list", "rem-c")
    cancel_reminder(reminder_id="rem-c")

    res = client.get("/debug/reminders/rdb-list", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    ids = {r["id"] for r in res.json()["reminders"]}
    assert ids == {"rem-p", "rem-c"}


def test_debug_patch_reminder_updates_fields(client, fresh_db):
    _setup_account("rdb-patch")
    _create_reminder("rdb-patch", "rem-patch")

    res = client.patch(
        "/debug/reminders/rem-patch",
        headers=ADMIN_HEADERS,
        json={"text": "新内容", "due_at": "2026-07-01 08:00:00"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["reminder"]["text"] == "新内容"
    assert data["reminder"]["due_at"] == "2026-07-01 08:00:00"


def test_debug_patch_reminder_rejects_non_pending(client, fresh_db):
    _setup_account("rdb-patch-bad")
    _create_reminder("rdb-patch-bad", "rem-bad")
    cancel_reminder(reminder_id="rem-bad")

    res = client.patch(
        "/debug/reminders/rem-bad",
        headers=ADMIN_HEADERS,
        json={"text": "新内容"},
    )
    assert res.status_code == 400


def test_debug_delete_reminder_cancels_pending(client, fresh_db):
    _setup_account("rdb-del")
    _create_reminder("rdb-del", "rem-del")

    res = client.delete("/debug/reminders/rem-del", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["reminder"]["status"] == "cancelled"


def test_debug_delete_reminder_rejects_non_pending(client, fresh_db):
    _setup_account("rdb-del-bad")
    _create_reminder("rdb-del-bad", "rem-del-bad")
    cancel_reminder(reminder_id="rem-del-bad")

    res = client.delete("/debug/reminders/rem-del-bad", headers=ADMIN_HEADERS)
    assert res.status_code == 400


def test_debug_routes_require_admin_auth(client, fresh_db):
    res = client.get("/debug/reminders/some-account")
    assert res.status_code == 401
    res = client.patch("/debug/reminders/some-id", json={})
    assert res.status_code == 401
    res = client.delete("/debug/reminders/some-id")
    assert res.status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_reminder_debug_routes.py -v
```

Expected: all 7 tests FAIL with `404 Not Found` (routes don't exist yet).

- [ ] **Step 3: Add 4 imports to app/main.py**

In `app/main.py`, the `from app.db import (...)` block spans lines 15–83. Add four new names inside that block (order doesn't matter, add after line 82 `upsert_admin_user,`):

```python
    # add these four lines before the closing )
    cancel_reminder,
    get_reminder,
    list_reminders_for_account,
    update_reminder,
```

So the end of the block looks like:

```python
    upsert_proactive_account_state,
    upsert_channel_binding,
    upsert_admin_user,
    cancel_reminder,
    get_reminder,
    list_reminders_for_account,
    update_reminder,
)
```

- [ ] **Step 4: Add Pydantic model + 3 routes to app/main.py**

Insert after line 1025 (after the `debug_update_profile` function, before the `# Web onboarding (MVP)` comment):

```python
# ---------------------------------------------------------------------------
# Reminder debug routes
# ---------------------------------------------------------------------------

class ReminderDebugUpdateRequest(BaseModel):
    text: Optional[str] = None
    due_at: Optional[str] = None
    recur_rule: Optional[str] = None
    clear_recur_rule: bool = False


@app.get("/debug/reminders/{account_id}")
def debug_get_reminders(account_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    reminders = list_reminders_for_account(account_id=account_id, limit=100)
    return {"account_id": account_id, "reminders": reminders}


@app.patch("/debug/reminders/{reminder_id}")
def debug_patch_reminder(
    reminder_id: str,
    payload: ReminderDebugUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    reminder = get_reminder(reminder_id=reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if reminder["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending reminders can be edited")
    updated = update_reminder(
        reminder_id=reminder_id,
        text=payload.text,
        due_at=payload.due_at,
        recur_rule=payload.recur_rule,
        clear_recur_rule=payload.clear_recur_rule,
    )
    return {"status": "ok", "reminder": updated}


@app.delete("/debug/reminders/{reminder_id}")
def debug_delete_reminder(reminder_id: str, _: None = Depends(verify_admin_auth)) -> dict:
    reminder = get_reminder(reminder_id=reminder_id)
    if reminder is None:
        raise HTTPException(status_code=404, detail="reminder not found")
    if reminder["status"] != "pending":
        raise HTTPException(status_code=400, detail="only pending reminders can be cancelled")
    cancelled = cancel_reminder(reminder_id=reminder_id)
    return {"status": "ok", "reminder": cancelled}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_reminder_debug_routes.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
python -m pytest tests/ --ignore=tests/test_web_onboarding.py -q
```

Expected: all tests pass (pre-existing 75 errors in `test_web_onboarding.py` are excluded; they were broken before this work).

- [ ] **Step 7: Commit**

```bash
git add tests/test_reminder_debug_routes.py app/main.py
git commit -m "feat: add reminder debug routes GET/PATCH/DELETE /debug/reminders"
```

---

## Task 2: Frontend page

**Files:**
- Create: `app/static/reminder_debug.html`

The page is served at `http://localhost:8180/ui/reminder_debug.html` via the existing `app.mount("/ui", StaticFiles(...))` in `app/main.py` (line 140) — no server change needed.

Tokens are stored in `localStorage` under keys `rdb_admin_token` and `rdb_bridge_token`. On page load, if they're missing, a warning is printed to the browser console with instructions.

- [ ] **Step 1: Create app/static/reminder_debug.html**

```html
<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <title>Reminder Debug</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f5; color: #222; font-size: 14px; }

    header { background: #1a1a2e; color: #fff; padding: 12px 20px; display: flex; align-items: center; gap: 12px; }
    header h1 { font-size: 16px; font-weight: 600; }
    header .badge { background: #e74c3c; color: #fff; font-size: 11px; padding: 2px 7px; border-radius: 10px; }

    .layout { display: grid; grid-template-columns: 320px 1fr; height: calc(100vh - 45px); overflow: hidden; }

    /* LEFT COLUMN */
    .left { display: flex; flex-direction: column; background: #fff; border-right: 1px solid #e0e0e0; overflow: hidden; }

    .account-section { padding: 10px 14px; border-bottom: 1px solid #f0f0f0; flex-shrink: 0; }
    .account-section .row { display: flex; gap: 6px; align-items: center; }
    .account-section select { flex: 1; border: 1px solid #ddd; border-radius: 6px; padding: 6px 8px; font-size: 13px; background: #fff; }
    .account-section select:focus { outline: none; border-color: #4a90e2; }

    .btn { display: inline-flex; align-items: center; justify-content: center; padding: 6px 12px; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; font-weight: 500; }
    .btn-primary { background: #4a90e2; color: #fff; }
    .btn-primary:hover { background: #357abd; }
    .btn-danger { background: #e74c3c; color: #fff; }
    .btn-danger:hover { background: #c0392b; }
    .btn-outline { background: #fff; color: #444; border: 1px solid #ddd; }
    .btn-outline:hover { background: #f5f5f5; }
    .btn-sm { padding: 4px 10px; font-size: 12px; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }

    .chat-body { flex: 1; overflow-y: auto; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
    .chat-empty { color: #aaa; font-size: 13px; text-align: center; padding: 20px 0; }

    .msg { max-width: 85%; }
    .msg.user { align-self: flex-end; }
    .msg.ai { align-self: flex-start; }
    .msg .bubble { padding: 9px 13px; border-radius: 14px; font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
    .msg.user .bubble { background: #4a90e2; color: #fff; border-bottom-right-radius: 4px; }
    .msg.ai .bubble { background: #f0f0f0; border-bottom-left-radius: 4px; }
    .msg.system { align-self: center; }
    .msg.system .bubble { background: #e8f5e9; color: #388e3c; font-size: 12px; padding: 5px 10px; border-radius: 10px; }

    .typing-indicator { align-self: flex-start; padding: 10px 14px; background: #f0f0f0; border-radius: 14px; border-bottom-left-radius: 4px; }
    .typing-indicator span { display: inline-block; width: 6px; height: 6px; background: #aaa; border-radius: 50%; margin: 0 2px; animation: bounce 1.2s infinite; }
    .typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
    .typing-indicator span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes bounce { 0%, 80%, 100% { transform: translateY(0); } 40% { transform: translateY(-6px); } }

    .chat-input-section { padding: 10px 14px; border-top: 1px solid #f0f0f0; flex-shrink: 0; display: flex; gap: 8px; align-items: flex-end; }
    .chat-input-section textarea { flex: 1; border: 1px solid #ddd; border-radius: 8px; padding: 8px 12px; font-size: 13px; resize: none; font-family: inherit; line-height: 1.4; max-height: 100px; }
    .chat-input-section textarea:focus { outline: none; border-color: #4a90e2; }

    /* RIGHT COLUMN */
    .right { display: flex; flex-direction: column; overflow: hidden; }

    .right-header { padding: 12px 20px; background: #fff; border-bottom: 1px solid #e0e0e0; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }
    .right-header h2 { font-size: 14px; font-weight: 600; color: #333; }
    .right-header .sub { font-size: 12px; color: #999; margin-top: 2px; }

    .reminder-list { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 12px; }
    .reminder-empty { color: #aaa; font-size: 13px; text-align: center; padding: 40px 0; }

    .reminder-card { background: #fff; border: 1px solid #e0e0e0; border-radius: 10px; padding: 14px 16px; }
    .reminder-card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
    .reminder-card-meta { display: flex; flex-direction: column; gap: 3px; }
    .reminder-card-due { font-size: 13px; font-weight: 600; color: #333; }
    .reminder-card-id { font-size: 11px; color: #bbb; font-family: monospace; }
    .reminder-card-actions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
    .reminder-card-text { font-size: 14px; color: #222; margin-bottom: 6px; line-height: 1.5; }
    .reminder-card-info { display: flex; gap: 10px; flex-wrap: wrap; }
    .reminder-card-info span { font-size: 12px; color: #888; }

    .status-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .status-pending  { background: #dbeafe; color: #1d4ed8; }
    .status-sending  { background: #ffedd5; color: #c2410c; }
    .status-sent     { background: #dcfce7; color: #15803d; }
    .status-failed   { background: #fee2e2; color: #b91c1c; }
    .status-cancelled{ background: #f3f4f6; color: #6b7280; }

    .edit-form { margin-top: 12px; border-top: 1px solid #f0f0f0; padding-top: 12px; display: flex; flex-direction: column; gap: 8px; }
    .edit-form label { font-size: 12px; color: #666; display: flex; flex-direction: column; gap: 4px; }
    .edit-form input[type="text"] { border: 1px solid #ddd; border-radius: 6px; padding: 6px 10px; font-size: 13px; font-family: inherit; }
    .edit-form input[type="text"]:focus { outline: none; border-color: #4a90e2; }
    .edit-form .checkbox-row { flex-direction: row; align-items: center; gap: 8px; }
    .edit-form .form-actions { display: flex; gap: 8px; }

    .toast { position: fixed; bottom: 20px; right: 20px; background: #333; color: #fff; padding: 10px 16px; border-radius: 8px; font-size: 13px; z-index: 999; display: none; }
    .toast.show { display: block; animation: fadeInOut 3s forwards; }
    @keyframes fadeInOut { 0%{opacity:0} 10%{opacity:1} 80%{opacity:1} 100%{opacity:0} }
  </style>
</head>
<body>

<header>
  <h1>Reminder Debug Panel</h1>
  <span class="badge">DEV ONLY</span>
</header>

<div class="layout">

  <!-- LEFT COLUMN: account selector + chat -->
  <div class="left">
    <div class="account-section">
      <div class="row">
        <select id="accountSelect" onchange="onAccountChange()">
          <option value="">— 选择账号 —</option>
        </select>
        <button class="btn btn-outline btn-sm" onclick="loadAccounts()">刷新</button>
      </div>
    </div>
    <div class="chat-body" id="chatBody">
      <div class="chat-empty">选择账号后发消息测试提醒创建</div>
    </div>
    <div class="chat-input-section">
      <textarea id="chatInput" placeholder="输入消息，Enter 发送，Shift+Enter 换行" rows="2" onkeydown="handleKey(event)"></textarea>
      <button class="btn btn-primary btn-sm" onclick="sendMessage()">发送</button>
    </div>
  </div>

  <!-- RIGHT COLUMN: reminder list -->
  <div class="right">
    <div class="right-header">
      <div>
        <h2>提醒列表</h2>
        <div class="sub" id="rightSubtitle">请先选择账号</div>
      </div>
      <button class="btn btn-outline btn-sm" onclick="refreshReminders()">刷新</button>
    </div>
    <div class="reminder-list" id="reminderList">
      <div class="reminder-empty">暂无提醒</div>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
// Tokens are stored in localStorage.
// To set them, paste in browser console:
//   localStorage.setItem("rdb_admin_token", "dev-admin-token");
//   localStorage.setItem("rdb_bridge_token", "dev-secret");
//   location.reload();
const ADMIN_TOKEN  = localStorage.getItem('rdb_admin_token')  || '';
const BRIDGE_TOKEN = localStorage.getItem('rdb_bridge_token') || '';
if (!ADMIN_TOKEN || !BRIDGE_TOKEN) {
  console.warn(
    '[reminder_debug] tokens not set.\n' +
    'localStorage.setItem("rdb_admin_token", "dev-admin-token");\n' +
    'localStorage.setItem("rdb_bridge_token", "dev-secret");\n' +
    'location.reload();'
  );
}

let currentAccountId = null;
let msgCounter = 0;

// ─── API helpers ──────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: {
      'Authorization': `Bearer ${ADMIN_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

async function sendTurn(accountId, text) {
  const res = await fetch('/openclaw/turn', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${BRIDGE_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      account_id: accountId,
      session_key: accountId,
      sender_id: 'debug-sender',
      chat_type: 'private',
      message_type: 'text',
      message_id: `debug-${Date.now()}-${++msgCounter}`,
      text,
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ─── Account list ─────────────────────────────────────────────────────────────

async function loadAccounts() {
  try {
    const data = await api('GET', '/admin/accounts');
    const sel  = document.getElementById('accountSelect');
    const prev = sel.value;
    const accounts = data.accounts || [];
    sel.innerHTML = '<option value="">— 选择账号 —</option>' +
      accounts.map(a =>
        `<option value="${escHtml(a.id)}"${a.id === prev ? ' selected' : ''}>${escHtml(a.display_name || a.id)}</option>`
      ).join('');
  } catch (e) {
    showToast(`加载账号失败: ${e.message}`);
  }
}

function onAccountChange() {
  const id = document.getElementById('accountSelect').value;
  currentAccountId = id || null;
  document.getElementById('chatBody').innerHTML =
    '<div class="chat-empty">发消息测试提醒创建</div>';
  document.getElementById('rightSubtitle').textContent = id || '请先选择账号';
  if (id) refreshReminders();
}

// ─── Reminder list ────────────────────────────────────────────────────────────

async function refreshReminders() {
  if (!currentAccountId) return;
  try {
    const data = await api('GET', `/debug/reminders/${currentAccountId}`);
    renderReminders(data.reminders || []);
  } catch (e) {
    showToast(`加载提醒失败: ${e.message}`);
  }
}

function renderReminders(reminders) {
  const el = document.getElementById('reminderList');
  if (reminders.length === 0) {
    el.innerHTML = '<div class="reminder-empty">暂无提醒</div>';
    return;
  }
  el.innerHTML = reminders.map(r => {
    const isPending = r.status === 'pending';
    const actions = isPending
      ? `<button class="btn btn-outline btn-sm" onclick="toggleEdit('${escHtml(r.id)}')">编辑</button>
         <button class="btn btn-danger  btn-sm" onclick="doCancel('${escHtml(r.id)}')">取消</button>`
      : `<span class="status-badge status-${escHtml(r.status)}">${escHtml(r.status)}</span>`;

    const infoItems = [
      r.recur_rule  ? `🔁 ${escHtml(r.recur_rule)}` : '',
      r.sent_count  ? `已发 ${r.sent_count} 次` : '',
      r.error       ? `❌ ${escHtml(r.error)}` : '',
    ].filter(Boolean);

    const editHtml = isPending ? `
      <div class="edit-form" id="edit-${escHtml(r.id)}" style="display:none">
        <label>内容
          <input type="text" id="et-${escHtml(r.id)}" value="${escHtml(r.text)}" />
        </label>
        <label>时间 (YYYY-MM-DD HH:MM:SS)
          <input type="text" id="ed-${escHtml(r.id)}" value="${escHtml(r.due_at || '')}" />
        </label>
        <label>循环规则 (daily / weekly:N / monthly:D)
          <input type="text" id="er-${escHtml(r.id)}" value="${escHtml(r.recur_rule || '')}" />
        </label>
        <label class="checkbox-row">
          <input type="checkbox" id="ec-${escHtml(r.id)}" />
          清除循环规则
        </label>
        <div class="form-actions">
          <button class="btn btn-primary btn-sm" onclick="doSaveEdit('${escHtml(r.id)}')">保存</button>
          <button class="btn btn-outline btn-sm" onclick="toggleEdit('${escHtml(r.id)}')">关闭</button>
        </div>
      </div>` : '';

    return `
      <div class="reminder-card" id="card-${escHtml(r.id)}">
        <div class="reminder-card-header">
          <div class="reminder-card-meta">
            <div class="reminder-card-due">${escHtml(r.due_at || '—')}</div>
            <div class="reminder-card-id">${escHtml(r.id)}</div>
          </div>
          <div class="reminder-card-actions">${actions}</div>
        </div>
        <div class="reminder-card-text">${escHtml(r.text)}</div>
        ${infoItems.length ? `<div class="reminder-card-info">${infoItems.map(i => `<span>${i}</span>`).join('')}</div>` : ''}
        ${editHtml}
      </div>`;
  }).join('');
}

function toggleEdit(id) {
  const el = document.getElementById(`edit-${id}`);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function doSaveEdit(id) {
  const text       = document.getElementById(`et-${id}`).value.trim();
  const due_at     = document.getElementById(`ed-${id}`).value.trim();
  const recur_rule = document.getElementById(`er-${id}`).value.trim();
  const clear      = document.getElementById(`ec-${id}`).checked;
  const body = {};
  if (text)       body.text       = text;
  if (due_at)     body.due_at     = due_at;
  if (recur_rule) body.recur_rule = recur_rule;
  if (clear)      body.clear_recur_rule = true;
  try {
    await api('PATCH', `/debug/reminders/${id}`, body);
    showToast('已保存');
    refreshReminders();
  } catch (e) {
    showToast(`保存失败: ${e.message}`);
  }
}

async function doCancel(id) {
  if (!confirm(`确认取消提醒 ${id}？`)) return;
  try {
    await api('DELETE', `/debug/reminders/${id}`);
    showToast('已取消');
    refreshReminders();
  } catch (e) {
    showToast(`取消失败: ${e.message}`);
  }
}

// ─── Chat ─────────────────────────────────────────────────────────────────────

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

async function sendMessage() {
  const input = document.getElementById('chatInput');
  const text  = input.value.trim();
  if (!text || !currentAccountId) return;
  input.value = '';

  appendMsg('user', text);
  const typing = appendTyping();

  try {
    const res = await sendTurn(currentAccountId, text);
    removeTyping(typing);
    appendMsg('ai', res.reply || '（无回复）');
    await refreshReminders();
  } catch (e) {
    removeTyping(typing);
    appendMsg('system', `❌ ${e.message}`);
  }
}

function appendMsg(role, text) {
  const body  = document.getElementById('chatBody');
  const empty = body.querySelector('.chat-empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.innerHTML = `<div class="bubble">${escHtml(text)}</div>`;
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
}

function appendTyping() {
  const body = document.getElementById('chatBody');
  const div  = document.createElement('div');
  div.className = 'typing-indicator';
  div.innerHTML  = '<span></span><span></span><span></span>';
  body.appendChild(div);
  body.scrollTop = body.scrollHeight;
  return div;
}

function removeTyping(el) {
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show';
  setTimeout(() => { t.className = 'toast'; }, 3000);
}

// ─── Init ─────────────────────────────────────────────────────────────────────

loadAccounts();
</script>
</body>
</html>
```

- [ ] **Step 2: Set tokens in browser console and verify the page loads**

Start the server (if not already running):
```bash
uvicorn app.main:app --reload --port 8180
```

Open `http://localhost:8180/ui/reminder_debug.html` in a browser.

Open DevTools → Console, paste:
```js
localStorage.setItem("rdb_admin_token", "dev-admin-token");
localStorage.setItem("rdb_bridge_token", "dev-secret");
location.reload();
```

Expected: page reloads, account dropdown populates with accounts from the DB.

- [ ] **Step 3: Verify the golden path**

1. Select account `86f866663cf9-im-bot` from the dropdown.
2. Right panel shows existing reminders (or "暂无提醒" if none).
3. Type `明天上午10点提醒我喝水` in the chat input, press Enter.
4. Typing indicator appears, then AI reply appears in the chat.
5. Right panel auto-refreshes — new reminder card appears with `pending` status badge (blue).
6. Click **编辑** on the new reminder → inline form expands.
7. Change the time to `2026-12-01 09:00:00`, click **保存** → card updates with new time, toast shows "已保存".
8. Click **取消** → confirmation dialog appears → confirm → card disappears (or shows `cancelled` badge), toast shows "已取消".

- [ ] **Step 4: Verify non-pending reminders are read-only**

1. In the DB, find a `sent` reminder for the selected account (if none exist, send a message, wait for a reminder to be dispatched via the scheduler, or directly run `dispatch_due_reminders` in a Python shell).
2. Reload the reminder list — the `sent` reminder shows the status badge but no edit/cancel buttons.

- [ ] **Step 5: Commit**

```bash
git add app/static/reminder_debug.html
git commit -m "feat: add reminder debug UI at /ui/reminder_debug.html"
```

---

## Self-Review

**Spec coverage:**
- ✅ Left: account selector + chat (LLM path) — Task 2, Step 1 HTML
- ✅ Right: reminder list with status badges — `renderReminders()`
- ✅ Edit pending reminders — `doSaveEdit()` + PATCH route (Task 1)
- ✅ Cancel pending reminders — `doCancel()` + DELETE route (Task 1)
- ✅ Auto-refresh after chat turn — `await refreshReminders()` in `sendMessage()`
- ✅ Read-only for non-pending — `isPending` gate in `renderReminders()`
- ✅ Auth required — `Depends(verify_admin_auth)` on all 3 routes + test

**Placeholder scan:** No TBD, no TODO, all code is complete.

**Type consistency:** `reminder_id` used consistently across routes, tests, and JS functions. `ReminderDebugUpdateRequest` field names match `update_reminder()` parameter names exactly.
