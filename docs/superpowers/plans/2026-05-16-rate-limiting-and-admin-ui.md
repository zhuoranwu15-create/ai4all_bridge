# Rate Limiting + Admin Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-account RPM + daily rate limiting to the turn endpoint, then build a minimal HTML+JS admin UI served by FastAPI.

**Architecture:** In-memory sliding window for RPM; SQLite `daily_usage` table for daily counts. Static HTML/JS files in `app/static/` mounted at `/ui`. All admin API calls use the existing Bearer token auth.

**Tech Stack:** Python 3.9, FastAPI 0.115.6, SQLite, vanilla HTML/JS (no build step), pytest + httpx for tests.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `requirements.txt` | Modify | Add pytest, httpx |
| `app/config.py` | Modify | 4 new rate limit settings |
| `app/db.py` | Modify | `daily_usage` table, `accounts` new columns, new DB functions, `update_account` with `_UNSET` |
| `app/rate_limiter.py` | Create | `RateLimiter` class + module-level singleton |
| `app/main.py` | Modify | Rate limit checks in `openclaw_turn`, usage endpoint, static mount, `AccountUpdateRequest` new fields |
| `app/static/admin.js` | Create | Shared fetch wrapper, auth, utilities |
| `app/static/style.css` | Create | Minimal admin styles |
| `app/static/index.html` | Create | Account list page |
| `app/static/account.html` | Create | Account detail page |
| `tests/conftest.py` | Create | Shared fixtures (fresh DB, test client) |
| `tests/test_rate_limiter.py` | Create | Unit tests for `RateLimiter` |
| `tests/test_db_rate_limit.py` | Create | Unit tests for `daily_usage` DB functions |
| `tests/test_turn_rate_limit.py` | Create | Integration tests for rate limiting in turn endpoint |

---

## Task 1: Dependencies + Test Infrastructure

**Files:**
- Modify: `requirements.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add test dependencies**

Edit `requirements.txt` to add at the end:
```
pytest==8.3.4
httpx==0.28.1
```

- [ ] **Step 2: Install**

```bash
.venv/bin/pip install pytest==8.3.4 httpx==0.28.1
```
Expected: Successfully installed both packages.

- [ ] **Step 3: Create tests package**

```bash
mkdir -p tests && touch tests/__init__.py
```

- [ ] **Step 4: Write conftest.py**

Create `tests/conftest.py`:
```python
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture
def test_settings(tmp_path):
    s = MagicMock()
    s.database_path = str(tmp_path / "test.db")
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.ai4all_bridge_secret = "test-secret"
    s.admin_token = "test-admin"
    s.app_env = "test"
    s.llm_api_key = ""
    s.llm_context_messages = 12
    s.llm_default_prompt = "你是测试助手"
    s.rate_limit_daily = 3
    s.rate_limit_rpm = 10
    s.rate_limit_daily_message = "每日上限"
    s.rate_limit_rpm_message = "每分钟上限"
    return s


@pytest.fixture
def fresh_db(test_settings):
    """Patch app.db.settings to use a temp SQLite file."""
    with patch("app.db.settings", test_settings):
        from app.db import init_db
        init_db()
        yield test_settings


