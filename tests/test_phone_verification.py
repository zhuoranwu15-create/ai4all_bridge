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
