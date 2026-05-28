# Review Fixes: Identity & Binding Issues

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all Critical and Important issues identified in the code review of the user identity and WeChat binding feature.

**Architecture:** 8 independent fixes across `app/openclaw_gateway.py`, `app/db.py`, `app/main.py`, `app/static/onboarding.html`, and `tests/conftest.py`. Each fix is self-contained and testable.

**Tech Stack:** Python/FastAPI, SQLite, `qrcode[pil]` (new dep), pytest

---

## Files Modified

- `requirements.txt` — add `qrcode[pil]` and `Pillow`
- `app/openclaw_gateway.py` — replace hardcoded Node.js path with Python QR rendering
- `app/db.py` — phone normalization, binding_intent expiry, channel_account_id column, duplicate channel_binding dedup
- `app/main.py` — add auth to debug endpoints, expose expired binding status
- `app/static/onboarding.html` — add `expired` to status text (already in terminalBindingStatuses)
- `tests/conftest.py` — add OpenClaw settings

---

## Task 1: Fix QR rendering — replace hardcoded Homebrew path with Python

**Files:**
- Modify: `requirements.txt`
- Modify: `app/openclaw_gateway.py`

- [ ] Add `qrcode[pil]` to requirements.txt

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic-settings==2.7.1
pytest==8.3.4
httpx==0.28.1
qrcode[pil]==8.0
```

- [ ] Install it

```bash
pip install qrcode[pil]
```

- [ ] Replace `_render_qr_payload_to_data_url` in `app/openclaw_gateway.py` with Python implementation

Remove the entire `_render_qr_payload_to_data_url` function and replace with:

```python
import base64
import io
import json
import subprocess
from typing import Any, Dict, Optional

import qrcode
import qrcode.image.pil


class OpenClawGatewayError(RuntimeError):
    pass


def _render_qr_payload_to_data_url(payload: str) -> str:
    img = qrcode.make(payload, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"
```

- [ ] Run tests

```bash
cd /Users/suchong/workspace/ai4all/weixin_bot
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] Verify manually that `normalize_qr_data_url` passes through `data:image/` URLs unchanged and renders raw strings via Python

```bash
python -c "
from app.openclaw_gateway import normalize_qr_data_url
# passthrough
assert normalize_qr_data_url('data:image/png;base64,abc') == 'data:image/png;base64,abc'
# render raw
result = normalize_qr_data_url('https://weixin.qq.com/some-payload')
assert result.startswith('data:image/png;base64,'), result[:80]
print('OK')
"
```

---

## Task 2: Add per-user account limit to prevent unlimited account creation

**Files:**
- Modify: `app/db.py` — add limit check in `create_ai4all_account_for_user`
- Modify: `tests/test_web_onboarding.py` — add a test for the limit

The limit is 10 agents per user (configurable but hardcoded for MVP).

- [ ] Add limit check in `create_ai4all_account_for_user` (in the `with connect()` block, after verifying the user exists)

```python
existing_count = conn.execute(
    """
    SELECT COUNT(*) FROM account_owner_bindings
    WHERE platform_user_id = ? AND status = 'active'
    """,
    (platform_user_id,),
).fetchone()[0]
if existing_count >= 10:
    raise ValueError("platform_user has reached the maximum number of agents (10)")
```

- [ ] Add test in `tests/test_web_onboarding.py`

```python
def test_web_create_agent_enforces_per_user_limit(client):
    user = client.post(
        "/web/register",
        json={"phone": "13800009999"},
    ).json()["platform_user"]
    for i in range(10):
        res = client.post(
            "/web/agents",
            json={"platform_user_id": user["id"], "agent_name": f"Bot {i}"},
        )
        assert res.status_code == 200
    res = client.post(
        "/web/agents",
        json={"platform_user_id": user["id"], "agent_name": "Bot 11"},
    )
    assert res.status_code == 400
    assert "maximum" in res.json()["detail"]
```

- [ ] Run test

```bash
python -m pytest tests/test_web_onboarding.py::test_web_create_agent_enforces_per_user_limit -v
```

Expected: PASS.

---

## Task 3: Implement binding_intent expiry

**Files:**
- Modify: `app/db.py` — auto-expire in `get_binding_intent`
- Modify: `app/static/onboarding.html` — add `expired` status text (terminalBindingStatuses already has it)
- Modify: `tests/test_web_onboarding.py` — add expiry test