@pytest.fixture
def client(fresh_db):
    """FastAPI TestClient with isolated DB, test settings, mocked LLM."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.rate_limiter import RateLimiter

    patches = [
        patch("app.main.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch("app.main.rate_limiter", RateLimiter()),
        patch("app.main.generate_reply", return_value="mock reply"),
    ]
    for p in patches:
        p.start()
    yield TestClient(app)
    for p in patches:
        p.stop()
```

- [ ] **Step 5: Verify pytest runs**

```bash
.venv/bin/pytest tests/ -v
```
Expected: `no tests ran` (0 collected, no errors).

- [ ] **Step 6: Commit**

```bash
git add requirements.txt tests/
git commit -m "test: add pytest + httpx, create test infrastructure"
```

---

## Task 2: Config — Rate Limit Settings

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add settings to config.py**

In `app/config.py`, add 4 fields inside the `Settings` class after `llm_default_prompt`:
```python
rate_limit_daily: int = 100
rate_limit_rpm: int = 5
rate_limit_daily_message: str = "今天聊得有点多了，我晚些时候再继续陪你。"
rate_limit_rpm_message: str = "消息来得太快了，稍等一下再发我吧。"
```

- [ ] **Step 2: Update .env.example**

Add at the end of `.env.example`:
```
RATE_LIMIT_DAILY=100
RATE_LIMIT_RPM=5
RATE_LIMIT_DAILY_MESSAGE=今天聊得有点多了，我晚些时候再继续陪你。
RATE_LIMIT_RPM_MESSAGE=消息来得太快了，稍等一下再发我吧。
```

- [ ] **Step 3: Verify import**

```bash
.venv/bin/python -c "from app.config import settings; print(settings.rate_limit_daily, settings.rate_limit_rpm)"
```
Expected: `100 5`

- [ ] **Step 4: Commit**

```bash
git add app/config.py .env.example
git commit -m "feat: add rate limit config settings"
```

---

## Task 3: DB — daily_usage Table + accounts New Columns

**Files:**
- Modify: `app/db.py`
- Create: `tests/test_db_rate_limit.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_db_rate_limit.py`:
```python
import pytest
from unittest.mock import patch


def test_get_daily_usage_returns_zero_when_no_record(fresh_db):
    from app.db import get_daily_usage
    with patch("app.db.settings", fresh_db):
        count = get_daily_usage(account_id="acc1", date="2026-01-01")
    assert count == 0


def test_increment_daily_usage_creates_and_increments(fresh_db):
    from app.db import get_or_create_session, increment_daily_usage, get_daily_usage
    with patch("app.db.settings", fresh_db):
        # Account must exist before we can insert daily_usage (FK)
        get_or_create_session(
            account_id="acc1", channel="test",
            sender_id="s1", sender_name=None,
            chat_id=None, session_key="sk1",
        )
        count1 = increment_daily_usage(account_id="acc1", date="2026-01-01")
        count2 = increment_daily_usage(account_id="acc1", date="2026-01-01")
        count3 = get_daily_usage(account_id="acc1", date="2026-01-01")
    assert count1 == 1
    assert count2 == 2
    assert count3 == 2


def test_different_dates_tracked_separately(fresh_db):
    from app.db import get_or_create_session, increment_daily_usage, get_daily_usage
    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="acc1", channel="test",
            sender_id="s1", sender_name=None,
            chat_id=None, session_key="sk1",
        )
        increment_daily_usage(account_id="acc1", date="2026-01-01")
        increment_daily_usage(account_id="acc1", date="2026-01-02")
        count1 = get_daily_usage(account_id="acc1", date="2026-01-01")
        count2 = get_daily_usage(account_id="acc1", date="2026-01-02")
    assert count1 == 1
    assert count2 == 1


def test_get_usage_last_7_days(fresh_db):
    from app.db import get_or_create_session, increment_daily_usage, get_usage_last_7_days
    with patch("app.db.settings", fresh_db):
        get_or_create_session(
            account_id="acc1", channel="test",
            sender_id="s1", sender_name=None,
            chat_id=None, session_key="sk1",
        )
        increment_daily_usage(account_id="acc1", date="2026-01-01")
        increment_daily_usage(account_id="acc1", date="2026-01-01")
        increment_daily_usage(account_id="acc1", date="2026-01-03")
        rows = get_usage_last_7_days(account_id="acc1")
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-01-03"  # ordered DESC
    assert rows[1]["message_count"] == 2
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_db_rate_limit.py -v
```
Expected: `ImportError: cannot import name 'get_daily_usage' from 'app.db'`

- [ ] **Step 3: Add daily_usage table + accounts columns to db.py**

In `init_db()` inside `app/db.py`, add to `conn.executescript(...)` after the messages index:
```sql
CREATE TABLE IF NOT EXISTS daily_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    date TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, date),
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);
```

After the `_ensure_column` calls at the end of `init_db()`, add:
```python
_ensure_column(conn, "accounts", "daily_limit", "INTEGER")
_ensure_column(conn, "accounts", "rpm_limit", "INTEGER")
```

- [ ] **Step 4: Add DB functions to db.py**

Add after `set_account_status`:
```python
def get_daily_usage(*, account_id: str, date: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT message_count FROM daily_usage WHERE account_id = ? AND date = ?",
            (account_id, date),
        ).fetchone()
    return int(row["message_count"]) if row else 0


def increment_daily_usage(*, account_id: str, date: str) -> int:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_usage(account_id, date, message_count, updated_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id, date) DO UPDATE SET
                message_count = message_count + 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account_id, date),
        )
        row = conn.execute(
            "SELECT message_count FROM daily_usage WHERE account_id = ? AND date = ?",
            (account_id, date),
        ).fetchone()
    return int(row["message_count"]) if row else 1


def get_usage_last_7_days(*, account_id: str) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT date, message_count FROM daily_usage
            WHERE account_id = ?
            ORDER BY date DESC
            LIMIT 7
            """,
            (account_id,),
        ).fetchall()
    return [dict(row) for row in rows]
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/pytest tests/test_db_rate_limit.py -v
```
Expected: 4 tests PASS.

- [ ] **Step 6: Verify migration on existing DB**

```bash
.venv/bin/python -c "from app.db import init_db; init_db(); print('OK')"
sqlite3 data/ai4all.sqlite3 ".tables"
sqlite3 data/ai4all.sqlite3 "PRAGMA table_info(accounts)" | grep -E "daily|rpm"
```
Expected: `daily_usage` appears in tables; two rows for `daily_limit` and `rpm_limit`.

- [ ] **Step 7: Commit**

```bash
git add app/db.py tests/test_db_rate_limit.py
git commit -m "feat: add daily_usage table and per-account rate limit columns"
```

---

## Task 4: Update update_account for New Fields

**Files:**
- Modify: `app/db.py`
- Modify: `app/main.py`

- [ ] **Step 1: Add _UNSET sentinel and update update_account in db.py**

At the top of `app/db.py`, after the imports, add:
```python
_UNSET = object()
```

Replace the `update_account` function:
```python
def update_account(
    *,
    account_id: str,
    display_name=_UNSET,
    notes=_UNSET,
    daily_limit=_UNSET,
    rpm_limit=_UNSET,
) -> Optional[Dict[str, Any]]:
    current = get_account(account_id=account_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE accounts
            SET display_name = ?,
                notes = ?,
                daily_limit = ?,
                rpm_limit = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                current.get("display_name") if display_name is _UNSET else display_name,
                current.get("notes") if notes is _UNSET else notes,
                current.get("daily_limit") if daily_limit is _UNSET else daily_limit,
                current.get("rpm_limit") if rpm_limit is _UNSET else rpm_limit,
                account_id,
            ),
        )
    return get_account(account_id=account_id)
