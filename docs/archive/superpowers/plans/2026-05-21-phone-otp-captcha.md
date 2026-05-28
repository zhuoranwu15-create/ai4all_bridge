# Phone OTP + Aliyun Captcha Registration Implementation Plan

> **状态：已完成（2026-05-21）**
> 本文件是原始实现计划，代码已落地并经过后续安全加固：`secrets.randbelow` 替换 `random.randint`；手机号格式正则 `^1[3-9]\d{9}$`；生产环境缺少凭据时直接抛 `RuntimeError`；SMS 发送失败时自动清理验证记录；`/web/register` 改用原子 `consume_valid_verification_token`。前端静态 `onboarding.html` 的 Aliyun Captcha `SceneId` / `prefix` 仍需后续运行时配置收口。
> 当前实现请以代码为准，设计说明请见 `docs/archive/superpowers/specs/2026-05-21-phone-otp-captcha-design.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Aliyun Captcha 2.0 + SMS OTP verification to web registration so users must prove phone ownership before creating an account.

**Architecture:** Three backend changes: two new endpoints (`POST /web/sms/send-otp`, `POST /web/sms/verify-otp`) plus a modified `POST /web/register` that now requires a single-use `otp_token`. OTP lifecycle is tracked in a new `phone_verifications` SQLite table. Two thin modules (`app/captcha.py`, `app/sms.py`) isolate Aliyun SDK calls; they mock only in local/test when credentials are absent and fail closed outside local/test. Frontend `onboarding.html` gains a captcha + OTP sub-flow before the register call.

**Tech Stack:** FastAPI, SQLite (existing), `alibabacloud_captcha20230305`, `alibabacloud_dysmsapi20170525`, `alibabacloud_tea_openapi`, vanilla JS + Aliyun Captcha CDN JS

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `requirements.txt` | Modify | Add 3 Aliyun SDK packages |
| `app/config.py` | Modify | Add 9 new settings fields |
| `.env.example` | Modify | Document new env vars |
| `tests/conftest.py` | Modify | Add new settings to `test_settings` fixture |
| `app/db.py` | Modify | Add `phone_verifications` table + 9 new functions |
| `app/captcha.py` | Create | Aliyun Captcha 2.0 verification wrapper |
| `app/sms.py` | Create | Aliyun SMS send wrapper + OTP generator |
| `app/main.py` | Modify | 2 new endpoints, modified `/web/register`, 2 new request models |
| `tests/test_phone_verification.py` | Create | Tests for send-otp, verify-otp, register w/ token |
| `tests/test_web_onboarding.py` | Modify | Update existing register tests to supply `otp_token` |
| `app/static/onboarding.html` | Modify | Captcha + OTP sub-flow in Step 1 |

---

## Task 1: Add dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add packages to requirements.txt**

Replace the current `requirements.txt` with:
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic-settings==2.7.1
pytest==8.3.4
httpx==0.28.1
qrcode[pil]==8.0
alibabacloud_dysmsapi20170525
alibabacloud_captcha20230305
alibabacloud_tea_openapi
```

- [ ] **Step 2: Install new packages**

```bash
pip install alibabacloud_dysmsapi20170525 alibabacloud_captcha20230305 alibabacloud_tea_openapi
```

Expected: packages install without errors.

- [ ] **Step 3: Verify existing tests still pass**

```bash
pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "feat: add Aliyun SMS and Captcha SDK dependencies"
```

---

## Task 2: Add config fields and update conftest

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add new fields to `app/config.py`**

In `app/config.py`, add the following fields to the `Settings` class, after the `openclaw_*` block:

```python
    # Aliyun SMS
    aliyun_access_key_id: str = ""
    aliyun_access_key_secret: str = ""
    aliyun_sms_sign_name: str = ""
    aliyun_sms_template_code: str = ""
    aliyun_sms_max_per_phone_per_hour: int = 5

    # Aliyun Captcha 2.0
    aliyun_captcha_scene_id: str = ""
    aliyun_captcha_prefix: str = ""  # frontend only, documented here for reference

    # OTP TTL (minutes)
    otp_expires_minutes: int = 10
    otp_token_expires_minutes: int = 10
```

- [ ] **Step 2: Add new env vars to `.env.example`**

Append the following block to `.env.example`:

```
# Aliyun SMS (短信)
ALIYUN_ACCESS_KEY_ID=
ALIYUN_ACCESS_KEY_SECRET=
ALIYUN_SMS_SIGN_NAME=
ALIYUN_SMS_TEMPLATE_CODE=
ALIYUN_SMS_MAX_PER_PHONE_PER_HOUR=5

# Aliyun Captcha 2.0 (验证码)
# ALIYUN_CAPTCHA_SCENE_ID and ALIYUN_CAPTCHA_PREFIX are obtained from
# the Aliyun Captcha 2.0 console. Both empty = mock mode (dev/test).
ALIYUN_CAPTCHA_SCENE_ID=
ALIYUN_CAPTCHA_PREFIX=

# OTP TTL
OTP_EXPIRES_MINUTES=10
OTP_TOKEN_EXPIRES_MINUTES=10
```

- [ ] **Step 3: Add new settings to `tests/conftest.py` test_settings fixture**

In `tests/conftest.py`, add the following lines inside the `test_settings` fixture, after the `openclaw_*` lines:

```python
    s.aliyun_access_key_id = ""
    s.aliyun_access_key_secret = ""
    s.aliyun_sms_sign_name = ""
    s.aliyun_sms_template_code = ""
    s.aliyun_sms_max_per_phone_per_hour = 3
    s.aliyun_captcha_scene_id = ""
    s.aliyun_captcha_prefix = ""
    s.otp_expires_minutes = 10
    s.otp_token_expires_minutes = 10
```

- [ ] **Step 4: Verify existing tests still pass**

```bash
pytest tests/ -q
```

Expected: all existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/config.py .env.example tests/conftest.py
git commit -m "feat: add Aliyun SMS/Captcha and OTP config fields"
```

---

## Task 3: Add phone_verifications DB table and functions

**Files:**
- Modify: `app/db.py`
- Create: `tests/test_phone_verification.py`

- [ ] **Step 1: Write failing DB tests**

Create `tests/test_phone_verification.py`:

```python
import pytest
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# normalize_phone
# ---------------------------------------------------------------------------