- [ ] Add expiry check inside `get_binding_intent`, after parsing `raw_result_json`

```python
# Auto-expire stale qr_created intents
if item.get("status") == "qr_created" and item.get("expires_at"):
    with connect() as conn:
        conn.execute(
            """
            UPDATE binding_intents
            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'qr_created'
              AND expires_at < datetime('now')
            """,
            (binding_intent_id,),
        )
        refreshed = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, qr_data_url, manual_login_command,
                raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents WHERE id = ?
            """,
            (binding_intent_id,),
        ).fetchone()
    if refreshed is not None:
        item = dict(refreshed)
        try:
            item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
        except json.JSONDecodeError:
            item["raw_result"] = {}
```

- [ ] Add `expired` to status text map in `bindingStatusText` in `onboarding.html`

```javascript
if (intent.status === 'expired') return '二维码已过期，请重新生成';
```

- [ ] Add test

```python
def test_get_binding_intent_auto_expires_stale_qr(client):
    from app.db import get_binding_intent, update_binding_intent
    import app.db as db_module

    user = client.post(
        "/web/register",
        json={"phone": "13800007777"},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={"platform_user_id": user["id"], "agent_name": "Expiry Bot"},
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "exp-session"},
    ):
        intent = client.post(
            "/web/binding-intents",
            json={"platform_user_id": user["id"], "account_id": account["id"]},
        ).json()["binding_intent"]

    # Force expires_at to the past
    with db_module.connect() as conn:
        conn.execute(
            "UPDATE binding_intents SET expires_at = datetime('now', '-1 minute') WHERE id = ?",
            (intent["id"],),
        )

    fetched = get_binding_intent(binding_intent_id=intent["id"])
    assert fetched["status"] == "expired"
```

- [ ] Run test

```bash
python -m pytest tests/test_web_onboarding.py::test_get_binding_intent_auto_expires_stale_qr -v
```

Expected: PASS.

---

## Task 4: Add `channel_account_id` column to `binding_intents` and use it in lookup

**Files:**
- Modify: `app/db.py` — add column, update writer, update reader

- [ ] Add `_ensure_column` call in `init_db` (after the existing 3 `_ensure_column` calls for binding_intents)

```python
_ensure_column(conn, "binding_intents", "channel_account_id", "TEXT")
```

- [ ] Update `get_binding_intent` SELECT to include the new column

In the SELECT statement, add `channel_account_id` to the column list:
```sql
SELECT
    id, platform_user_id, account_id, openclaw_login_session_key,
    channel, status, channel_account_id, qr_data_url, manual_login_command,
    raw_result_json, expires_at, completed_at, error, created_at, updated_at
FROM binding_intents
WHERE id = ?
```

Apply the same change to the refreshed SELECT inside the expiry check (Task 3).

- [ ] Update `update_binding_intent` to accept and store `channel_account_id`

Add parameter `channel_account_id: Optional[str] = None` and update the UPDATE statement:

```python
def update_binding_intent(
    *,
    binding_intent_id: str,
    status: Optional[str] = None,
    openclaw_login_session_key: Optional[str] = None,
    channel_account_id: Optional[str] = None,
    qr_data_url: Optional[str] = None,
    raw_result: Optional[Dict[str, Any]] = None,
    completed: bool = False,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_binding_intent(binding_intent_id=binding_intent_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE binding_intents
            SET status = ?,
                openclaw_login_session_key = ?,
                channel_account_id = COALESCE(?, channel_account_id),
                qr_data_url = ?,
                raw_result_json = ?,
                error = ?,
                completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status if status is not None else current["status"],
                openclaw_login_session_key if openclaw_login_session_key is not None else current["openclaw_login_session_key"],
                channel_account_id,
                qr_data_url if qr_data_url is not None else current.get("qr_data_url"),
                json.dumps(raw_result, ensure_ascii=False) if raw_result is not None else json.dumps(current.get("raw_result") or {}, ensure_ascii=False),
                error,
                1 if completed else 0,
                binding_intent_id,
            ),
        )
    return get_binding_intent(binding_intent_id=binding_intent_id)
```

- [ ] Update `_complete_binding_intent_from_wait_result` in `main.py` to pass `channel_account_id`

```python
update_binding_intent(
    binding_intent_id=binding_intent["id"],
    status="completed",
    channel_account_id=channel_account_id,   # <-- add this
    raw_result=raw_result,
    completed=True,
    error=None,
)
```