```

- [ ] **Step 2: Update AccountUpdateRequest in main.py**

Replace the existing `AccountUpdateRequest` class:
```python
class AccountUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    notes: Optional[str] = None
    daily_limit: Optional[int] = None
    rpm_limit: Optional[int] = None
```

- [ ] **Step 3: Update admin_update_account endpoint in main.py to use model_dump(exclude_unset=True)**

Replace the `admin_update_account` endpoint body:
```python
@app.patch("/admin/accounts/{account_id}")
def admin_update_account(
    account_id: str,
    payload: AccountUpdateRequest,
    _: None = Depends(verify_admin_auth),
) -> dict:
    updates = payload.model_dump(exclude_unset=True)
    account = update_account(account_id=account_id, **updates)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return {"status": "ok", "account": account}
```

- [ ] **Step 4: Verify with curl (backend must be running)**

```bash
# Restart backend
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
.venv/bin/uvicorn app.main:app --port 8000 &
sleep 2

# Set daily_limit on one account
curl -s -X PATCH -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"daily_limit": 50}' \
  http://127.0.0.1:8000/admin/accounts/acct_example | python3 -m json.tool | grep daily_limit

# Clear it back to null
curl -s -X PATCH -H "Authorization: Bearer dev-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"daily_limit": null}' \
  http://127.0.0.1:8000/admin/accounts/acct_example | python3 -m json.tool | grep daily_limit
```
Expected: First response `"daily_limit": 50`, second response `"daily_limit": null`.

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/main.py
git commit -m "feat: extend update_account with daily_limit and rpm_limit fields"
```

---

## Task 5: RateLimiter Module

**Files:**
- Create: `app/rate_limiter.py`
- Create: `tests/test_rate_limiter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_rate_limiter.py`:
```python
import time
from app.rate_limiter import RateLimiter


def test_allows_requests_under_limit():
    rl = RateLimiter()
    for _ in range(5):
        assert rl.check_rpm("acc", 5) is True


def test_blocks_at_limit():
    rl = RateLimiter()
    for _ in range(5):
        rl.check_rpm("acc", 5)
    assert rl.check_rpm("acc", 5) is False


def test_denied_request_not_recorded():
    rl = RateLimiter()
    for _ in range(3):
        rl.check_rpm("acc", 3)
    # At limit — denied, should not advance the window
    rl.check_rpm("acc", 3)
    rl.check_rpm("acc", 3)
    # Still exactly 3 recorded
    assert len(rl._windows["acc"]) == 3


def test_accounts_are_isolated():
    rl = RateLimiter()
    for _ in range(3):
        rl.check_rpm("acc_a", 3)
    # acc_a is at limit
    assert rl.check_rpm("acc_a", 3) is False
    # acc_b is unaffected
    assert rl.check_rpm("acc_b", 3) is True


def test_zero_limit_means_no_limit():
    rl = RateLimiter()
    for _ in range(1000):
        assert rl.check_rpm("acc", 0) is True


def test_window_slides_after_60_seconds(monkeypatch):
    import time as time_module
    rl = RateLimiter()
    now = [0.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: now[0])

    for _ in range(3):
        rl.check_rpm("acc", 3)
    assert rl.check_rpm("acc", 3) is False  # at limit

    now[0] = 61.0  # advance past the 60s window
    assert rl.check_rpm("acc", 3) is True  # old entries expired
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_rate_limiter.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.rate_limiter'`

