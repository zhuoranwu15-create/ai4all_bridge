"""Tests for first-chat onboarding state machine and helpers."""
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# State machine helpers
# ---------------------------------------------------------------------------

def test_is_onboarding_active_states():
    from app.onboarding import is_onboarding_active
    assert is_onboarding_active("pending") is True
    assert is_onboarding_active("step1_sent") is True
    assert is_onboarding_active("step2_sent") is True
    assert is_onboarding_active("step3_sent") is True
    assert is_onboarding_active("complete") is False
    assert is_onboarding_active("timed_out") is False


def test_next_state_from_pending():
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="pending",
        extracted={"user_name": None, "skip": False},
        user_name_ask_count=0,
        persona_ask_count=0,
    )
    assert result == "step1_sent"


def test_next_state_from_step1():
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="step1_sent",
        extracted={"user_name": "小明", "skip": False},
        user_name_ask_count=1,
        persona_ask_count=0,
    )
    assert result == "step2_sent"


def test_next_state_from_step2():
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="step2_sent",
        extracted={"ai_name": "星星", "skip": False},
        user_name_ask_count=1,
        persona_ask_count=0,
    )
    assert result == "step3_sent"


def test_next_state_from_step3_to_complete():
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="step3_sent",
        extracted={"persona": "chaochao", "skip": False},
        user_name_ask_count=1,
        persona_ask_count=1,
    )
    assert result == "complete"


def test_next_state_step3_skip_also_completes():
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="step3_sent",
        extracted={"persona": None, "skip": True},
        user_name_ask_count=1,
        persona_ask_count=1,
    )
    assert result == "complete"


# ---------------------------------------------------------------------------
# Prompt context builder
# ---------------------------------------------------------------------------

def test_prompt_context_pending_includes_step1_guidance():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="pending",
        user_name=None,
        ai_name=None,
        persona=None,
        user_name_ask_count=0,
        persona_ask_count=0,
    )
    assert "Onboarding" in ctx
    assert "pending" in ctx
    assert "称呼你" in ctx


def test_prompt_context_step2_includes_user_name():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step2_sent",
        user_name="小晨",
        ai_name=None,
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=0,
    )
    assert "小晨" in ctx
    assert "AI" in ctx


def test_prompt_context_step2_includes_persona_options():
    # step2_sent = user just replied to AI-name question; LLM should now ask persona
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step2_sent",
        user_name="小晨",
        ai_name=None,
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=0,
    )
    assert "朝朝" in ctx
    assert "夕夕" in ctx
    assert "橘" in ctx


def test_prompt_context_step3_is_wrapup():
    # step3_sent = user just replied to persona question; LLM should wrap up
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step3_sent",
        user_name="小晨",
        ai_name="星星",
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=1,
    )
    assert "onboarding" in ctx
    assert "朝朝" not in ctx  # persona options not shown at wrap-up stage


def test_prompt_context_empty_when_complete():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="complete",
        user_name="小晨",
        ai_name="星星",
        persona="chaochao",
        user_name_ask_count=1,
        persona_ask_count=1,
    )
    assert ctx == ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def test_get_and_set_onboarding_state(fresh_db):
    from app.db import get_account_onboarding_state, set_account_onboarding_state
    from app.db import create_ai4all_account_for_user, create_or_get_platform_user_by_phone

    user = create_or_get_platform_user_by_phone(phone="13900000001")
    result = create_ai4all_account_for_user(platform_user_id=user["id"], display_name="测试用户")
    account_id = result["account"]["id"]

    state = get_account_onboarding_state(account_id=account_id)
    assert state == "pending"

    set_account_onboarding_state(account_id=account_id, state="step1_sent")
    state = get_account_onboarding_state(account_id=account_id)
    assert state == "step1_sent"

    set_account_onboarding_state(account_id=account_id, state="complete")
    state = get_account_onboarding_state(account_id=account_id)
    assert state == "complete"


# ---------------------------------------------------------------------------
# user_profiles helpers
# ---------------------------------------------------------------------------

def test_write_user_name_creates_user_md(tmp_path, fresh_db):
    from app.user_profiles import write_user_name, context_file_path

    account_id = "test-write-user-name"
    write_user_name(account_id, "小晨")

    path = context_file_path(account_id, "USER.md")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "小晨" in content
    assert "用户称呼" in content


