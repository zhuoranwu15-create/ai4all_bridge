import threading
import time

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


# ---------------------------------------------------------------------------
# get_valid_verification_by_token
# ---------------------------------------------------------------------------

def test_get_valid_verification_by_token_returns_row_when_valid(fresh_db):
    from app.db import (
        create_phone_verification,
        set_verification_verified,
        get_valid_verification_by_token,
    )
    row = create_phone_verification(phone="13800000040", code="888888", expires_minutes=10)
    verified = set_verification_verified(row["id"], token_expires_minutes=10)
    result = get_valid_verification_by_token(verified["verified_token"], "13800000040")
    assert result is not None
    assert result["phone"] == "13800000040"


def test_get_valid_verification_by_token_rejects_wrong_phone(fresh_db):
    from app.db import (
        create_phone_verification,
        set_verification_verified,
        get_valid_verification_by_token,
    )
    row = create_phone_verification(phone="13800000041", code="888889", expires_minutes=10)
    verified = set_verification_verified(row["id"], token_expires_minutes=10)
    assert get_valid_verification_by_token(verified["verified_token"], "13899999999") is None


def test_get_valid_verification_by_token_rejects_consumed(fresh_db):
    from app.db import (
        create_phone_verification,
        set_verification_verified,
        consume_verification_token,
        get_valid_verification_by_token,
    )
    row = create_phone_verification(phone="13800000042", code="888890", expires_minutes=10)
    verified = set_verification_verified(row["id"], token_expires_minutes=10)
    consume_verification_token(verified["id"])
    assert get_valid_verification_by_token(verified["verified_token"], "13800000042") is None


# ---------------------------------------------------------------------------
# POST /web/sms/send-otp
# ---------------------------------------------------------------------------

def test_send_otp_rejects_invalid_phone(client):
    from unittest.mock import patch
    with patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp"):
        res = client.post("/web/sms/send-otp", json={
            "phone": "123",
            "captcha_verify_param": "fake-param",
        })
    assert res.status_code == 400
    assert "phone" in res.json()["detail"]


def test_send_otp_rejects_failed_captcha(client):
    from unittest.mock import patch
    with patch("app.routers.web.verify_captcha", return_value=False), \
         patch("app.routers.web.send_otp"):
        res = client.post("/web/sms/send-otp", json={
            "phone": "13800000010",
            "captcha_verify_param": "bad-param",
        })
    assert res.status_code == 400
    assert "验证码" in res.json()["detail"]


def test_send_otp_returns_ok_and_creates_record(client):
    from unittest.mock import patch
    from app.db import get_latest_active_verification
    with patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp") as mock_sms:
        res = client.post("/web/sms/send-otp", json={
            "phone": "13800000011",
            "captcha_verify_param": "ok-param",
        })
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    mock_sms.assert_called_once()
    record = get_latest_active_verification("13800000011")
    assert record is not None
    assert len(record["code"]) == 6


def test_send_otp_rate_limits_per_hour(client):
    from unittest.mock import patch
    with patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp"):
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
    with patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp"):
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
    assert second["id"] != first["id"]


def test_send_otp_concurrent_requests_leave_one_active(client):
    """Two simultaneous /web/sms/send-otp for the same phone must not both
    invalidate each other's row (mutual-expire). At least one active row
    must remain so the user can verify the SMS they actually received."""
    from concurrent.futures import ThreadPoolExecutor
    from unittest.mock import patch
    from app.db import connect, get_latest_active_verification

    phone = "13800000014"
    in_send = threading.Event()
    resume_send = threading.Event()
    send_calls: list[int] = []
    send_calls_lock = threading.Lock()

    def slow_send(*, phone, code):
        with send_calls_lock:
            send_calls.append(1)
            is_first = len(send_calls) == 1
        if is_first:
            in_send.set()
            # Hold the first request inside the would-be-critical section
            # long enough for the second to race if the per-phone lock is missing.
            resume_send.wait(timeout=2.0)

    def hit():
        return client.post(
            "/web/sms/send-otp",
            json={"phone": phone, "captcha_verify_param": "ok"},
        )

    # Patch ONCE at module level so both threads share the same mock.
    with patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp", side_effect=slow_send):
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_a = pool.submit(hit)
            assert in_send.wait(timeout=2.0), "first send did not reach send_otp"
            fut_b = pool.submit(hit)
            # Give the second worker a chance to advance to the per-phone lock.
            time.sleep(0.1)
            resume_send.set()
            res_a = fut_a.result(timeout=5.0)
            res_b = fut_b.result(timeout=5.0)

    assert res_a.status_code == 200
    assert res_b.status_code == 200
    active = get_latest_active_verification(phone)
    assert active is not None, "lock failed: both rows mutually expired"
    # Both requests counted toward rate limit; both rows exist in DB.
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM phone_verifications WHERE phone = ?", (phone,)
        ).fetchall()
    assert len(rows) == 2


