from pathlib import Path
from unittest.mock import MagicMock

from app import profile_storage
from app.turn_context import TurnContext


def _ctx(account_id: str = "bazi-account") -> TurnContext:
    identity = MagicMock()
    return TurnContext(
        account_id=account_id,
        account={"id": account_id},
        session={"id": 1},
        identity=identity,
        binding={},
        message_id="message-1",
        text="排八字",
        today="2026-07-15",
        business_day="2026-07-15",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
    )


def test_bazi_profile_updates_memory_without_overwriting_existing_fields(fresh_db):
    from app.tools.bazi_profile_handlers import handle_update_bazi_profile
    from app.user_profiles import read_bazi_profile

    ctx = _ctx()
    first = handle_update_bazi_profile(
        {"birth_date": "1994-01-25", "birth_time_text": "下午", "birth_time_precision": "part_of_day"},
        ctx,
    )
    second = handle_update_bazi_profile({"gender": "女"}, ctx)

    assert first["success"] is True
    assert second["success"] is True
    assert second["profile"] == {
        "birth_date": "1994-01-25",
        "birth_time_text": "下午",
        "birth_time_precision": "part_of_day",
        "gender": "女",
    }
    assert read_bazi_profile(ctx.account_id) == second["profile"]


def test_bazi_profile_keeps_ambiguous_time_verbatim(fresh_db):
    from app.tools.bazi_profile_handlers import handle_update_bazi_profile

    result = handle_update_bazi_profile(
        {"birth_time_text": "下午", "birth_time_precision": "part_of_day"}, _ctx()
    )

    assert result["profile"]["birth_time_text"] == "下午"
    assert result["profile"]["birth_time_precision"] == "part_of_day"
    assert "14:00" not in profile_storage.read_file("bazi-account", "MEMORY.md")


def test_bazi_profile_is_available_across_sessions_and_days(fresh_db):
    from app.tools.bazi_profile_handlers import handle_get_bazi_profile, handle_update_bazi_profile

    handle_update_bazi_profile({"birth_date": "1994-01-25"}, _ctx())

    later_ctx = _ctx()
    later_ctx.session = {"id": 2}
    later_ctx.today = "2026-07-17"
    result = handle_get_bazi_profile({}, later_ctx)

    assert result == {"success": True, "profile": {"birth_date": "1994-01-25"}}


def test_bazi_profile_clear_field_and_delete_all(fresh_db):
    from app.tools.bazi_profile_handlers import (
        handle_clear_bazi_profile_field,
        handle_delete_bazi_profile,
        handle_update_bazi_profile,
    )

    ctx = _ctx()
    handle_update_bazi_profile({"birth_date": "1994-01-25", "gender": "女"}, ctx)

    cleared = handle_clear_bazi_profile_field({"field": "gender"}, ctx)
    deleted = handle_delete_bazi_profile({}, ctx)

    assert cleared["success"] is True
    assert cleared["profile"] == {"birth_date": "1994-01-25"}
    assert deleted == {"success": True, "deleted": True, "profile": {}}


def test_bazi_profile_section_survives_dreaming_memory_rewrite(fresh_db):
    from app.dreaming import write_long_term_memory
    from app.tools.bazi_profile_handlers import handle_update_bazi_profile
    from app.user_profiles import read_bazi_profile

    ctx = _ctx()
    handle_update_bazi_profile({"birth_date": "1994-01-25"}, ctx)
    write_long_term_memory(ctx.account_id, "- 普通长期记忆")

    assert read_bazi_profile(ctx.account_id) == {"birth_date": "1994-01-25"}
    memory = profile_storage.read_file(ctx.account_id, "MEMORY.md")
    assert "普通长期记忆" in memory
    assert "八字资料" in memory


def test_bazi_profile_invalid_update_fails_without_write(fresh_db):
    from app.tools.bazi_profile_handlers import handle_update_bazi_profile

    result = handle_update_bazi_profile({"birth_time_precision": "invented"}, _ctx())

    assert result["success"] is False
    assert "error" in result
    assert profile_storage.read_file("bazi-account", "MEMORY.md") is None


def test_bazi_skill_requires_successful_tool_write_before_claiming_saved():
    skill = (Path(__file__).resolve().parents[1] / "app/skills/bazi/SKILL.md").read_text(encoding="utf-8")

    assert "success=true" in skill
    assert "不得声称已保存" in skill
    first_phase = skill.split("## 第二阶段", 1)[0]
    assert "**姓名**" not in first_phase
    assert "**曾用名**" not in first_phase