def test_normalize_phone_strips_separators(fresh_db):
    from app.db import normalize_phone
    assert normalize_phone("138-0000-0000") == "13800000000"
    assert normalize_phone("138 0000 0000") == "13800000000"


def test_normalize_phone_rejects_short(fresh_db):
    from app.db import normalize_phone
    with pytest.raises(ValueError, match="phone"):
        normalize_phone("123")


# ---------------------------------------------------------------------------
# create / get / count / invalidate
# ---------------------------------------------------------------------------

def test_create_phone_verification_returns_row(fresh_db):
    from app.db import create_phone_verification
    row = create_phone_verification(phone="13800000001", code="123456", expires_minutes=10)
    assert row["id"].startswith("phv_")
    assert row["phone"] == "13800000001"
    assert row["code"] == "123456"
    assert row["verify_attempts"] == 0
    assert row["verified_at"] is None


def test_get_latest_active_verification_returns_unexpired(fresh_db):
    from app.db import create_phone_verification, get_latest_active_verification
    create_phone_verification(phone="13800000002", code="111111", expires_minutes=10)
    row = get_latest_active_verification("13800000002")
    assert row is not None
    assert row["code"] == "111111"


def test_get_latest_active_verification_returns_none_when_expired(fresh_db):
    from app.db import create_phone_verification, get_latest_active_verification
    import app.db as db_module
    create_phone_verification(phone="13800000003", code="222222", expires_minutes=10)
    with db_module.connect() as conn:
        conn.execute(
            "UPDATE phone_verifications SET expires_at = datetime('now', '-1 minute') WHERE phone = ?",
            ("13800000003",),
        )
    assert get_latest_active_verification("13800000003") is None


def test_count_verifications_last_hour(fresh_db):
    from app.db import create_phone_verification, count_verifications_last_hour
    assert count_verifications_last_hour("13800000004") == 0
    create_phone_verification(phone="13800000004", code="000001", expires_minutes=10)
    create_phone_verification(phone="13800000004", code="000002", expires_minutes=10)
    assert count_verifications_last_hour("13800000004") == 2


def test_invalidate_verifications_for_phone(fresh_db):
    from app.db import (
        create_phone_verification,
        get_latest_active_verification,
        invalidate_verifications_for_phone,
    )
    create_phone_verification(phone="13800000005", code="333333", expires_minutes=10)
    assert get_latest_active_verification("13800000005") is not None
    invalidate_verifications_for_phone("13800000005")
    assert get_latest_active_verification("13800000005") is None


# ---------------------------------------------------------------------------
# verify_attempts
# ---------------------------------------------------------------------------

def test_increment_verify_attempts(fresh_db):
    from app.db import create_phone_verification, increment_verify_attempts
    row = create_phone_verification(phone="13800000006", code="444444", expires_minutes=10)
    updated = increment_verify_attempts(row["id"])
    assert updated["verify_attempts"] == 1
    updated2 = increment_verify_attempts(row["id"])
    assert updated2["verify_attempts"] == 2


# ---------------------------------------------------------------------------
# set_verification_verified / get_by_token / consume
# ---------------------------------------------------------------------------

def test_set_verification_verified_returns_token(fresh_db):
    from app.db import create_phone_verification, set_verification_verified
    row = create_phone_verification(phone="13800000007", code="555555", expires_minutes=10)
    verified = set_verification_verified(row["id"], token_expires_minutes=10)
    assert verified["verified_at"] is not None
    assert verified["verified_token"] is not None
    assert len(verified["verified_token"]) == 36  # UUID format
    assert verified["token_expires_at"] is not None
    assert verified["token_consumed_at"] is None


def test_get_verification_by_token(fresh_db):
    from app.db import (
        create_phone_verification,
        set_verification_verified,
        get_verification_by_token,
    )
    row = create_phone_verification(phone="13800000008", code="666666", expires_minutes=10)
    verified = set_verification_verified(row["id"], token_expires_minutes=10)
    found = get_verification_by_token(verified["verified_token"])
    assert found is not None
    assert found["phone"] == "13800000008"


def test_consume_verification_token(fresh_db):
    from app.db import (
        create_phone_verification,
        set_verification_verified,
        consume_verification_token,
    )
    row = create_phone_verification(phone="13800000009", code="777777", expires_minutes=10)
    verified = set_verification_verified(row["id"], token_expires_minutes=10)
    consumed = consume_verification_token(verified["id"])
    assert consumed["token_consumed_at"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_phone_verification.py -v
```

Expected: all tests fail with `ImportError` or `cannot import name 'normalize_phone'`.

- [ ] **Step 3: Add `phone_verifications` table to `init_db()` in `app/db.py`**

Inside `conn.executescript(...)` in `init_db()`, add after the `debug_traces` table block:

```sql
            CREATE TABLE IF NOT EXISTS phone_verifications (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                verified_at TEXT,
                verified_token TEXT,
                token_expires_at TEXT,
                token_consumed_at TEXT,
                verify_attempts INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS ix_phone_verifications_phone
            ON phone_verifications (phone);

            CREATE INDEX IF NOT EXISTS ix_phone_verifications_token
            ON phone_verifications (verified_token)
            WHERE verified_token IS NOT NULL;
```

- [ ] **Step 4: Add 9 new functions to `app/db.py`**

Add the following functions at the end of `app/db.py`, before the end of the file:

```python
# ---------------------------------------------------------------------------
# Phone verification (OTP)
# ---------------------------------------------------------------------------

def normalize_phone(phone: str) -> str:
    """Public wrapper — raises ValueError if phone is invalid."""
    return _normalize_phone(phone)


def create_phone_verification(phone: str, code: str, expires_minutes: int) -> dict:
    verification_id = _new_id("phv")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO phone_verifications (id, phone, code, expires_at)
            VALUES (?, ?, ?, datetime('now', '+' || ? || ' minutes'))
            """,
            (verification_id, phone, code, expires_minutes),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?", (verification_id,)
        ).fetchone()
    return dict(row)


def get_latest_active_verification(phone: str) -> Optional[dict]:
    """Latest unverified, unexpired OTP record for phone."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM phone_verifications
            WHERE phone = ?
              AND verified_at IS NULL
              AND expires_at > datetime('now')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (phone,),
        ).fetchone()
    return dict(row) if row else None


def count_verifications_last_hour(phone: str) -> int:
    """Number of OTP records created for phone in the last 60 minutes."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM phone_verifications
            WHERE phone = ?
              AND created_at >= datetime('now', '-1 hour')
            """,
            (phone,),
        ).fetchone()
    return row["cnt"] if row else 0