def test_failed_resend_can_expire_new_record_without_expiring_previous(fresh_db):
    from app.db import (
        create_phone_verification,
        get_latest_active_verification,
        invalidate_verification,
    )

    first = create_phone_verification(phone="13800000014", code="111111", expires_minutes=10)
    failed_new = create_phone_verification(phone="13800000014", code="222222", expires_minutes=10)

    invalidate_verification(failed_new["id"])

    active = get_latest_active_verification("13800000014")
    assert active["id"] == first["id"]
    assert active["code"] == "111111"


def test_successful_resend_expires_previous_record(fresh_db):
    from app.db import (
        create_phone_verification,
        get_latest_active_verification,
        invalidate_other_verifications_for_phone,
    )

    first = create_phone_verification(phone="13800000015", code="111111", expires_minutes=10)
    second = create_phone_verification(phone="13800000015", code="222222", expires_minutes=10)

    invalidate_other_verifications_for_phone("13800000015", second["id"])

    active = get_latest_active_verification("13800000015")
    assert active["id"] == second["id"]
    assert active["id"] != first["id"]


def test_send_otp_failed_resend_keeps_previous_record_without_testclient(fresh_db):
    from unittest.mock import patch
    from fastapi import HTTPException
    from app.db import get_latest_active_verification
    from app.routers.web import SendOtpRequest, web_send_otp

    payload = SendOtpRequest(phone="13800000016", captcha_verify_param="ok")
    with patch("app.routers.web.settings", fresh_db), \
         patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp"):
        assert web_send_otp(payload) == {"status": "ok"}
    first = get_latest_active_verification("13800000016")

    with patch("app.routers.web.settings", fresh_db), \
         patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp", side_effect=RuntimeError("minute flow control")):
        with pytest.raises(HTTPException) as exc_info:
            web_send_otp(payload)

    assert exc_info.value.status_code == 500
    active = get_latest_active_verification("13800000016")
    assert active["id"] == first["id"]


# ---------------------------------------------------------------------------
# POST /web/sms/verify-otp
# ---------------------------------------------------------------------------

def _send_otp_for(client, phone):
    from unittest.mock import patch
    from app.db import get_latest_active_verification
    with patch("app.routers.web.verify_captcha", return_value=True), \
         patch("app.routers.web.send_otp"):
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
    assert len(data["verified_token"]) == 36


def test_verify_otp_token_is_single_use(client):
    from app.db import get_latest_active_verification
    record = _send_otp_for(client, "13800000025")
    res1 = client.post("/web/sms/verify-otp", json={
        "phone": "13800000025",
        "code": record["code"],
    })
    assert res1.status_code == 200
    res2 = client.post("/web/sms/verify-otp", json={
        "phone": "13800000025",
        "code": record["code"],
    })
    assert res2.status_code == 400


# ---------------------------------------------------------------------------
# POST /web/register (with otp_token)
# ---------------------------------------------------------------------------

def _get_verified_token(phone: str) -> str:
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
        "phone": "13899999999",
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
    client.post("/web/register", json={
        "phone": "13800000034",
        "otp_token": token,
        "display_name": "Test",
    })
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