def test_write_user_name_updates_existing(tmp_path, fresh_db):
    from app.user_profiles import write_user_name, context_file_path

    account_id = "test-update-user-name"
    write_user_name(account_id, "小晨")
    write_user_name(account_id, "阿晨")

    path = context_file_path(account_id, "USER.md")
    content = path.read_text(encoding="utf-8")
    assert "阿晨" in content
    assert "小晨" not in content


def test_write_ai_name_to_identity(tmp_path, fresh_db):
    from app.user_profiles import write_ai_name_to_identity, context_file_path

    account_id = "test-ai-name"
    write_ai_name_to_identity(account_id, "星星")

    path = context_file_path(account_id, "IDENTITY.md")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "星星" in content


def test_apply_soul_preset_blank(tmp_path, fresh_db):
    from app.user_profiles import apply_soul_preset, context_file_path

    account_id = "test-soul-blank"
    apply_soul_preset(account_id, "blank")

    path = context_file_path(account_id, "SOUL.md")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "SOUL" in content
    assert "温柔" in content


def test_apply_soul_preset_chaochao(tmp_path, fresh_db):
    from app.user_profiles import apply_soul_preset, write_ai_name_to_identity, context_file_path

    account_id = "test-soul-chaochao"
    write_ai_name_to_identity(account_id, "朝朝")
    apply_soul_preset(account_id, "chaochao")

    content = context_file_path(account_id, "SOUL.md").read_text(encoding="utf-8")
    assert "朝朝" in content
    assert "爱自由" in content


def test_apply_soul_preset_with_ai_name(tmp_path, fresh_db):
    from app.user_profiles import apply_soul_preset, write_ai_name_to_identity, context_file_path

    account_id = "test-soul-with-name"
    write_ai_name_to_identity(account_id, "星星")
    apply_soul_preset(account_id, "blank")

    content = context_file_path(account_id, "SOUL.md").read_text(encoding="utf-8")
    assert "星星" in content


# ---------------------------------------------------------------------------
# Onboarding turn integration (via test client)
# ---------------------------------------------------------------------------

BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _turn_payload(account_id: str, session_key: str, text: str, message_id: str = "msg-1") -> dict:
    return {
        "account_id": account_id,
        "session_key": session_key,
        "sender_id": f"sender-{account_id}",
        "chat_type": "private",
        "message_type": "text",
        "message_id": message_id,
        "text": text,
        "raw": {"event_type": "message", "content": text},
    }


def test_first_turn_enters_onboarding_mode(client, fresh_db):
    """When onboarding_state=pending, the first user turn should trigger step1 and advance state.

    The turn resolver uses session_key as the account_id when no binding intent exists.
    """
    from app.db import get_account_onboarding_state
    from unittest.mock import patch

    # account_id == session_key in this test (the resolver returns session_key when no binding)
    session_key = "onboard-test-account-1"

    with patch("app.turn_service.generate_reply", return_value="你好！你希望我怎么称呼你？"):
        res = client.post(
            "/openclaw/turn",
            json=_turn_payload(session_key, session_key, "你好"),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    # The resolver returns session_key as account_id when no binding intent exists
    state = get_account_onboarding_state(account_id=session_key)
    assert state == "step1_sent"


def test_onboarding_complete_state_not_reprocessed(client, fresh_db):
    """When onboarding_state=complete, the turn should NOT inject onboarding context."""
    from app.db import set_account_onboarding_state
    from unittest.mock import patch

    session_key = "onboard-test-account-2"

    # Seed the account by sending a turn first
    with patch("app.turn_service.generate_reply", return_value="欢迎！"):
        client.post(
            "/openclaw/turn",
            json=_turn_payload(session_key, session_key, "你好", "msg-init"),
            headers=BRIDGE_HEADERS,
        )

    # Set state to complete
    set_account_onboarding_state(account_id=session_key, state="complete")

    with patch("app.turn_service.generate_reply", return_value="好的！"), \
         patch("app.turn_service.build_onboarding_prompt_context") as mock_ctx:
        res = client.post(
            "/openclaw/turn",
            json=_turn_payload(session_key, session_key, "帮我查天气", "msg-2"),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    # build_onboarding_prompt_context should not be called when onboarding is complete
    mock_ctx.assert_not_called()