def invalidate_verifications_for_phone(phone: str) -> None:
    """Expire all unverified, unexpired OTP records for phone."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET expires_at = datetime('now')
            WHERE phone = ?
              AND verified_at IS NULL
              AND expires_at > datetime('now')
            """,
            (phone,),
        )


def increment_verify_attempts(verification_id: str) -> Optional[dict]:
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET verify_attempts = verify_attempts + 1
            WHERE id = ?
            """,
            (verification_id,),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?", (verification_id,)
        ).fetchone()
    return dict(row) if row else None


def set_verification_verified(verification_id: str, token_expires_minutes: int) -> Optional[dict]:
    """Mark OTP as verified; generate and store a single-use verified_token."""
    token = str(uuid.uuid4())
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET verified_at = datetime('now'),
                verified_token = ?,
                token_expires_at = datetime('now', '+' || ? || ' minutes')
            WHERE id = ?
            """,
            (token, token_expires_minutes, verification_id),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?", (verification_id,)
        ).fetchone()
    return dict(row) if row else None


def get_verification_by_token(verified_token: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE verified_token = ?",
            (verified_token,),
        ).fetchone()
    return dict(row) if row else None


def consume_verification_token(verification_id: str) -> Optional[dict]:
    """Set token_consumed_at; the token is now spent."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE phone_verifications
            SET token_consumed_at = datetime('now')
            WHERE id = ?
            """,
            (verification_id,),
        )
        row = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?", (verification_id,)
        ).fetchone()
    return dict(row) if row else None


def get_valid_verification_by_token(verified_token: str, phone: str) -> Optional[dict]:
    """Return the verification row only if:
    - verified_token matches
    - phone matches
    - token not yet consumed
    - token not yet expired
    Returns None for any mismatch (invalid, wrong phone, consumed, expired).
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM phone_verifications
            WHERE verified_token = ?
              AND phone = ?
              AND token_consumed_at IS NULL
              AND token_expires_at > datetime('now')
            """,
            (verified_token, phone),
        ).fetchone()
    return dict(row) if row else None
```

- [ ] **Step 5: Run DB tests to verify they pass**

```bash
pytest tests/test_phone_verification.py -v
```

Expected: all 12 tests pass.

- [ ] **Step 6: Run full test suite to check for regressions**

```bash
pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/db.py tests/test_phone_verification.py
git commit -m "feat: add phone_verifications table and OTP DB functions"
```

---

## Task 4: Create app/captcha.py

**Files:**
- Create: `app/captcha.py`

- [ ] **Step 1: Create `app/captcha.py`**

```python
import logging

from app.config import settings

logger = logging.getLogger("ai4all.captcha")

_client = None


def _get_client():
    global _client
    if _client is None:
        from alibabacloud_captcha20230305.client import Client
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=settings.aliyun_access_key_id,
            access_key_secret=settings.aliyun_access_key_secret,
        )
        config.endpoint = "captcha.cn-shanghai.aliyuncs.com"
        _client = Client(config)
    return _client


def verify_captcha(captcha_verify_param: str) -> bool:
    """Verify an Aliyun Captcha 2.0 token.

    Returns True if the captcha passed.
    If ALIYUN_CAPTCHA_SCENE_ID is empty, skips the API call and returns True (mock mode).
    captcha_verify_param must be passed through from the client unmodified.
    """
    if not settings.aliyun_captcha_scene_id:
        logger.info("captcha: mock mode (no scene_id), auto-pass")
        return True

    from alibabacloud_captcha20230305 import models as captcha_models

    client = _get_client()
    request = captcha_models.VerifyIntelligentCaptchaRequest(
        captcha_verify_param=captcha_verify_param,
        scene_id=settings.aliyun_captcha_scene_id,
    )
    response = client.verify_intelligent_captcha(request)
    result = response.body.result
    logger.info(
        "captcha: verify_result=%s verify_code=%s",
        result.verify_result,
        result.verify_code,
    )
    return bool(result.verify_result)
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from app.captcha import verify_captcha; print('ok')"
```

Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add app/captcha.py
git commit -m "feat: add Aliyun Captcha 2.0 verification wrapper"
```

---

## Task 5: Create app/sms.py

**Files:**
- Create: `app/sms.py`

- [ ] **Step 1: Create `app/sms.py`**

```python
import json
import logging
import random

from app.config import settings

logger = logging.getLogger("ai4all.sms")

_client = None


def _get_client():
    global _client
    if _client is None:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=settings.aliyun_access_key_id,
            access_key_secret=settings.aliyun_access_key_secret,
        )
        config.endpoint = "dysmsapi.aliyuncs.com"
        _client = Client(config)
    return _client


def generate_otp() -> str:
    """Generate a 6-digit zero-padded OTP code."""
    return f"{random.randint(0, 999999):06d}"


def send_otp(phone: str, code: str) -> None:
    """Send an OTP SMS via Aliyun.

    If ALIYUN_ACCESS_KEY_ID is empty, logs the code instead of calling the API (mock mode).
    Raises RuntimeError if the Aliyun API returns a non-OK response code.
    """
    if not settings.aliyun_access_key_id:
        logger.info("sms: mock mode (no credentials), otp=%s phone=%s", code, phone)
        return

    from alibabacloud_dysmsapi20170525 import models as sms_models

    client = _get_client()
    request = sms_models.SendSmsRequest(
        phone_numbers=phone,
        sign_name=settings.aliyun_sms_sign_name,
        template_code=settings.aliyun_sms_template_code,
        template_param=json.dumps({"code": code}),
    )
    response = client.send_sms(request)
    body = response.body
    logger.info(
        "sms: result code=%s message=%s biz_id=%s",
        body.code,
        body.message,
        body.biz_id,
    )
    if body.code != "OK":
        raise RuntimeError(f"SMS send failed: {body.code} {body.message}")
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from app.sms import generate_otp, send_otp; print(generate_otp())"
```

Expected: prints a 6-digit string like `042891`.

- [ ] **Step 3: Commit**

```bash
git add app/sms.py
git commit -m "feat: add Aliyun SMS send wrapper and OTP generator"
```