- [ ] **Step 3: Create app/rate_limiter.py**

```python
import threading
import time
from collections import defaultdict, deque
from typing import Dict


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: Dict[str, deque] = defaultdict(deque)

    def check_rpm(self, account_id: str, limit: int) -> bool:
        if limit == 0:
            return True
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            window = self._windows[account_id]
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True


rate_limiter = RateLimiter()
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/pytest tests/test_rate_limiter.py -v
```
Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add RateLimiter sliding window module"
```

---

## Task 6: Wire Rate Limiting into openclaw_turn + Usage Endpoint

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_turn_rate_limit.py`

- [ ] **Step 1: Write failing integration tests**

Create `tests/test_turn_rate_limit.py`:
```python
import pytest

BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def make_payload(msg_id: str, text: str = "hello") -> dict:
    return {
        "account_id": "acc-test",
        "session_key": "sk-test",
        "sender_id": "sender-test",
        "chat_type": "private",
        "message_type": "text",
        "message_id": msg_id,
        "text": text,
    }


def test_daily_rate_limit_blocks_after_limit(client):
    # daily limit is 3 (set in conftest test_settings)
    for i in range(3):
        res = client.post("/openclaw/turn", json=make_payload(f"m{i}"), headers=BRIDGE_HEADERS)
        assert res.status_code == 200
        assert res.json()["status"] != "rate_limited"

    res = client.post("/openclaw/turn", json=make_payload("m3"), headers=BRIDGE_HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "rate_limited"
    assert data["reply"] == "每日上限"
    assert data.get("no_reply") is not True  # reply should be sent


def test_rate_limited_message_not_counted(client):
    # Fill up the 3-message daily limit
    for i in range(3):
        client.post("/openclaw/turn", json=make_payload(f"m{i}"), headers=BRIDGE_HEADERS)

    # Rate limited — should not increment counter further
    for i in range(5):
        res = client.post("/openclaw/turn", json=make_payload(f"extra{i}"), headers=BRIDGE_HEADERS)
        assert res.json()["status"] == "rate_limited"

    # Check usage — still 3, not 8
    res = client.get(
        "/admin/accounts/acc-test/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    assert res.json()["today"]["message_count"] == 3


def test_usage_endpoint_returns_today_and_history(client):
    # First create the account by posting a turn
    client.post("/openclaw/turn", json=make_payload("m0"), headers=BRIDGE_HEADERS)

    res = client.get(
        "/admin/accounts/acc-test/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "today" in data
    assert "last_7_days" in data
    assert data["today"]["message_count"] == 1


def test_usage_endpoint_404_for_unknown_account(client):
    res = client.get(
        "/admin/accounts/nonexistent/usage",
        headers={"Authorization": "Bearer test-admin"},
    )
    assert res.status_code == 404
```

- [ ] **Step 2: Run to confirm failure**

```bash
.venv/bin/pytest tests/test_turn_rate_limit.py -v
```
Expected: Tests fail — rate limiting not implemented yet, usage endpoint doesn't exist.

- [ ] **Step 3: Update imports in main.py**

Add to the import block in `app/main.py`:
```python
from datetime import date as date_cls
from app.rate_limiter import rate_limiter
from app.db import (
    ...
    get_daily_usage,
    get_usage_last_7_days,
    increment_daily_usage,
    ...
)
```

- [ ] **Step 4: Add rate limit checks to openclaw_turn in main.py**

In `openclaw_turn`, after the `if account.get("status") == "disabled":` block and before the `duplicate_reply = get_duplicate_reply(...)` call, insert:

```python
today = date_cls.today().isoformat()
effective_rpm = settings.rate_limit_rpm if account.get("rpm_limit") is None else account["rpm_limit"]
effective_daily = settings.rate_limit_daily if account.get("daily_limit") is None else account["daily_limit"]

if effective_rpm > 0 and not rate_limiter.check_rpm(account_id, effective_rpm):
    logger.info("openclaw_turn rpm_limited account=%s", account_id)
    return OpenClawTurnResponse(
        status="rate_limited",
        reply=settings.rate_limit_rpm_message,
        metadata={"account_id": account_id, "reason": "rpm"},
    )

if effective_daily > 0:
    current_count = get_daily_usage(account_id=account_id, date=today)
    if current_count >= effective_daily:
        logger.info("openclaw_turn daily_limited account=%s count=%s", account_id, current_count)
        return OpenClawTurnResponse(
            status="rate_limited",
            reply=settings.rate_limit_daily_message,
            metadata={"account_id": account_id, "reason": "daily", "count": current_count},
        )
```

