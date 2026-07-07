"""app.db.campaign：营销活码配置与注册归因快照（campaign_codes_technical_design.md §1/§2）。"""
import pytest

from app.db.campaign import (
    create_campaign_code,
    get_campaign_attribution,
    get_campaign_code,
    increment_campaign_code_used,
    list_campaign_codes,
    update_campaign_code,
    validate_campaign_code,
    write_campaign_attribution,
)


def _create_account(account_id: str) -> None:
    from app.db import get_or_create_session

    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )


# ---------------------------------------------------------------------------
# create / get / list / update
# ---------------------------------------------------------------------------

def test_create_and_get_campaign_code(fresh_db):
    created = create_campaign_code(code="618A", campaign_key="618种草视频A")

    assert created["code"] == "618A"
    assert created["status"] == "active"
    assert created["used_count"] == 0

    fetched = get_campaign_code(code="618A")
    assert fetched["campaign_key"] == "618种草视频A"


def test_create_duplicate_code_raises(fresh_db):
    create_campaign_code(code="DUP1", campaign_key="a")
    with pytest.raises(ValueError):
        create_campaign_code(code="DUP1", campaign_key="b")


def test_create_with_unknown_mission_id_raises(fresh_db):
    with pytest.raises(ValueError):
        create_campaign_code(code="BADM", campaign_key="a", mission_id="mission_999")


def test_create_with_unknown_soul_preset_key_raises(fresh_db):
    with pytest.raises(ValueError):
        create_campaign_code(code="BADS", campaign_key="a", soul_preset_key="not_a_preset")


def test_create_with_known_mission_and_soul_preset_succeeds(fresh_db):
    created = create_campaign_code(
        code="GOOD1",
        campaign_key="a",
        mission_id="mission_001",
        soul_preset_key="xiaotaiyang",
        onboarding_script_variant="欢迎参加活动",
    )
    assert created["mission_id"] == "mission_001"
    assert created["soul_preset_key"] == "xiaotaiyang"
    assert created["onboarding_script_variant"] == "欢迎参加活动"


def test_onboarding_script_variant_is_free_text_no_whitelist(fresh_db):
    """自由文本不做白名单校验——运营可填任意引导语（§4.2）。"""
    created = create_campaign_code(
        code="FREETXT", campaign_key="a", onboarding_script_variant="随便写点什么都行 12345"
    )
    assert created["onboarding_script_variant"] == "随便写点什么都行 12345"


def test_list_campaign_codes_filters_by_status(fresh_db):
    create_campaign_code(code="ACT1", campaign_key="a", status="active")
    create_campaign_code(code="DIS1", campaign_key="b", status="disabled")

    active_only = list_campaign_codes(status="active")
    codes = {c["code"] for c in active_only}
    assert "ACT1" in codes
    assert "DIS1" not in codes


def test_update_campaign_code_status(fresh_db):
    create_campaign_code(code="UPD1", campaign_key="a")
    updated = update_campaign_code(code="UPD1", status="disabled")
    assert updated["status"] == "disabled"


def test_update_unknown_code_raises(fresh_db):
    with pytest.raises(ValueError):
        update_campaign_code(code="NOPE", status="disabled")


def test_update_with_unsupported_field_raises(fresh_db):
    create_campaign_code(code="UPD2", campaign_key="a")
    with pytest.raises(ValueError):
        update_campaign_code(code="UPD2", used_count=99)


# ---------------------------------------------------------------------------
# code 字符集 / status 枚举 / 有效期窗口校验（codex review 发现的 P1/P2 修复）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_code", ["a b", "code'", 'code"', "code\\x", "a/b", "中文码", "x" * 65])
def test_create_rejects_invalid_code_charset(fresh_db, bad_code):
    with pytest.raises(ValueError):
        create_campaign_code(code=bad_code, campaign_key="a")


def test_create_accepts_url_safe_code_charset(fresh_db):
    created = create_campaign_code(code="Valid_Code-1", campaign_key="a")
    assert created["code"] == "Valid_Code-1"


def test_create_rejects_unknown_status(fresh_db):
    with pytest.raises(ValueError):
        create_campaign_code(code="BADSTAT", campaign_key="a", status="not_a_status")


def test_update_rejects_unknown_status(fresh_db):
    create_campaign_code(code="UPDSTAT", campaign_key="a")
    with pytest.raises(ValueError):
        update_campaign_code(code="UPDSTAT", status="not_a_status")


def test_create_rejects_malformed_datetime(fresh_db):
    with pytest.raises(ValueError):
        create_campaign_code(code="BADDT", campaign_key="a", expires_at="not-a-date")


def test_create_rejects_valid_from_after_expires_at(fresh_db):
    with pytest.raises(ValueError):
        create_campaign_code(
            code="BADWIN",
            campaign_key="a",
            valid_from="2026-12-01 00:00:00",
            expires_at="2026-01-01 00:00:00",
        )


