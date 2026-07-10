"""Tests for first-chat onboarding state machine and helpers."""
import pytest
from unittest.mock import patch, MagicMock

from app import profile_storage


# ---------------------------------------------------------------------------
# State machine helpers
# ---------------------------------------------------------------------------

def test_set_onboarding_state_emits_analytics_event(fresh_db):
    """A2 打点：状态转移写入 analytics_events（from/to），仅状态实际变化时记一条。"""
    from app.db import connect, get_or_create_session, set_account_onboarding_state

    account_id = "acc-onb-event"
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="sender",
        sender_name=None,
        chat_id="chat",
        session_key=f"session-{account_id}",
    )

    set_account_onboarding_state(account_id=account_id, state="step1_sent")
    set_account_onboarding_state(account_id=account_id, state="step1_sent")  # 无变化，不应再记
    set_account_onboarding_state(account_id=account_id, state="step2_sent")

    with connect() as conn:
        rows = conn.execute(
            "SELECT from_state, to_state FROM analytics_events "
            "WHERE account_id = ? AND event_name = 'onboarding_state_changed' ORDER BY id",
            (account_id,),
        ).fetchall()

    transitions = [(r["from_state"], r["to_state"]) for r in rows]
    assert transitions == [("pending", "step1_sent"), ("step1_sent", "step2_sent")]


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
        extracted={"ai_name": "星星", "persona": "xiaotaiyang", "skip": False},
        user_name_ask_count=1,
        persona_ask_count=0,
    )
    assert result == "complete"


def test_next_state_from_step3_to_complete():
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="step3_sent",
        extracted={"persona": "xiaotaiyang", "skip": False},
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
    assert "不要说自己没有这些能力" in ctx


def test_prompt_context_step1_includes_user_name_and_combined_question():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step1_sent",
        user_name="小晨",
        ai_name=None,
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=0,
    )
    assert "小晨" in ctx
    assert "怎么称呼你（AI）" in ctx
    assert "希望你是什么样的陪伴" in ctx


def test_prompt_context_step1_includes_combined_options():
    # step1_sent = user just replied to user-name question; LLM should now ask the combined setup question
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step1_sent",
        user_name="小晨",
        ai_name=None,
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=0,
    )
    assert "小太阳" in ctx
    assert "小月牙" in ctx
    assert "橘" in ctx
    assert "自己设定" in ctx
    assert "名字只是建议" in ctx


def test_prompt_context_step2_is_wrapup_for_combined_reply():
    from app.onboarding import build_onboarding_prompt_context

    ctx = build_onboarding_prompt_context(
        state="step2_sent",
        user_name="小晨",
        ai_name="小满",
        persona="xiaotaiyang",
        user_name_ask_count=1,
        persona_ask_count=0,
    )

    assert "小满" in ctx
    assert "小太阳" in ctx
    assert "不再追问 onboarding 问题" in ctx
    assert "1. 先留白" not in ctx


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
    assert "菜单式破冰入口" in ctx
    assert "1-4 的编号菜单感" in ctx
    assert "小太阳" not in ctx  # persona options not shown at wrap-up stage


def test_prompt_context_empty_when_complete():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="complete",
        user_name="小晨",
        ai_name="星星",
        persona="xiaotaiyang",
        user_name_ask_count=1,
        persona_ask_count=1,
    )
    assert ctx == ""


# ---------------------------------------------------------------------------
# 营销活码：onboarding 话术 override + 强制 SOUL 人设跳过选人设问句
# （campaign_codes_technical_design.md §4.2/§4.3）
# ---------------------------------------------------------------------------

def test_prompt_context_appends_onboarding_script_override():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="pending",
        user_name=None,
        ai_name=None,
        persona=None,
        user_name_ask_count=0,
        persona_ask_count=0,
        onboarding_script_override="欢迎参加618活动的朋友！",
    )
    assert "欢迎参加618活动的朋友！" in ctx
    assert "本账号专属引导语" in ctx