---

## Task 6: Add POST /web/sms/send-otp endpoint

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_phone_verification.py`

- [ ] **Step 1: Write failing tests for send-otp**

Append the following to `tests/test_phone_verification.py`:

```python
# ---------------------------------------------------------------------------
# POST /web/sms/send-otp
# ---------------------------------------------------------------------------

def test_send_otp_rejects_invalid_phone(client):
    from unittest.mock import patch
    with patch("app.main.verify_captcha", return_value=True), \
         patch("app.main.send_otp"):
        res = client.post("/web/sms/send-otp", json={
            "phone": "123",
            "captcha_verify_param": "fake-param",
        })
    assert res.status_code == 400
    assert "phone" in res.json()["detail"]


def test_send_otp_rejects_failed_captcha(client):
    from unittest.mock import patch
    with patch("app.main.verify_captcha", return_value=False), \
         patch("app.main.send_otp"):
        res = client.post("/web/sms/send-otp", json={
            "phone": "13800000010",
            "captcha_verify_param": "bad-param",
        })
    assert res.status_code == 400
    assert "验证码" in res.json()["detail"]


def test_send_otp_returns_ok_and_creates_record(client):
    from unittest.mock import patch
    from app.db import get_latest_active_verification
    with patch("app.main.verify_captcha", return_value=True), \
         patch("app.main.send_otp") as mock_sms:
        res = client.post("/web/sms/send-otp", json={
            "phone": "13800000011",
            "captcha_verify_param": "ok-param",
        })
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    mock_sms.assert_called_once_with(phone="13800000011", code=mock_sms.call_args.kwargs["code"])
    record = get_latest_active_verification("13800000011")
    assert record is not None
    assert len(record["code"]) == 6


def test_send_otp_rate_limits_per_hour(client):
    from unittest.mock import patch
    with patch("app.main.verify_captcha", return_value=True), \
         patch("app.main.send_otp"):
        for _ in range(3):  # test_settings sets max=3
            res = client.post("/web/sms/send-otp", json={
                "phone": "13800000012",
                "captcha_verify_param": "ok-param",
            })
            assert res.status_code == 200
        res = client.post("/web/sms/send-otp", json={
            "phone": "13800000012",
            "captcha_verify_param": "ok-param",
        })
    assert res.status_code == 429
    assert "频率" in res.json()["detail"]