def test_update_rejects_valid_from_after_existing_expires_at(fresh_db):
    create_campaign_code(code="UPDWIN", campaign_key="a", expires_at="2026-06-01 00:00:00")
    with pytest.raises(ValueError):
        update_campaign_code(code="UPDWIN", valid_from="2026-12-01 00:00:00")


def test_create_defaults_expires_at_to_three_months_out(fresh_db):
    created = create_campaign_code(code="DEFEXP", campaign_key="a")
    assert created["expires_at"] is not None

    from datetime import datetime

    created_at = datetime.strptime(created["created_at"], "%Y-%m-%d %H:%M:%S")
    expires_at = datetime.strptime(created["expires_at"], "%Y-%m-%d %H:%M:%S")
    delta_days = (expires_at - created_at).days
    assert 85 <= delta_days <= 92  # ~3 个自然月，跨月天数有 28~31 天的波动


def test_create_explicit_expires_at_overrides_default(fresh_db):
    created = create_campaign_code(code="EXPOVR", campaign_key="a", expires_at="2099-01-01 00:00:00")
    assert created["expires_at"] == "2099-01-01 00:00:00"


# ---------------------------------------------------------------------------
# validate_campaign_code
# ---------------------------------------------------------------------------

def test_validate_not_found(fresh_db):
    result = validate_campaign_code(code="MISSING")
    assert result == {"valid": False, "reason": "not_found"}


def test_validate_disabled(fresh_db):
    create_campaign_code(code="DISV", campaign_key="a", status="disabled")
    result = validate_campaign_code(code="DISV")
    assert result == {"valid": False, "reason": "disabled"}


def test_validate_expired(fresh_db):
    create_campaign_code(code="EXPV", campaign_key="a", expires_at="2000-01-01 00:00:00")
    result = validate_campaign_code(code="EXPV")
    assert result == {"valid": False, "reason": "expired"}


def test_validate_active_returns_strategy_fields(fresh_db):
    create_campaign_code(
        code="OKV",
        campaign_key="a",
        mission_id="mission_002",
        soul_preset_key="ju",
        onboarding_script_variant="hi",
    )
    result = validate_campaign_code(code="OKV")
    assert result["valid"] is True
    assert result["mission_id"] == "mission_002"
    assert result["soul_preset_key"] == "ju"
    assert result["onboarding_script_variant"] == "hi"


def test_increment_campaign_code_used(fresh_db):
    create_campaign_code(code="INC1", campaign_key="a")
    increment_campaign_code_used(code="INC1")
    increment_campaign_code_used(code="INC1")
    assert get_campaign_code(code="INC1")["used_count"] == 2


# ---------------------------------------------------------------------------
# attribution snapshot
# ---------------------------------------------------------------------------

def test_write_and_get_campaign_attribution(fresh_db):
    _create_account("acc-attr-1")
    write_campaign_attribution(
        account_id="acc-attr-1",
        campaign_code="ATTR1",
        mission_id="mission_001",
        onboarding_script_variant="hello",
        soul_preset_key="xiaoyueya",
    )
    attribution = get_campaign_attribution(account_id="acc-attr-1")
    assert attribution["campaign_code"] == "ATTR1"
    assert attribution["mission_id"] == "mission_001"
    assert attribution["soul_preset_key"] == "xiaoyueya"


def test_attribution_is_snapshot_not_live_join(fresh_db):
    """活码后续被编辑不影响已写入的归因快照（核心设计原则，§1）。"""
    _create_account("acc-attr-2")
    create_campaign_code(code="SNAP1", campaign_key="a", mission_id="mission_001")
    write_campaign_attribution(
        account_id="acc-attr-2",
        campaign_code="SNAP1",
        mission_id="mission_001",
        onboarding_script_variant=None,
        soul_preset_key=None,
    )

    update_campaign_code(code="SNAP1", mission_id="mission_002", status="disabled")

    attribution = get_campaign_attribution(account_id="acc-attr-2")
    assert attribution["mission_id"] == "mission_001"


def test_get_campaign_attribution_none_when_absent(fresh_db):
    assert get_campaign_attribution(account_id="acc-no-attr") is None


def test_write_campaign_attribution_does_not_overwrite(fresh_db):
    _create_account("acc-attr-3")
    write_campaign_attribution(
        account_id="acc-attr-3",
        campaign_code="FIRST",
        mission_id="mission_001",
        onboarding_script_variant=None,
        soul_preset_key=None,
    )
    write_campaign_attribution(
        account_id="acc-attr-3",
        campaign_code="SECOND",
        mission_id="mission_002",
        onboarding_script_variant=None,
        soul_preset_key=None,
    )
    attribution = get_campaign_attribution(account_id="acc-attr-3")
    assert attribution["campaign_code"] == "FIRST"