def test_prompt_context_no_override_section_when_absent():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="pending",
        user_name=None,
        ai_name=None,
        persona=None,
        user_name_ask_count=0,
        persona_ask_count=0,
    )
    assert "本账号专属引导语" not in ctx


def test_prompt_context_step1_forced_soul_preset_skips_persona_options():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step1_sent",
        user_name="小晨",
        ai_name=None,
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=0,
        has_forced_soul_preset=True,
    )
    assert "怎么称呼你（AI）" in ctx
    assert "小太阳" not in ctx
    assert "自己设定" not in ctx
    assert "不要询问或提及人设" in ctx


def test_prompt_context_step2_forced_soul_preset_only_confirms_ai_name():
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step2_sent",
        user_name="小晨",
        ai_name="小满",
        persona="xiaotaiyang",  # 即便被误提取，也不应出现在强制人设文案里
        user_name_ask_count=1,
        persona_ask_count=0,
        has_forced_soul_preset=True,
    )
    assert "小满" in ctx
    assert "不再追问 onboarding 问题" in ctx


def test_apply_extracted_onboarding_info_forced_soul_preset_skips_persona(tmp_path, fresh_db):
    from app.onboarding import apply_extracted_onboarding_info
    from app import profile_storage

    account_id = "acc-forced-soul"
    profile_storage.write_file(account_id, "IDENTITY.md", "# IDENTITY\n- 你的名字是 小满，用它自称。\n")

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={"persona": "xiaotaiyang", "ai_name": None, "user_name": None},
        current_state="step2_sent",
        has_forced_soul_preset=True,
    )

    assert "persona" not in written


def test_apply_extracted_onboarding_info_applies_persona_without_forced_preset(tmp_path, fresh_db):
    from app.onboarding import apply_extracted_onboarding_info
    from app import profile_storage

    account_id = "acc-normal-soul"
    profile_storage.write_file(account_id, "IDENTITY.md", "# IDENTITY\n- 你的名字是 小满，用它自称。\n")

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={"persona": "xiaotaiyang", "ai_name": None, "user_name": None},
        current_state="step2_sent",
    )

    assert written.get("persona") == "xiaotaiyang"


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def test_extract_ai_name_uses_llm_result():
    import asyncio
    from app.onboarding import extract_onboarding_info_async

    with patch(
        "app.llm.generate_completion",
        return_value='{"user_name": null, "ai_name": "小A", "persona": null, "persona_custom": null, "skip": false}',
    ):
        result = asyncio.run(
            extract_onboarding_info_async(
                user_text="小A",
                current_state="step2_sent",
            )
        )

    assert result["ai_name"] == "小A"


def test_extract_combined_ai_name_and_modified_preset():
    import asyncio
    from app.onboarding import extract_onboarding_info_async

    with patch(
        "app.llm.generate_completion",
        return_value='{"user_name": null, "ai_name": "小满", "ai_name_source": "modified_preset", "persona": "xiaotaiyang", "persona_custom": null, "skip": false, "needs_confirmation": false}',
    ):
        result = asyncio.run(
            extract_onboarding_info_async(
                user_text="选 2，但别叫小太阳，叫你小满",
                current_state="step2_sent",
            )
        )

    assert result["ai_name"] == "小满"
    assert result["ai_name_source"] == "modified_preset"
    assert result["persona"] == "xiaotaiyang"


def test_extract_ai_name_does_not_guess_when_llm_fails():
    import asyncio
    from app.onboarding import extract_onboarding_info_async

    with patch("app.llm.generate_completion", side_effect=RuntimeError("llm unavailable")):
        result = asyncio.run(
            extract_onboarding_info_async(
                user_text="小A",
                current_state="step2_sent",
            )
        )

    assert result["ai_name"] is None


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

    content = profile_storage.read_file(account_id, "USER.md")
    assert content is not None
    assert "小晨" in content
    assert "用户称呼" in content


def test_write_user_name_updates_existing(tmp_path, fresh_db):
    from app.user_profiles import write_user_name, context_file_path

    account_id = "test-update-user-name"
    write_user_name(account_id, "小晨")
    write_user_name(account_id, "阿晨")

    content = profile_storage.read_file(account_id, "USER.md")
    assert "阿晨" in content
    assert "小晨" not in content