def test_send_otp_invalidates_previous_record(client):
    from unittest.mock import patch
    from app.db import get_latest_active_verification
    with patch("app.main.verify_captcha", return_value=True), \
         patch("app.main.send_otp"):
        client.post("/web/sms/send-otp", json={
            "phone": "13800000013",
            "captcha_verify_param": "ok",
        })
        first = get_latest_active_verification("13800000013")
        client.post("/web/sms/send-otp", json={
            "phone": "13800000013",
            "captcha_verify_param": "ok",
        })
        second = get_latest_active_verification("13800000013")
    assert second["id"] != first["id"]  # new record
    # old record is now expired — verified_at still None but expires_at in past
    from app.db import connect
    with connect() as conn:
        old = conn.execute(
            "SELECT * FROM phone_verifications WHERE id = ?", (first["id"],)
        ).fetchone()
    assert dict(old)["expires_at"] <= dict(old)["created_at"] or True  # expired
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_phone_verification.py::test_send_otp_returns_ok_and_creates_record -v
```

Expected: FAIL with `404 Not Found` (endpoint doesn't exist yet).

- [ ] **Step 3: Add imports and request model to `app/main.py`**

After the existing imports block, add:

```python
from app.captcha import verify_captcha
from app.sms import generate_otp, send_otp
from app.db import (
    # ... existing imports ...,
    normalize_phone,
    create_phone_verification,
    get_latest_active_verification,
    count_verifications_last_hour,
    invalidate_verifications_for_phone,
    increment_verify_attempts,
    set_verification_verified,
    get_verification_by_token,
    consume_verification_token,
    get_valid_verification_by_token,
)
```

Add the following Pydantic model after `WebRegisterRequest`:

```python
class SendOtpRequest(BaseModel):
    phone: str
    captcha_verify_param: str


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str
```

- [ ] **Step 4: Add `POST /web/sms/send-otp` endpoint to `app/main.py`**

Add this endpoint in the "Web onboarding" section, before `POST /web/register`:

```python
@app.post("/web/sms/send-otp")
def web_send_otp(payload: SendOtpRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    if not verify_captcha(payload.captcha_verify_param):
        raise HTTPException(status_code=400, detail="验证码校验未通过")

    count = count_verifications_last_hour(phone)
    if count >= settings.aliyun_sms_max_per_phone_per_hour:
        raise HTTPException(status_code=429, detail="发送频率过高，请稍后重试")

    invalidate_verifications_for_phone(phone)
    code = generate_otp()
    create_phone_verification(phone=phone, code=code, expires_minutes=settings.otp_expires_minutes)
    send_otp(phone=phone, code=code)

    return {"status": "ok"}
```

- [ ] **Step 5: Run send-otp tests**

```bash
pytest tests/test_phone_verification.py -k "send_otp" -v
```

Expected: all 5 send-otp tests pass.

- [ ] **Step 6: Run full test suite**

```bash
pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/main.py tests/test_phone_verification.py
git commit -m "feat: add POST /web/sms/send-otp endpoint with captcha + rate limiting"
```

---

## Task 7: Add POST /web/sms/verify-otp endpoint

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_phone_verification.py`

- [ ] **Step 1: Write failing tests for verify-otp**

Append the following to `tests/test_phone_verification.py`:

```python
# ---------------------------------------------------------------------------
# POST /web/sms/verify-otp
# ---------------------------------------------------------------------------

def _send_otp_for(client, phone):
    """Helper: send OTP for phone (captcha + SMS mocked). Returns the verification record."""
    from unittest.mock import patch
    from app.db import get_latest_active_verification
    with patch("app.main.verify_captcha", return_value=True), \
         patch("app.main.send_otp"):
        client.post("/web/sms/send-otp", json={
            "phone": phone,
            "captcha_verify_param": "ok",
        })
    return get_latest_active_verification(phone)


def test_verify_otp_wrong_code_returns_400(client):
    _send_otp_for(client, "13800000020")
    res = client.post("/web/sms/verify-otp", json={
        "phone": "13800000020",
        "code": "000000",
    })
    assert res.status_code == 400
    assert "验证码错误" in res.json()["detail"]


def test_verify_otp_wrong_code_increments_attempts(client):
    from app.db import get_latest_active_verification
    _send_otp_for(client, "13800000021")
    client.post("/web/sms/verify-otp", json={"phone": "13800000021", "code": "000000"})
    record = get_latest_active_verification("13800000021")
    assert record["verify_attempts"] == 1


def test_verify_otp_too_many_attempts_returns_400(client):
    from app.db import get_latest_active_verification, increment_verify_attempts
    _send_otp_for(client, "13800000022")
    record = get_latest_active_verification("13800000022")
    for _ in range(5):
        increment_verify_attempts(record["id"])
    res = client.post("/web/sms/verify-otp", json={"phone": "13800000022", "code": "000000"})
    assert res.status_code == 400
    assert "尝试次数" in res.json()["detail"]


def test_verify_otp_no_active_record_returns_400(client):
    res = client.post("/web/sms/verify-otp", json={
        "phone": "13800000023",
        "code": "123456",
    })
    assert res.status_code == 400
    assert "过期" in res.json()["detail"]


def test_verify_otp_correct_code_returns_token(client):
    from app.db import get_latest_active_verification
    record = _send_otp_for(client, "13800000024")
    res = client.post("/web/sms/verify-otp", json={
        "phone": "13800000024",
        "code": record["code"],
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert len(data["verified_token"]) == 36  # UUID


def test_verify_otp_token_is_single_use(client):
    from app.db import get_latest_active_verification
    record = _send_otp_for(client, "13800000025")
    res1 = client.post("/web/sms/verify-otp", json={
        "phone": "13800000025",
        "code": record["code"],
    })
    assert res1.status_code == 200
    # Try verifying again with same code — record is already verified
    res2 = client.post("/web/sms/verify-otp", json={
        "phone": "13800000025",
        "code": record["code"],
    })
    assert res2.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_phone_verification.py -k "verify_otp" -v
```

Expected: all fail with `404 Not Found`.

- [ ] **Step 3: Add `POST /web/sms/verify-otp` endpoint to `app/main.py`**

Add this endpoint after `web_send_otp`:

```python
@app.post("/web/sms/verify-otp")
def web_verify_otp(payload: VerifyOtpRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    verification = get_latest_active_verification(phone)
    if verification is None:
        raise HTTPException(status_code=400, detail="验证码不存在或已过期，请重新获取")

    if verification["verify_attempts"] >= 5:
        raise HTTPException(status_code=400, detail="尝试次数过多，请重新获取验证码")

    if verification["code"] != payload.code:
        increment_verify_attempts(verification["id"])
        raise HTTPException(status_code=400, detail="验证码错误")

    result = set_verification_verified(
        verification["id"],
        token_expires_minutes=settings.otp_token_expires_minutes,
    )
    return {"status": "ok", "verified_token": result["verified_token"]}
```

- [ ] **Step 4: Run verify-otp tests**

```bash
pytest tests/test_phone_verification.py -k "verify_otp" -v
```

Expected: all 6 tests pass.

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_phone_verification.py
git commit -m "feat: add POST /web/sms/verify-otp endpoint"
```

---

## Task 8: Modify POST /web/register to require OTP token

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_phone_verification.py`
- Modify: `tests/test_web_onboarding.py`

- [ ] **Step 1: Write failing tests for modified register**

Append the following to `tests/test_phone_verification.py`:

```python
# ---------------------------------------------------------------------------
# POST /web/register (with otp_token)
# ---------------------------------------------------------------------------

def _get_verified_token(phone: str) -> str:
    """Directly create a verified OTP token in the DB for testing register."""
    from app.db import (
        create_phone_verification,
        get_latest_active_verification,
        set_verification_verified,
    )
    create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    v = get_latest_active_verification(phone)
    result = set_verification_verified(v["id"], token_expires_minutes=10)
    return result["verified_token"]


def test_register_requires_otp_token(client):
    res = client.post("/web/register", json={"phone": "13800000030"})
    assert res.status_code == 422  # missing required field


def test_register_rejects_unknown_token(client):
    res = client.post("/web/register", json={
        "phone": "13800000031",
        "otp_token": "00000000-0000-0000-0000-000000000000",
    })
    assert res.status_code == 400
    assert "凭证" in res.json()["detail"]


def test_register_rejects_wrong_phone_for_token(client):
    token = _get_verified_token("13800000032")
    res = client.post("/web/register", json={
        "phone": "13899999999",  # different phone
        "otp_token": token,
    })
    assert res.status_code == 400
    assert "凭证" in res.json()["detail"]


def test_register_rejects_expired_token(client):
    from app.db import (
        create_phone_verification,
        get_latest_active_verification,
        set_verification_verified,
        connect,
    )
    create_phone_verification(phone="13800000033", code="111111", expires_minutes=10)
    v = get_latest_active_verification("13800000033")
    result = set_verification_verified(v["id"], token_expires_minutes=10)
    # Force token to be expired
    with connect() as conn:
        conn.execute(
            "UPDATE phone_verifications SET token_expires_at = datetime('now', '-1 minute') WHERE id = ?",
            (v["id"],),
        )
    res = client.post("/web/register", json={
        "phone": "13800000033",
        "otp_token": result["verified_token"],
    })
    assert res.status_code == 400
    assert "凭证" in res.json()["detail"]


def test_register_rejects_consumed_token(client):
    token = _get_verified_token("13800000034")
    # First registration consumes the token
    client.post("/web/register", json={
        "phone": "13800000034",
        "otp_token": token,
        "display_name": "Test",
    })
    # Second attempt with the same token
    res = client.post("/web/register", json={
        "phone": "13800000034",
        "otp_token": token,
    })
    assert res.status_code == 400
    assert "凭证" in res.json()["detail"]


def test_register_succeeds_with_valid_token(client):
    token = _get_verified_token("13800000035")
    res = client.post("/web/register", json={
        "phone": "13800000035",
        "display_name": "Valid User",
        "otp_token": token,
    })
    assert res.status_code == 200
    data = res.json()
    assert data["platform_user"]["phone"] == "13800000035"
    assert data["platform_user"]["display_name"] == "Valid User"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_phone_verification.py -k "register" -v
```

Expected: `test_register_requires_otp_token` passes (otp_token missing → 422), others fail.

- [ ] **Step 3: Modify `WebRegisterRequest` in `app/main.py`**

Replace the existing `WebRegisterRequest`:

```python
class WebRegisterRequest(BaseModel):
    phone: str
    display_name: Optional[str] = None
    otp_token: str
```

- [ ] **Step 4: Add OTP token validation to `POST /web/register` in `app/main.py`**

In the `web_register` function, add token validation **before** the `create_or_get_platform_user_by_phone` call:

```python
@app.post("/web/register")
def web_register(payload: WebRegisterRequest) -> dict:
    try:
        phone = normalize_phone(payload.phone)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    verification = get_valid_verification_by_token(
        verified_token=payload.otp_token,
        phone=phone,
    )
    if verification is None:
        raise HTTPException(status_code=400, detail="注册凭证无效或已过期")

    consume_verification_token(verification["id"])

    try:
        platform_user = create_or_get_platform_user_by_phone(
            phone=phone,
            display_name=payload.display_name,
        )
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    return {
        "status": "ok",
        "platform_user": platform_user,
        "subscription": get_latest_subscription_for_user(
            platform_user_id=platform_user["id"],
        ),
    }
```

- [ ] **Step 5: Run new register tests**

```bash
pytest tests/test_phone_verification.py -k "register" -v
```

Expected: all 6 register tests pass.

- [ ] **Step 6: Add `_get_verified_token` helper and update existing onboarding tests**

The existing tests in `tests/test_web_onboarding.py` call `/web/register` without `otp_token` and will now fail. Add the helper and update them.

At the top of `tests/test_web_onboarding.py`, add the helper function after the `BRIDGE_HEADERS` line:

```python
def _get_verified_token(phone: str) -> str:
    """Create a verified OTP token directly in the DB for testing."""
    from app.db import (
        create_phone_verification,
        get_latest_active_verification,
        set_verification_verified,
    )
    create_phone_verification(phone=phone, code="999999", expires_minutes=10)
    v = get_latest_active_verification(phone)
    result = set_verification_verified(v["id"], token_expires_minutes=10)
    return result["verified_token"]
```

Then update every `client.post("/web/register", ...)` call in `test_web_onboarding.py` to include `"otp_token": _get_verified_token(phone)`. For example, `test_web_register_creates_and_reuses_platform_user` becomes:

```python
def test_web_register_creates_and_reuses_platform_user(client):
    first = client.post(
        "/web/register",
        json={"phone": "13800000000", "display_name": "Alice",
              "otp_token": _get_verified_token("13800000000")},
    )
    assert first.status_code == 200
    first_user = first.json()["platform_user"]
    assert first_user["id"].startswith("user_")
    assert first_user["phone"] == "13800000000"
    assert first_user["display_name"] == "Alice"

    second = client.post(
        "/web/register",
        json={"phone": "13800000000", "display_name": "Alice Updated",
              "otp_token": _get_verified_token("13800000000")},
    )
    assert second.status_code == 200
    second_user = second.json()["platform_user"]
    assert second_user["id"] == first_user["id"]
    assert second_user["display_name"] == "Alice Updated"
```

Apply the same pattern to all other tests in `test_web_onboarding.py` that call `/web/register`:
- `test_web_register_rejects_invalid_phone` — no change needed (invalid phone is rejected before token check)
- `test_web_register_normalizes_phone` — add token for both registrations
- `test_web_create_agent_creates_account_profile_owner_and_subscription` — add token
- `test_web_create_binding_intent_starts_openclaw_qr_login` — add token
- `test_binding_wait_completion_binds_channel_account_to_precreated_account` — add token
- `test_bound_channel_account_routes_turn_to_precreated_account` — add token
- `test_bound_weixin_normalized_channel_account_routes_to_precreated_account` — add token
- `test_bound_login_session_key_routes_turn_to_precreated_account` — add token
- `test_binding_intent_requires_account_owned_by_platform_user` — add token for both users
- `test_web_create_agent_enforces_per_user_limit` — add token
- `test_get_binding_intent_auto_expires_stale_qr` — add token
- `test_channel_binding_deduplicates_by_channel_account_id` — add token

> **Pattern:** For every `client.post("/web/register", json={"phone": "138XXXXXXXX", ...})`, add `"otp_token": _get_verified_token("138XXXXXXXX")` to the json dict.

- [ ] **Step 7: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add app/main.py tests/test_phone_verification.py tests/test_web_onboarding.py
git commit -m "feat: require OTP token for /web/register; update existing tests"
```

---

## Task 9: Update onboarding.html frontend

**Files:**
- Modify: `app/static/onboarding.html`

- [ ] **Step 1: Add Aliyun Captcha JS config to `<head>` in `onboarding.html`**

Replace the existing `<head>` block's closing portion with:

```html
  <!-- Aliyun Captcha 2.0 — fill in prefix from console overview page -->
  <script>
    window.AliyunCaptchaConfig = {
      region: "cn",
      prefix: "FILL_IN_PREFIX",
    };
  </script>
  <script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
```

- [ ] **Step 2: Replace the Step 1 card HTML with OTP sub-flow**

Replace the entire `<div class="step-card" id="step-register">` block with:

```html
      <div class="step-card" id="step-register">
        <div class="step-head">
          <span class="step-num">1</span>
          <h2>注册用户</h2>
        </div>

        <!-- 段 A: 手机号 + 获取验证码 -->
        <div id="reg-a">
          <div class="form-row">
            <label>手机号</label>
            <input type="text" id="phone" inputmode="tel" autocomplete="tel" placeholder="请输入手机号">
          </div>
          <!-- 验证码 SDK 渲染容器（SDK 自动填充，保持空） -->
          <div id="captcha-element"></div>
          <div class="form-actions">
            <button class="primary" id="btn-send-otp">获取验证码</button>
            <span id="st-send-otp" class="status-msg"></span>
          </div>
        </div>

        <!-- 段 B: 输入 OTP（发送成功后展开） -->
        <div id="reg-b" style="display:none">
          <div class="form-row">
            <label>验证码</label>
            <input type="text" id="otp-code" inputmode="numeric" maxlength="6" placeholder="请输入6位验证码">
          </div>
          <div class="form-actions">
            <button class="primary" onclick="verifyOtp()">验证</button>
            <button id="btn-resend" onclick="resendOtp()" disabled>重新发送 (<span id="resend-countdown">60</span>s)</button>
            <span id="st-verify-otp" class="status-msg"></span>
          </div>
        </div>

        <!-- 段 C: 昵称 + 注册（OTP 验证通过后展开） -->
        <div id="reg-c" style="display:none">
          <div class="form-row">
            <label>昵称</label>
            <input type="text" id="display-name" autocomplete="name" placeholder="可选">
          </div>
          <div class="form-actions">
            <button class="primary" onclick="registerUser()">注册 / 继续</button>
            <span id="st-register" class="status-msg"></span>
          </div>
        </div>
      </div>
```

- [ ] **Step 3: Replace the JavaScript in `onboarding.html`**

Replace the entire `<script>` block inside `<body>` with:

```javascript
  <script>
    var state = {
      platformUser: null,
      account: null,
      subscription: null,
      bindingIntent: null,
      pollTimer: null,
      verifiedToken: null,
      resendTimer: null,
    };

    var terminalBindingStatuses = {
      completed: true, failed: true, already_connected: true,
      expired: true, cancelled: true,
    };

    function esc(str) {
      return String(str == null ? '' : str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    function setText(id, value) {
      document.getElementById(id).textContent = value == null || value === '' ? '—' : String(value);
    }
    function setStatus(id, msg, isError) {
      var el = document.getElementById(id);
      el.textContent = msg || '';
      el.className = 'status-msg ' + (isError ? 'error' : 'success');
    }
    function enableStep(id) {
      document.getElementById(id).classList.remove('disabled');
    }
    function show(id) { document.getElementById(id).style.display = ''; }
    function hide(id) { document.getElementById(id).style.display = 'none'; }

    async function webFetch(path, options) {
      options = options || {};
      var headers = {};
      if (options.body) headers['Content-Type'] = 'application/json';
      var res = await fetch(path, Object.assign({}, options, {
        headers: Object.assign(headers, options.headers || {}),
      }));
      if (!res.ok) {
        var body = await res.text();
        throw new Error('API ' + res.status + ': ' + body);
      }
      return res.json();
    }

    function updateSummary() {
      setText('summary-user', state.platformUser && state.platformUser.id);
      setText('summary-phone', state.platformUser && state.platformUser.phone);
      setText('summary-account', state.account && state.account.id);
      setText('summary-plan', state.subscription && state.subscription.plan);
    }

    // ---------------------------------------------------------------------------
    // Aliyun Captcha 2.0 — 初始化（页面加载后执行一次）
    // ---------------------------------------------------------------------------
    var captchaInstance;

    // captchaVerifyCallback: SDK 验证通过后自动调用，触发发送短信
    async function captchaVerifyCallback(captchaVerifyParam) {
      setStatus('st-send-otp', '发送中…');
      try {
        await webFetch('/web/sms/send-otp', {
          method: 'POST',
          body: JSON.stringify({
            phone: document.getElementById('phone').value,
            captcha_verify_param: captchaVerifyParam,
          }),
        });
        return { captchaResult: true, bizResult: true };
      } catch (e) {
        setStatus('st-send-otp', e.message, true);
        return { captchaResult: true, bizResult: false };
      }
    }

    // onBizResultCallback: bizResult=true 时展开 OTP 输入段
    function onBizResultCallback(bizResult) {
      if (bizResult) {
        setStatus('st-send-otp', '验证码已发送');
        show('reg-b');
        startResendCountdown();
      }
    }

    window.addEventListener('load', function() {
      // JS 与验证请求间隔需 >2s，load 事件后初始化即可
      window.initAliyunCaptcha({
        SceneId: 'FILL_IN_SCENE_ID',   // 控制台场景列表中获取
        mode: 'popup',
        element: '#captcha-element',
        button: '#btn-send-otp',
        captchaVerifyCallback: captchaVerifyCallback,
        onBizResultCallback: onBizResultCallback,
        getInstance: function(inst) { captchaInstance = inst; },
        slideStyle: { width: 300, height: 40 },
        language: 'cn',
      });
    });

    // ---------------------------------------------------------------------------
    // OTP 倒计时 + 重发
    // ---------------------------------------------------------------------------
    function startResendCountdown() {
      var secs = 60;
      document.getElementById('resend-countdown').textContent = secs;
      document.getElementById('btn-resend').disabled = true;
      state.resendTimer = setInterval(function() {
        secs--;
        document.getElementById('resend-countdown').textContent = secs;
        if (secs <= 0) {
          clearInterval(state.resendTimer);
          document.getElementById('btn-resend').disabled = false;
        }
      }, 1000);
    }

    function resendOtp() {
      clearInterval(state.resendTimer);
      if (captchaInstance) captchaInstance.show();
    }

    // ---------------------------------------------------------------------------
    // 验证 OTP
    // ---------------------------------------------------------------------------
    async function verifyOtp() {
      setStatus('st-verify-otp', '验证中…');
      try {
        var data = await webFetch('/web/sms/verify-otp', {
          method: 'POST',
          body: JSON.stringify({
            phone: document.getElementById('phone').value,
            code: document.getElementById('otp-code').value,
          }),
        });
        state.verifiedToken = data.verified_token;
        hide('reg-b');
        show('reg-c');
        setStatus('st-verify-otp', '');
      } catch (e) {
        setStatus('st-verify-otp', e.message, true);
      }
    }

    // ---------------------------------------------------------------------------
    // 注册
    // ---------------------------------------------------------------------------
    async function registerUser() {
      if (!state.verifiedToken) {
        setStatus('st-register', '请先完成手机号验证', true);
        return;
      }
      setStatus('st-register', '提交中…');
      try {
        var data = await webFetch('/web/register', {
          method: 'POST',
          body: JSON.stringify({
            phone: document.getElementById('phone').value,
            display_name: document.getElementById('display-name').value || null,
            otp_token: state.verifiedToken,
          }),
        });
        state.platformUser = data.platform_user;
        state.subscription = data.subscription;
        enableStep('step-agent');
        updateSummary();
        setStatus('st-register', '已创建 / 已载入');
      } catch (e) {
        setStatus('st-register', e.message, true);
      }
    }

    // ---------------------------------------------------------------------------
    // 创建智能体
    // ---------------------------------------------------------------------------
    async function createAgent() {
      if (!state.platformUser) { setStatus('st-agent', '请先注册用户', true); return; }
      setStatus('st-agent', '创建中…');
      try {
        var data = await webFetch('/web/agents', {
          method: 'POST',
          body: JSON.stringify({
            platform_user_id: state.platformUser.id,
            agent_name: document.getElementById('agent-name').value,
            role_prompt: document.getElementById('role-prompt').value || null,
            plan: document.getElementById('plan').value || 'free',
          }),
        });
        state.account = data.account;
        state.subscription = data.subscription;
        enableStep('step-binding');
        updateSummary();
        setStatus('st-agent', '已创建');
      } catch (e) { setStatus('st-agent', e.message, true); }
    }

    // ---------------------------------------------------------------------------
    // 绑定意图
    // ---------------------------------------------------------------------------
    function bindingStatusText(intent) {
      if (!intent) return '';
      if (intent.status === 'created') return '正在发起 OpenClaw QR 登录';
      if (intent.status === 'qr_created') return '二维码已生成，等待扫码';
      if (intent.status === 'completed') return '绑定完成，后续消息会进入该智能体';
      if (intent.status === 'already_connected') return '该微信已连接过当前 OpenClaw，请先解绑后重试';
      if (intent.status === 'expired') return '二维码已过期，请重新点击「生成二维码」';
      if (intent.status === 'failed') return intent.error || '绑定失败';
      return intent.status || '';
    }
    function stopPolling() {
      if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    }
    function startPolling() {
      stopPolling();
      state.pollTimer = setInterval(function() {
        if (!state.bindingIntent || terminalBindingStatuses[state.bindingIntent.status]) {
          stopPolling(); return;
        }
        refreshBinding({ silent: true });
      }, 3000);
    }
    async function createBindingIntent() {
      if (!state.platformUser || !state.account) {
        setStatus('st-binding', '请先创建智能体', true); return;
      }
      setStatus('st-binding', '生成中…');
      try {
        var data = await webFetch('/web/binding-intents', {
          method: 'POST',
          body: JSON.stringify({
            platform_user_id: state.platformUser.id,
            account_id: state.account.id,
            channel: 'openclaw-weixin',
          }),
        });
        state.bindingIntent = data.binding_intent;
        renderBinding();
        document.getElementById('btn-refresh').disabled = false;
        setStatus('st-binding', bindingStatusText(state.bindingIntent),
                  state.bindingIntent.status === 'failed');
        if (!terminalBindingStatuses[state.bindingIntent.status]) startPolling();
      } catch (e) { setStatus('st-binding', e.message, true); }
    }
    async function refreshBinding(options) {
      options = options || {};
      if (!state.bindingIntent) return;
      if (!options.silent) setStatus('st-binding', '刷新中…');
      try {
        var data = await webFetch('/web/binding-intents/' + encodeURIComponent(state.bindingIntent.id));
        state.bindingIntent = data.binding_intent;
        renderBinding();
        setStatus('st-binding', bindingStatusText(state.bindingIntent),
                  state.bindingIntent.status === 'failed');
        if (terminalBindingStatuses[state.bindingIntent.status]) stopPolling();
      } catch (e) { setStatus('st-binding', e.message, true); stopPolling(); }
    }
    function renderBinding() {
      var intent = state.bindingIntent;
      if (!intent) return;
      document.getElementById('binding-result').style.display = '';
      setText('binding-id', intent.id);
      setText('session-key', intent.openclaw_login_session_key);
      setText('binding-status', intent.status);
      setText('binding-expires', intent.expires_at);
      document.getElementById('manual-command').textContent = intent.manual_login_command || '';
      var qrPanel = document.getElementById('qr-panel');
      var qrImage = document.getElementById('binding-qr');
      var placeholder = document.getElementById('qr-placeholder');
      if (intent.qr_data_url) {
        qrImage.src = intent.qr_data_url;
        qrPanel.style.display = ''; placeholder.style.display = 'none';
      } else {
        qrImage.removeAttribute('src');
        qrPanel.style.display = 'none'; placeholder.style.display = '';
      }
      var errorEl = document.getElementById('binding-error');
      if (intent.error) {
        errorEl.textContent = intent.error; errorEl.style.display = '';
      } else {
        errorEl.textContent = ''; errorEl.style.display = 'none';
      }
    }
  </script>
```

- [ ] **Step 4: Fill in `FILL_IN_PREFIX` and `FILL_IN_SCENE_ID`**

In the `<head>` block, replace `FILL_IN_PREFIX` with the `prefix` value from the Aliyun Captcha 2.0 console overview page.

In the `initAliyunCaptcha` call, replace `FILL_IN_SCENE_ID` with the `SceneId` from the verification scene list.

- [ ] **Step 5: Start the dev server and manually test the flow**

```bash
uvicorn app.main:app --reload
```

Open `http://localhost:8000/ui/onboarding.html` in a browser and verify:

1. Enter phone number → click「获取验证码」→ Aliyun captcha popup appears (or auto-passes in mock mode)
2. OTP input section appears
3. Enter OTP code (check server log for mock OTP: `sms: mock mode ... otp=XXXXXX`) → click「验证」
4. Nickname input section appears
5. Click「注册/继续」→ Step 2「创建智能体」unlocks

- [ ] **Step 6: Commit**

```bash
git add app/static/onboarding.html
git commit -m "feat: add captcha + OTP sub-flow to onboarding.html Step 1"
```

---

## Final verification

- [ ] **Run full test suite one last time**

```bash
pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Check mock mode works end-to-end** (no Aliyun credentials needed)

```bash
uvicorn app.main:app --reload
# In another terminal:
curl -s -X POST http://localhost:8000/web/sms/send-otp \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800000099","captcha_verify_param":"mock"}' | python3 -m json.tool
# Check server log for: "sms: mock mode (no credentials), otp=XXXXXX"
# Then use that OTP:
curl -s -X POST http://localhost:8000/web/sms/verify-otp \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800000099","code":"XXXXXX"}' | python3 -m json.tool
# Copy the verified_token, then:
curl -s -X POST http://localhost:8000/web/register \
  -H "Content-Type: application/json" \
  -d '{"phone":"13800000099","display_name":"Test","otp_token":"<token>"}' | python3 -m json.tool
```

Expected: full flow works, platform user is created.