- [ ] Update `get_active_binding_intent_for_channel_account` to use the column first, fall back to json_extract

```python
def get_active_binding_intent_for_channel_account(
    *,
    channel: str,
    channel_account_id: str,
) -> Optional[Dict[str, Any]]:
    account_aliases = _channel_account_id_aliases(channel_account_id)
    if not account_aliases:
        return None
    placeholders = ", ".join("?" for _ in account_aliases)
    with connect() as conn:
        # Primary lookup: dedicated column (fast, reliable)
        row = conn.execute(
            f"""
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE channel = ?
              AND status = 'completed'
              AND channel_account_id IN ({placeholders})
            ORDER BY completed_at DESC, updated_at DESC
            LIMIT 1
            """,
            (channel, *account_aliases),
        ).fetchone()
        if row is None:
            # Fallback: json_extract for rows written before column was added
            row = conn.execute(
                f"""
                SELECT
                    id, platform_user_id, account_id, openclaw_login_session_key,
                    channel, status, channel_account_id, qr_data_url, manual_login_command,
                    raw_result_json, expires_at, completed_at, error, created_at, updated_at
                FROM binding_intents
                WHERE channel = ?
                  AND status = 'completed'
                  AND json_extract(raw_result_json, '$.channel_account_id') IN ({placeholders})
                ORDER BY completed_at DESC, updated_at DESC
                LIMIT 1
                """,
                (channel, *account_aliases),
            ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
    except json.JSONDecodeError:
        item["raw_result"] = {}
        item["raw_result_decode_error"] = True
    return item
```

Also update the two other SELECT statements in `get_completed_binding_intent_for_openclaw_login_session_key` to include `channel_account_id`.

- [ ] Run tests

```bash
python -m pytest tests/test_web_onboarding.py -v
```

Expected: all pass.

---

## Task 5: Add admin auth to sensitive debug endpoints

**Files:**
- Modify: `app/main.py` — add `Depends(verify_admin_auth)` to 4 endpoints

Sensitive endpoints that expose system prompts, memory, and identity metadata:
- `GET /debug/traces`
- `GET /debug/traces/{trace_id}`
- `GET /debug/accounts/{account_id}/prompt-preview`
- `GET /debug/accounts/{account_id}/user-profile`

Keep pre-existing low-risk endpoints (`/debug/sessions`, `/debug/messages/*`, `/debug/sessions/*/reset`, `/debug/sessions/*/profile`) unauthenticated.

- [ ] Add `_: None = Depends(verify_admin_auth)` to each of the 4 debug endpoints

For example:
```python
@app.get("/debug/traces")
def debug_traces(
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    limit: int = 50,
    _: None = Depends(verify_admin_auth),
) -> dict:
    ...
```

- [ ] Run tests to make sure existing tests that call these endpoints are patched

```bash
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: all pass (existing tests don't call these 4 endpoints without auth; if any fail, add admin auth headers).

---

## Task 6: Fix conftest.py — add explicit OpenClaw settings

**Files:**
- Modify: `tests/conftest.py`

- [ ] Add the following attributes to `test_settings` fixture

```python
s.openclaw_login_auto_start = False
s.openclaw_login_start_timeout_ms = 5000
s.openclaw_login_wait_timeout_ms = 5000
s.openclaw_gateway_call_timeout_ms = 5000
```

- [ ] Run tests

```bash
python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: all pass.

---

## Task 7: Improve phone normalization — strip hyphens and spaces uniformly

**Files:**
- Modify: `app/db.py` — update `_normalize_phone`

- [ ] Update `_normalize_phone` to also strip hyphens and parentheses

```python
def _normalize_phone(phone: str) -> str:
    # Strip all whitespace, hyphens, parentheses, dots
    normalized = str(phone or "")
    for ch in (" ", "-", "(", ")", "."):
        normalized = normalized.replace(ch, "")
    normalized = normalized.strip()
    if len(normalized) < 6 or len(normalized) > 32:
        raise ValueError("phone must be 6-32 characters after stripping spaces and hyphens")
    return normalized
```

- [ ] Update test to verify normalization

In `tests/test_web_onboarding.py`, add:

```python
def test_web_register_normalizes_phone(client):
    res1 = client.post(
        "/web/register",
        json={"phone": "138-0000-0000", "display_name": "Phone Test"},
    )
    assert res1.status_code == 200
    assert res1.json()["platform_user"]["phone"] == "13800000000"

    # Same number with spaces deduplicates to same user
    res2 = client.post(
        "/web/register",
        json={"phone": "138 0000 0000"},
    )
    assert res2.status_code == 200
    assert res2.json()["platform_user"]["id"] == res1.json()["platform_user"]["id"]
```

Wait — the test `test_web_register_creates_and_reuses_platform_user` already uses `13800000000` as raw digits. The normalized form of `138-0000-0000` will be `13800000000` which is the SAME as the previous test's phone. So use a different number in this test.

Corrected test:

```python
def test_web_register_normalizes_phone(client):
    res1 = client.post(
        "/web/register",
        json={"phone": "135-8888-8888", "display_name": "Phone Test"},
    )
    assert res1.status_code == 200
    assert res1.json()["platform_user"]["phone"] == "13588888888"

    res2 = client.post(
        "/web/register",
        json={"phone": "135 8888 8888"},
    )
    assert res2.status_code == 200
    assert res2.json()["platform_user"]["id"] == res1.json()["platform_user"]["id"]
```

- [ ] Run test

```bash
python -m pytest tests/test_web_onboarding.py::test_web_register_normalizes_phone -v
```

Expected: PASS.

---

## Task 8: Fix duplicate channel_bindings — deduplicate by channel_account_id

**Files:**
- Modify: `app/db.py` — update `upsert_channel_binding`

- [ ] After the upsert-by-session-key in `upsert_channel_binding`, add a dedup DELETE for rows with the same channel_account_id but different session_key

```python
# After the INSERT ... ON CONFLICT DO UPDATE block:
if channel_account_id:
    aliases = _channel_account_id_aliases(channel_account_id)
    placeholders = ", ".join("?" for _ in aliases)
    conn.execute(
        f"""
        DELETE FROM channel_bindings
        WHERE account_id = ? AND channel = ?
          AND channel_account_id IN ({placeholders})
          AND session_key != ?
        """,
        (account_id, channel, *aliases, session_key),
    )
```

- [ ] Add test in `tests/test_web_onboarding.py`

```python
def test_channel_binding_deduplicates_by_channel_account_id(client):
    from app.db import (
        get_binding_intent,
        list_channel_bindings_for_account,
        upsert_channel_binding,
    )
    from app.main import _complete_binding_intent_from_wait_result

    user = client.post(
        "/web/register",
        json={"phone": "13800006666"},
    ).json()["platform_user"]
    account = client.post(
        "/web/agents",
        json={"platform_user_id": user["id"], "agent_name": "Dedup Bot"},
    ).json()["account"]
    with patch("app.main._schedule_binding_wait"), patch(
        "app.main.start_weixin_qr_login",
        return_value={"qrDataUrl": "data:image/png;base64,ZmFrZQ==", "sessionKey": "bind-dedup-session"},
    ):
        intent = client.post(
            "/web/binding-intents",
            json={"platform_user_id": user["id"], "account_id": account["id"]},
        ).json()["binding_intent"]
    _complete_binding_intent_from_wait_result(
        get_binding_intent(binding_intent_id=intent["id"]),
        {"connected": True, "accountId": "dedup-bot"},
    )

    # Simulate first inbound message with a different session_key but same channel_account_id
    upsert_channel_binding(
        account_id=account["id"],
        channel="openclaw-weixin",
        session_key="agent:main:openclaw-weixin:dedup-bot:direct:peer",
        channel_account_id="dedup-bot",
        sender_id="peer",
        chat_id="chat-dedup",
    )

    bindings = list_channel_bindings_for_account(account_id=account["id"])
    assert len(bindings) == 1, f"Expected 1 binding, got {len(bindings)}: {bindings}"
    assert bindings[0]["channel_account_id"] == "dedup-bot"
    assert bindings[0]["session_key"] == "agent:main:openclaw-weixin:dedup-bot:direct:peer"
```

- [ ] Run test

```bash
python -m pytest tests/test_web_onboarding.py::test_channel_binding_deduplicates_by_channel_account_id -v
```

Expected: PASS.

---

## Final: Run full test suite

- [ ] Run all tests

```bash
python -m pytest tests/ -v 2>&1 | tail -40
```

Expected: all pass.