def test_write_ai_name_to_identity(tmp_path, fresh_db):
    from app.user_profiles import write_ai_name_to_identity, context_file_path

    account_id = "test-ai-name"
    write_ai_name_to_identity(account_id, "星星")

    content = profile_storage.read_file(account_id, "IDENTITY.md")
    assert content is not None
    assert "星星" in content


def test_apply_soul_preset_blank(tmp_path, fresh_db):
    from app.user_profiles import apply_soul_preset, context_file_path

    account_id = "test-soul-blank"
    apply_soul_preset(account_id, "blank")

    content = profile_storage.read_file(account_id, "SOUL.md")
    assert content is not None
    assert "SOUL" in content
    assert "温柔" in content


def test_apply_soul_preset_xiaotaiyang(tmp_path, fresh_db):
    from app.user_profiles import apply_soul_preset, write_ai_name_to_identity, context_file_path

    account_id = "test-soul-xiaotaiyang"
    write_ai_name_to_identity(account_id, "小太阳")
    apply_soul_preset(account_id, "xiaotaiyang")

    content = profile_storage.read_file(account_id, "SOUL.md")
    assert "小太阳" in content
    assert "小精灵" in content


def test_apply_soul_preset_with_ai_name(tmp_path, fresh_db):
    from app.user_profiles import apply_soul_preset, write_ai_name_to_identity, context_file_path

    account_id = "test-soul-with-name"
    write_ai_name_to_identity(account_id, "星星")
    apply_soul_preset(account_id, "blank")

    content = profile_storage.read_file(account_id, "SOUL.md")
    assert "星星" in content


def test_persona_preset_preserves_existing_ai_name(tmp_path, fresh_db):
    from app.onboarding import apply_extracted_onboarding_info
    from app.user_profiles import context_file_path, write_ai_name_to_identity

    account_id = "test-persona-keeps-ai-name"
    write_ai_name_to_identity(account_id, "小A")

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={"persona": "ju", "skip": False},
        current_state="step3_sent",
    )

    identity = profile_storage.read_file(account_id, "IDENTITY.md")
    soul = profile_storage.read_file(account_id, "SOUL.md")
    assert written["persona"] == "ju"
    assert "AI 名字：小A" in identity
    assert "AI 名字：橘" not in identity
    assert "小A" in soul
    assert "慵懒" in soul


def test_step2_modified_preset_writes_custom_ai_name_and_preset_soul(tmp_path, fresh_db):
    from app.onboarding import apply_extracted_onboarding_info
    from app.user_profiles import context_file_path

    account_id = "test-step2-modified-preset"

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={
            "ai_name": "小满",
            "ai_name_source": "modified_preset",
            "persona": "xiaotaiyang",
            "skip": False,
        },
        current_state="step2_sent",
    )

    identity = profile_storage.read_file(account_id, "IDENTITY.md")
    soul = profile_storage.read_file(account_id, "SOUL.md")
    assert written["ai_name"] == "小满"
    assert written["persona"] == "xiaotaiyang"
    assert "AI 名字：小满" in identity
    assert "小满" in soul
    assert "小精灵" in soul


def test_step2_blank_does_not_fix_ai_name(tmp_path, fresh_db):
    from app.onboarding import apply_extracted_onboarding_info
    from app.user_profiles import context_file_path

    account_id = "test-step2-blank"

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={"persona": "blank", "skip": True},
        current_state="step2_sent",
    )

    assert written["persona"] == "blank"
    soul = profile_storage.read_file(account_id, "SOUL.md")
    assert not profile_storage.exists(account_id, "IDENTITY.md")
    assert "温柔" in soul