- [ ] **Step 5: Add increment_daily_usage call after successful message insert**

In `openclaw_turn`, after the `if inserted_id is None:` block (i.e., when we know it's NOT a duplicate), add before the `generation_error = None` line:

```python
increment_daily_usage(account_id=account_id, date=today)
```

- [ ] **Step 6: Add usage endpoint to main.py**

Add after `admin_get_user_profile`:
```python
@app.get("/admin/accounts/{account_id}/usage")
def admin_account_usage(
    account_id: str,
    _: None = Depends(verify_admin_auth),
) -> dict:
    if get_account(account_id=account_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    today = date_cls.today().isoformat()
    return {
        "account_id": account_id,
        "today": {
            "date": today,
            "message_count": get_daily_usage(account_id=account_id, date=today),
        },
        "last_7_days": get_usage_last_7_days(account_id=account_id),
    }
```

- [ ] **Step 7: Run tests**

```bash
.venv/bin/pytest tests/test_turn_rate_limit.py -v
```
Expected: 4 tests PASS.

- [ ] **Step 8: Run full test suite**

```bash
.venv/bin/pytest tests/ -v
```
Expected: All tests PASS.

- [ ] **Step 9: Commit**

```bash
git add app/main.py tests/test_turn_rate_limit.py
git commit -m "feat: wire per-account RPM and daily rate limiting into turn endpoint"
```

---

## Task 7: Static Files — Mount + admin.js + style.css

**Files:**
- Modify: `app/main.py`
- Create: `app/static/admin.js`
- Create: `app/static/style.css`

- [ ] **Step 1: Create static directory**

```bash
mkdir -p app/static
```

- [ ] **Step 2: Mount StaticFiles in main.py**

Add import at the top of `app/main.py`:
```python
from fastapi.staticfiles import StaticFiles
```

Add after `app = FastAPI(...)`:
```python
app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")
```

- [ ] **Step 3: Create app/static/admin.js**

```javascript
function getToken() {
  let token = localStorage.getItem('admin_token');
  if (!token) {
    token = prompt('请输入 Admin Token：');
    if (token) localStorage.setItem('admin_token', token.trim());
  }
  return token;
}

async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = { 'Authorization': 'Bearer ' + token };
  if (options.body) headers['Content-Type'] = 'application/json';

  const res = await fetch(path, {
    ...options,
    headers: Object.assign(headers, options.headers || {}),
  });

  if (res.status === 401) {
    localStorage.removeItem('admin_token');
    throw new Error('认证失败，请刷新页面重新输入 Token');
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error('API 错误 ' + res.status + ': ' + body);
  }
  return res.json();
}

function esc(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showStatus(el, msg, isError) {
  el.textContent = msg;
  el.className = 'status-msg ' + (isError ? 'error' : 'success');
  if (!isError) setTimeout(function() { el.textContent = ''; }, 3000);
}
```

- [ ] **Step 4: Create app/static/style.css**

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, sans-serif; background: #f5f5f5; color: #333; padding: 24px; }
h1 { font-size: 1.4rem; margin-bottom: 20px; }
h2 { font-size: 1rem; color: #555; margin-bottom: 12px; }
.card { background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #eee; font-size: .9rem; }
th { background: #f9f9f9; font-weight: 600; color: #666; font-size: .8rem; text-transform: uppercase; }
tr:last-child td { border-bottom: none; }
a { color: #3b82f6; text-decoration: none; }
a:hover { text-decoration: underline; }
button { cursor: pointer; padding: 5px 14px; border: 1px solid #d1d5db; border-radius: 5px; background: #fff; font-size: .85rem; }
button:hover { background: #f9fafb; }
button.primary { background: #3b82f6; color: #fff; border-color: #3b82f6; }
button.primary:hover { background: #2563eb; }
button.danger { color: #dc2626; border-color: #dc2626; }
button.danger:hover { background: #fef2f2; }
.status-active { color: #16a34a; font-weight: 600; }
.status-disabled { color: #dc2626; font-weight: 600; }
.status-msg { font-size: .82rem; margin-left: 10px; }
.status-msg.success { color: #16a34a; }
.status-msg.error { color: #dc2626; }
label { display: block; font-size: .82rem; color: #6b7280; margin-bottom: 4px; }
input[type=text], input[type=number], textarea { width: 100%; padding: 7px 10px; border: 1px solid #d1d5db; border-radius: 5px; font-size: .9rem; }
textarea { min-height: 90px; resize: vertical; font-family: inherit; }
.form-row { margin-bottom: 12px; }
.form-actions { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
.back-link { display: inline-block; margin-bottom: 16px; font-size: .9rem; color: #6b7280; }
.back-link:hover { color: #333; text-decoration: none; }
.session-row { border: 1px solid #e5e7eb; border-radius: 6px; margin-bottom: 8px; overflow: hidden; }
.session-hdr { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; cursor: default; background: #f9fafb; font-size: .85rem; }
.session-msgs { display: none; border-top: 1px solid #e5e7eb; }
.session-msgs.open { display: block; }
.msg { display: flex; gap: 8px; padding: 8px 14px; border-bottom: 1px solid #f3f4f6; font-size: .85rem; }
.msg:last-child { border-bottom: none; }
.msg-role { font-weight: 600; min-width: 30px; color: #6b7280; }
.msg-role.user { color: #374151; }
.msg-role.assistant { color: #3b82f6; }
.msg-content { flex: 1; white-space: pre-wrap; word-break: break-word; }
.msg-time { font-size: .75rem; color: #9ca3af; white-space: nowrap; }
```

- [ ] **Step 5: Restart backend and verify static files are served**

```bash
pkill -f "uvicorn app.main" 2>/dev/null; sleep 1
.venv/bin/uvicorn app.main:app --port 8000 &
sleep 2
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ui/admin.js
```
Expected: `200`

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/static/admin.js app/static/style.css
git commit -m "feat: mount static files and add shared admin JS + CSS"
```

---

## Task 8: index.html — Account List Page

**Files:**
- Create: `app/static/index.html`

- [ ] **Step 1: Create app/static/index.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI4ALL 运营后台</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <h1>AI4ALL 运营后台</h1>
  <div class="card">
    <table>
      <thead>
        <tr>
          <th>账号 ID</th>
          <th>状态</th>
          <th>今日 / 日上限</th>
          <th>最近活跃</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="tbody">
        <tr><td colspan="5">加载中…</td></tr>
      </tbody>
    </table>
  </div>

  <script src="admin.js"></script>
  <script>
    (async function load() {
      var tbody = document.getElementById('tbody');
      try {
        var data = await apiFetch('/admin/accounts');
        var accounts = data.accounts;
        if (!accounts.length) {
          tbody.innerHTML = '<tr><td colspan="5">暂无账号</td></tr>';
          return;
        }
        var usages = await Promise.all(accounts.map(function(a) {
          return apiFetch('/admin/accounts/' + encodeURIComponent(a.id) + '/usage')
            .catch(function() { return { today: { message_count: '?' } }; });
        }));
        tbody.innerHTML = accounts.map(function(a, i) {
          var count = usages[i].today.message_count;
          var limit = a.daily_limit != null ? a.daily_limit : '默认';
          var lastActive = a.last_active_at ? a.last_active_at.slice(0, 16) : '—';
          var toggleBtn = a.status === 'active'
            ? '<button class="danger" onclick="toggle(\'' + esc(a.id) + '\',\'disable\')">禁用</button>'
            : '<button class="primary" onclick="toggle(\'' + esc(a.id) + '\',\'enable\')">启用</button>';
          return '<tr>'
            + '<td><a href="account.html?id=' + encodeURIComponent(a.id) + '">' + esc(a.id) + '</a></td>'
            + '<td class="status-' + esc(a.status) + '">' + esc(a.status) + '</td>'
            + '<td>' + esc(String(count)) + ' / ' + esc(String(limit)) + '</td>'
            + '<td>' + esc(lastActive) + '</td>'
            + '<td>' + toggleBtn + '</td>'
            + '</tr>';
        }).join('');
      } catch (e) {
        tbody.innerHTML = '<tr><td colspan="5" class="status-msg error">' + esc(e.message) + '</td></tr>';
      }
    })();

    async function toggle(id, action) {
      try {
        await apiFetch('/admin/accounts/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
        location.reload();
      } catch (e) {
        alert(e.message);
      }
    }
  </script>
</body>
</html>
```

- [ ] **Step 2: Verify in browser**

Open `http://localhost:8000/ui/` in a browser. Enter `dev-admin-token` when prompted. Verify:
- Account table loads with all accounts
- Today's message count shows
- Disable/enable buttons work (page reloads after toggle)

- [ ] **Step 3: Commit**

```bash
git add app/static/index.html
git commit -m "feat: add admin UI account list page"
```

---

## Task 9: account.html — Account Detail Page

**Files:**
- Create: `app/static/account.html`

- [ ] **Step 1: Create app/static/account.html**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>账号详情 – AI4ALL</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <a class="back-link" href="index.html">← 返回列表</a>
  <div id="err" style="display:none" class="status-msg error"></div>
  <div id="content" style="display:none">

    <div class="card">
      <h2>基本信息</h2>
      <div class="form-row"><label>账号 ID</label><input id="f-id" disabled></div>
      <div class="form-row"><label>渠道</label><input id="f-channel" disabled></div>
      <div class="form-row"><label>状态</label><span id="f-status"></span></div>
      <div class="form-row"><label>备注</label><input type="text" id="f-notes" placeholder="运营备注"></div>
      <div class="form-row"><label>显示名称</label><input type="text" id="f-display-name" placeholder="昵称（可选）"></div>
      <div class="form-actions">
        <button class="primary" onclick="saveBasic()">保存</button>
        <span id="st-basic" class="status-msg"></span>
        <button id="btn-toggle" onclick="toggleStatus()" style="margin-left:auto"></button>
      </div>
    </div>

    <div class="card">
      <h2>限流配置</h2>
      <div class="form-row">
        <label>每日上限（空 = 全局默认 100）</label>
        <input type="number" id="f-daily-limit" min="0" placeholder="留空使用全局默认">
      </div>
      <div class="form-row">
        <label>每分钟上限（空 = 全局默认 5）</label>
        <input type="number" id="f-rpm-limit" min="0" placeholder="留空使用全局默认">
      </div>
      <div class="form-actions">
        <button class="primary" onclick="saveLimits()">保存</button>
        <span id="st-limits" class="status-msg"></span>
      </div>
    </div>

    <div class="card">
      <h2>AI 配置</h2>
      <div class="form-row"><label>回复风格</label><input type="text" id="f-style" placeholder="例：简洁活泼"></div>
      <div class="form-row">
        <label>System Prompt（空 = 系统默认）</label>
        <textarea id="f-system-prompt" placeholder="留空使用系统默认 Prompt"></textarea>
      </div>
      <div class="form-actions">
        <button class="primary" onclick="saveProfile()">保存</button>
        <span id="st-profile" class="status-msg"></span>
      </div>
    </div>

    <div class="card">
      <h2>今日用量</h2>
      <p id="usage-text">—</p>
    </div>

    <div class="card">
      <h2>会话列表</h2>
      <div id="sessions"></div>
    </div>

  </div>

  <script src="admin.js"></script>
  <script>
    var accountId = new URLSearchParams(location.search).get('id');
    if (!accountId) {
      showErr('缺少账号 ID 参数');
    } else {
      loadAll();
    }

    function showErr(msg) {
      var el = document.getElementById('err');
      el.textContent = msg;
      el.style.display = '';
    }

    async function loadAll() {
      try {
        var results = await Promise.all([
          apiFetch('/admin/accounts/' + encodeURIComponent(accountId)),
          apiFetch('/admin/accounts/' + encodeURIComponent(accountId) + '/usage'),
        ]);
        var accountData = results[0];
        var usageData = results[1];
        fillForm(accountData.account, accountData.profile);
        fillSessions(accountData.sessions);
        fillUsage(usageData);
        document.getElementById('content').style.display = '';
        document.title = accountId + ' – AI4ALL';
      } catch (e) {
        showErr(e.message);
      }
    }

    function fillForm(account, profile) {
      document.getElementById('f-id').value = account.id;
      document.getElementById('f-channel').value = account.channel || '';
      var statusEl = document.getElementById('f-status');
      statusEl.textContent = account.status;
      statusEl.className = 'status-' + account.status;
      document.getElementById('f-notes').value = account.notes || '';
      document.getElementById('f-display-name').value = account.display_name || '';
      document.getElementById('f-daily-limit').value = account.daily_limit != null ? account.daily_limit : '';
      document.getElementById('f-rpm-limit').value = account.rpm_limit != null ? account.rpm_limit : '';
      document.getElementById('f-style').value = (profile && profile.style) || '';
      document.getElementById('f-system-prompt').value = (profile && profile.system_prompt) || '';
      var btn = document.getElementById('btn-toggle');
      if (account.status === 'active') {
        btn.textContent = '禁用账号';
        btn.className = 'danger';
      } else {
        btn.textContent = '启用账号';
        btn.className = 'primary';
      }
    }

    function fillUsage(usage) {
      document.getElementById('usage-text').textContent =
        '今日已发 ' + usage.today.message_count + ' 条';
    }

    function fillSessions(sessions) {
      var el = document.getElementById('sessions');
      if (!sessions || !sessions.length) { el.textContent = '暂无会话'; return; }
      el.innerHTML = sessions.map(function(s) {
        return '<div class="session-row">'
          + '<div class="session-hdr">'
          + '<span style="font-family:monospace;font-size:.8rem">' + esc(s.session_key) + '</span>'
          + '<span>' + (s.message_count || 0) + ' 条'
          + (s.last_message_at ? ' · ' + s.last_message_at.slice(0,16) : '')
          + ' <button onclick="toggleMsgs(this,' + s.id + ')">展开</button></span>'
          + '</div>'
          + '<div class="session-msgs" id="sm-' + s.id + '"></div>'
          + '</div>';
      }).join('');
    }

    async function toggleMsgs(btn, sessionId) {
      var container = document.getElementById('sm-' + sessionId);
      if (container.classList.contains('open')) {
        container.classList.remove('open');
        btn.textContent = '展开';
        return;
      }
      if (!container.dataset.loaded) {
        btn.textContent = '加载中…';
        try {
          var data = await apiFetch('/admin/sessions/' + sessionId);
          var msgs = (data.messages || []).slice(-20);
          container.innerHTML = msgs.length
            ? msgs.map(function(m) {
                return '<div class="msg">'
                  + '<span class="msg-role ' + esc(m.role) + '">' + (m.role === 'user' ? '用户' : 'AI') + '</span>'
                  + '<span class="msg-content">' + esc(m.content || '') + '</span>'
                  + '<span class="msg-time">' + esc((m.created_at || '').slice(0,16)) + '</span>'
                  + '</div>';
              }).join('')
            : '<div class="msg"><span class="msg-content">暂无消息</span></div>';
          container.dataset.loaded = '1';
        } catch (e) {
          container.textContent = e.message;
        }
      }
      container.classList.add('open');
      btn.textContent = '收起';
    }

    async function saveBasic() {
      var st = document.getElementById('st-basic');
      showStatus(st, '保存中…');
      try {
        await apiFetch('/admin/accounts/' + encodeURIComponent(accountId), {
          method: 'PATCH',
          body: JSON.stringify({
            notes: document.getElementById('f-notes').value || null,
            display_name: document.getElementById('f-display-name').value || null,
          }),
        });
        showStatus(st, '已保存 ✓');
      } catch (e) { showStatus(st, e.message, true); }
    }

    async function saveLimits() {
      var st = document.getElementById('st-limits');
      showStatus(st, '保存中…');
      var dv = document.getElementById('f-daily-limit').value;
      var rv = document.getElementById('f-rpm-limit').value;
      try {
        await apiFetch('/admin/accounts/' + encodeURIComponent(accountId), {
          method: 'PATCH',
          body: JSON.stringify({
            daily_limit: dv === '' ? null : parseInt(dv, 10),
            rpm_limit: rv === '' ? null : parseInt(rv, 10),
          }),
        });
        showStatus(st, '已保存 ✓');
      } catch (e) { showStatus(st, e.message, true); }
    }

    async function saveProfile() {
      var st = document.getElementById('st-profile');
      showStatus(st, '保存中…');
      try {
        await apiFetch('/admin/accounts/' + encodeURIComponent(accountId) + '/profile', {
          method: 'PATCH',
          body: JSON.stringify({
            style: document.getElementById('f-style').value || null,
            system_prompt: document.getElementById('f-system-prompt').value || null,
          }),
        });
        showStatus(st, '已保存 ✓');
      } catch (e) { showStatus(st, e.message, true); }
    }

    async function toggleStatus() {
      var statusEl = document.getElementById('f-status');
      var action = statusEl.textContent === 'active' ? 'disable' : 'enable';
      try {
        await apiFetch('/admin/accounts/' + encodeURIComponent(accountId) + '/' + action, { method: 'POST' });
        loadAll();
      } catch (e) { alert(e.message); }
    }
  </script>
</body>
</html>
```

- [ ] **Step 2: Verify in browser**

Open `http://localhost:8000/ui/` → click any account ID link → verify:
- All four sections load with correct data
- 基本信息: notes and display_name save correctly
- 限流配置: setting a number saves; clearing to empty resets to global default
- AI 配置: style and system_prompt save correctly
- 会话列表: expand button loads last 20 messages

- [ ] **Step 3: Commit**

```bash
git add app/static/account.html
git commit -m "feat: add admin UI account detail page"
```

---

## Self-Review Checklist

- [x] Spec coverage:
  - daily_usage table ✓ (Task 3)
  - rpm in-memory window ✓ (Task 5)
  - accounts.daily_limit / rpm_limit ✓ (Task 3)
  - config 4 items ✓ (Task 2)
  - rate limit in turn endpoint ✓ (Task 6)
  - today variable defined before both checks ✓
  - increment after successful insert, not after rate-limited ✓ (Task 6 Step 5)
  - usage endpoint ✓ (Task 6)
  - static mount ✓ (Task 7)
  - index.html ✓ (Task 8)
  - account.html ✓ (Task 9)
  - admin.js + style.css ✓ (Task 7)
  - .env.example ✓ (Task 2)
  - update_account with _UNSET ✓ (Task 4)
  - model_dump(exclude_unset=True) ✓ (Task 4)
- [x] No placeholders: all code steps are complete
- [x] Type consistency: `get_daily_usage` and `increment_daily_usage` signatures match usage in main.py; `_UNSET` sentinel defined before use; `rate_limiter` imported before use