def test_step2_custom_persona_writes_summary(tmp_path, fresh_db):
    from app.onboarding import apply_extracted_onboarding_info
    from app.user_profiles import context_file_path

    account_id = "test-step2-custom"

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={
            "ai_name": "岚",
            "ai_name_source": "custom",
            "persona": "custom",
            "persona_custom": "慢热但可靠，平时克制，关键时刻会认真陪着用户。",
            "skip": False,
        },
        current_state="step2_sent",
    )

    identity = profile_storage.read_file(account_id, "IDENTITY.md")
    soul = profile_storage.read_file(account_id, "SOUL.md")
    assert written["ai_name"] == "岚"
    assert written["persona"] == "custom"
    assert "AI 名字：岚" in identity
    assert "慢热但可靠" in soul


# ---------------------------------------------------------------------------
# Onboarding turn integration (via test client)
# ---------------------------------------------------------------------------

BRIDGE_HEADERS = {"Authorization": "Bearer test-secret"}


def _turn_payload(account_id: str, session_key: str, text: str, message_id: str = "msg-1") -> dict:
    return {
        "channel": "openclaw-weixin",
        "channel_account_id": account_id,
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


def test_pending_onboarding_welcome_uses_chat_id_as_weixin_target(client, fresh_db):
    """The real Weixin bridge puts the sendable peer in chat_id, not sender_id."""
    from app.user_profiles import context_file_path
    from unittest.mock import patch

    session_key = "agent:main:openclaw-weixin:bot-a:direct:peer-a@im.wechat"
    payload = {
        "channel": "openclaw-weixin",
        "channel_account_id": "bot-a",
        "account_id": "bot-a",
        "session_key": session_key,
        "sender_id": session_key,
        "chat_id": "peer-a@im.wechat",
        "chat_type": "private",
        "message_type": "text",
        "message_id": "welcome-target-msg-1",
        "text": "你好",
        "raw": {"event_type": "message", "content": "你好"},
    }

    with patch(
        "app.turn_service.node_gateway.node_send_text",
        return_value={"messageId": "welcome-1"},
    ) as mock_send:
        res = client.post(
            "/openclaw/turn",
            json=payload,
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["no_reply"] is True
    assert data["metadata"]["onboarding_welcome_to_user_id"] == "peer-a@im.wechat"
    mock_send.assert_called_once()
    assert mock_send.call_args.kwargs["to_user_id"] == "peer-a@im.wechat"
    soul = profile_storage.read_file(session_key, "SOUL.md")
    assert "专属的陪伴" in soul
    assert "个人 AI 陪伴与生活助理" not in soul

    from app.db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT direction, role, content FROM messages WHERE account_id = ? ORDER BY id ASC",
            (session_key,),
        ).fetchall()
    assert [(r["direction"], r["role"], r["content"]) for r in rows] == [
        ("inbound", "user", "你好"),
        ("outbound", "assistant", "你好，很高兴能成为微信好友，你希望我怎么称呼你？"),
    ]


def test_pending_onboarding_fallback_reply_still_asks_user_name(client, fresh_db):
    from app.db import get_account_onboarding_state

    session_key = "onboard-welcome-fallback"

    with patch("app.turn_service.node_gateway.node_send_text", side_effect=RuntimeError("send failed")), \
         patch("app.turn_service.generate_reply", return_value="你好呀！很高兴认识你。"):
        res = client.post(
            "/openclaw/turn",
            json=_turn_payload(session_key, session_key, "你好"),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    data = res.json()
    assert data["reply"].endswith("你希望我怎么称呼你？")
    assert get_account_onboarding_state(account_id=session_key) == "step1_sent"


def test_step2_combined_reply_writes_settings_and_completes(client, fresh_db):
    from app.db import get_account_onboarding_state, set_account_onboarding_state
    from app.user_profiles import context_file_path

    session_key = "onboard-step2-combined"

    with patch("app.turn_service.node_gateway.node_send_text", return_value={"messageId": "welcome"}):
        client.post(
            "/openclaw/turn",
            json=_turn_payload(session_key, session_key, "你好", "msg-init"),
            headers=BRIDGE_HEADERS,
        )

    set_account_onboarding_state(account_id=session_key, state="step2_sent")
    captured = {}

    def fake_generate_reply(*, user_text, history, system_prompt, **_kwargs):
        captured["system_prompt"] = system_prompt
        return "好，那我就是小满了。我们慢慢来。"

    with patch(
        "app.llm.generate_completion",
        return_value='{"user_name": null, "ai_name": "小满", "ai_name_source": "modified_preset", "persona": "xiaotaiyang", "persona_custom": null, "skip": false, "needs_confirmation": false}',
    ), patch("app.turn_service.generate_reply", side_effect=fake_generate_reply):
        res = client.post(
            "/openclaw/turn",
            json=_turn_payload(session_key, session_key, "选 2，但别叫小太阳，叫你小满", "msg-ai-setup"),
            headers=BRIDGE_HEADERS,
        )

    assert res.status_code == 200
    identity = profile_storage.read_file(session_key, "IDENTITY.md")
    soul = profile_storage.read_file(session_key, "SOUL.md")
    assert "AI 名字：小满" in identity
    assert "小满" in soul
    assert "小精灵" in soul
    assert "不再追问 onboarding 问题" in captured["system_prompt"]
    assert "菜单式破冰入口" in captured["system_prompt"]
    assert "1-4 的编号菜单感" in captured["system_prompt"]
    assert "自拍或随手拍" in captured["system_prompt"]
    assert "星座/八字" in captured["system_prompt"]
    assert "1. 先留白" not in captured["system_prompt"]
    assert get_account_onboarding_state(account_id=session_key) == "complete"


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


def test_write_user_name_preserves_existing_memory(tmp_path, fresh_db):
    """#1 回归：USER.md 已有 dreaming 记忆且无'用户称呼'行时，写名字不得整文件覆盖。"""
    from app.user_profiles import write_user_name, context_file_path

    account_id = "test-preserve-mem"
    profile_storage.write_file(account_id, "USER.md", "# USER\n\n- 用户自称冲哥。\n- 用户是马刺球迷。\n")

    write_user_name(account_id, "冲哥")

    content = profile_storage.read_file(account_id, "USER.md")
    assert "用户是马刺球迷" in content          # 既有记忆保留
    assert "- 用户称呼：冲哥" in content         # 新名字以 bullet 追加
    assert content.count("用户称呼") == 1


def test_write_user_name_uses_bullet_and_clears_placeholder(tmp_path, fresh_db):
    """#2 回归：默认占位符被清掉，用户称呼以统一 bullet 格式写入。"""
    from app.user_profiles import write_user_name, context_file_path, ensure_agent_context_files

    account_id = "test-bullet-fmt"
    ensure_agent_context_files(account_id)  # 生成 '# USER\n\n- 暂无'
    write_user_name(account_id, "二哥")

    content = profile_storage.read_file(account_id, "USER.md")
    assert "暂无" not in content
    assert "- 用户称呼：二哥" in content


def test_write_user_name_migrates_legacy_format_in_place(tmp_path, fresh_db):
    """旧格式（无 bullet '用户称呼：X'）在再次写入时原地迁移为 bullet，且保留其它行。"""
    from app.user_profiles import write_user_name, context_file_path

    account_id = "test-migrate-fmt"
    profile_storage.write_file(account_id, "USER.md", "# USER\n\n用户称呼：老薛\n- 用户是球迷。\n")

    write_user_name(account_id, "薛哥")

    content = profile_storage.read_file(account_id, "USER.md")
    assert "- 用户称呼：薛哥" in content
    assert "老薛" not in content
    assert "用户是球迷" in content
    assert content.count("用户称呼") == 1


# ---------------------------------------------------------------------------
# 活码强制 AI 名字（ai_name_preset，§4.4）——四种组合分支
# ---------------------------------------------------------------------------

def test_next_state_both_forced_step1_jumps_to_complete():
    """soul + ai_name 都强制：step1_sent 收到用户称呼后无更多可问，直接完成。"""
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="step1_sent",
        extracted={"user_name": "小明", "skip": False},
        user_name_ask_count=1,
        persona_ask_count=0,
        has_forced_soul_preset=True,
        has_forced_ai_name=True,
    )
    assert result == "complete"


def test_next_state_ai_name_only_still_goes_step2():
    """仅强制 ai_name（人设未定）：仍需在 step2 问人设，不提前完成。"""
    from app.onboarding import next_onboarding_state
    result = next_onboarding_state(
        current_state="step1_sent",
        extracted={"user_name": "小明", "skip": False},
        user_name_ask_count=1,
        persona_ask_count=0,
        has_forced_soul_preset=False,
        has_forced_ai_name=True,
    )
    assert result == "step2_sent"


def test_prompt_context_step1_both_forced_asks_nothing_self_intro():
    """两者都强制：step1 不问 AI 名字/人设，确认用户称呼 + 自我介绍 + 破冰。"""
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step1_sent",
        user_name="小晨",
        ai_name=None,
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=0,
        has_forced_soul_preset=True,
        has_forced_ai_name=True,
    )
    assert "小晨" in ctx
    assert "怎么称呼你（AI）" not in ctx  # 不问 AI 名字
    assert "小太阳" not in ctx            # 不展示人设菜单
    assert "自我介绍" in ctx
    assert "破冰" in ctx or "菜单" in ctx


def test_prompt_context_step1_ai_name_only_shows_persona_menu_no_name_question():
    """仅强制 ai_name：给人设菜单，但不问 AI 名字、不邀请改名（名字-only 简化处理）。"""
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step1_sent",
        user_name="小晨",
        ai_name=None,
        persona=None,
        user_name_ask_count=1,
        persona_ask_count=0,
        has_forced_soul_preset=False,
        has_forced_ai_name=True,
    )
    assert "小太阳" in ctx                 # 人设菜单仍展示
    assert "怎么称呼你（AI）" not in ctx    # 不问 AI 名字
    assert "名字已经定好" in ctx


def test_prompt_context_step2_ai_name_only_confirms_persona_keeps_name():
    """仅强制 ai_name：step2 处理人设选择，确认时不改名。"""
    from app.onboarding import build_onboarding_prompt_context
    ctx = build_onboarding_prompt_context(
        state="step2_sent",
        user_name="小晨",
        ai_name=None,
        persona="xiaotaiyang",
        user_name_ask_count=1,
        persona_ask_count=0,
        has_forced_soul_preset=False,
        has_forced_ai_name=True,
    )
    assert "不再追问 onboarding 问题" in ctx
    assert "不要更改或重新询问你的名字" in ctx


def test_apply_extracted_forced_ai_name_does_not_overwrite_identity(fresh_db):
    """强制 ai_name 账号：用户回复里抽出的 ai_name 不应写 IDENTITY 覆盖已定死的名字。"""
    from app.onboarding import apply_extracted_onboarding_info
    from app import profile_storage

    account_id = "acc-forced-ainame"
    profile_storage.write_file(account_id, "IDENTITY.md", "# IDENTITY\n- AI 名字：小满\n")

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={"ai_name": "别名想改", "persona": None, "user_name": None},
        current_state="step2_sent",
        has_forced_ai_name=True,
    )

    assert "ai_name" not in written
    identity = profile_storage.read_file(account_id, "IDENTITY.md")
    assert "小满" in identity
    assert "别名想改" not in identity


def test_apply_extracted_forced_ai_name_skips_preset_default_name(fresh_db):
    """强制 ai_name 账号选了带名字预设（人设未强制）：不应用预设默认名回填覆盖强制名字。"""
    from app.onboarding import apply_extracted_onboarding_info
    from app import profile_storage

    account_id = "acc-forced-ainame-preset"
    profile_storage.write_file(account_id, "IDENTITY.md", "# IDENTITY\n- AI 名字：小满\n")

    written = apply_extracted_onboarding_info(
        account_id=account_id,
        extracted={"ai_name": None, "persona": "xiaotaiyang", "user_name": None},
        current_state="step2_sent",
        has_forced_soul_preset=False,
        has_forced_ai_name=True,
    )

    # persona 仍会应用（人设未强制），但 ai_name 不应被预设默认名"小太阳"回填
    assert written.get("ai_name") != "小太阳"
    identity = profile_storage.read_file(account_id, "IDENTITY.md")
    assert "小满" in identity
