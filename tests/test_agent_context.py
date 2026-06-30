import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import profile_storage


def _settings(tmp_path):
    s = MagicMock()
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.system_dir = str(tmp_path / "system")
    return s


def test_agent_context_user_files_created_from_blank_template(fresh_db, tmp_path):
    s = fresh_db  # 提供隔离 DB（含 account_profile_files 表）+ 同 tmp_path 的 dirs
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import (
            SYSTEM_CONTEXT_FILES,
            USER_CONTEXT_FILE_ORDER,
            account_profile_dir,
            read_agent_context,
        )

        context = read_agent_context("acc-context", display_name="测试助手")

        # User-level files 入库（profile_storage），不再落账号磁盘目录
        for filename in USER_CONTEXT_FILE_ORDER:
            assert profile_storage.exists("acc-context", filename)
            assert context.files[filename]["created"] is True

        # System-level files 仍落 system_dir 磁盘，不入账号 storage
        for filename in SYSTEM_CONTEXT_FILES:
            assert not profile_storage.exists("acc-context", filename)
            assert (tmp_path / "system" / filename).exists()

        assert "HEARTBEAT.md" not in context.files
        assert "HEARTBEAT" not in context.blocks
        assert "专属的陪伴" in context.blocks["SOUL"]
        assert "测试助手" in context.blocks["SOUL"]
        assert "{name_clause}" not in context.blocks["SOUL"]
        assert "{user_clause}" not in context.blocks["SOUL"]
        assert "个人 AI 陪伴与生活助理" not in context.blocks["SOUL"]
        assert "- 暂无" in context.blocks["USER"]
        # RELATIONSHIP.md 已移除：不再作为上下文文件，也不进 prompt（关系状态由 DB 维护）
        assert "RELATIONSHIP" not in context.blocks
        assert "RELATIONSHIP.md" not in context.files
        assert "- 暂无" in context.blocks["MEMORY"]
        assert "测试助手" in context.blocks["IDENTITY"]
        # System blocks are populated from data/system/
        assert context.blocks["AGENTS"] != ""
        assert context.blocks["TOOLS"] != ""
        assert "session_status" in context.blocks["TOOLS"]
        assert "web_search" in context.blocks["TOOLS"]
        assert "如网络搜索" not in context.blocks["TOOLS"]


def test_checked_in_system_tools_file_matches_default_template():
    from app.user_profiles import _default_system_templates

    repo_root = Path(__file__).resolve().parents[1]
    checked_in = (repo_root / "data" / "system" / "TOOLS.md").read_text(encoding="utf-8")

    assert checked_in.strip() == _default_system_templates()["TOOLS.md"].strip()


def test_checked_in_system_agents_file_matches_default_template():
    from app.user_profiles import _default_system_templates

    repo_root = Path(__file__).resolve().parents[1]
    checked_in = (repo_root / "data" / "system" / "AGENTS.md").read_text(encoding="utf-8")

    assert checked_in.strip() == _default_system_templates()["AGENTS.md"].strip()


def test_previous_full_tools_default_is_known_and_upgradable():
    """瘦身前的上一版完整 TOOLS.md 应被识别为已知默认，可被自愈升级。"""
    from app.user_profiles import (
        _PREV_DEFAULT_TOOLS_V1,
        _PREV_DEFAULT_TOOLS_V2,
        _known_default_tools_templates_cached,
    )

    assert _PREV_DEFAULT_TOOLS_V1.strip() in _known_default_tools_templates_cached()
    assert _PREV_DEFAULT_TOOLS_V2.strip() in _known_default_tools_templates_cached()


def test_default_tools_template_mentions_default_chat_tools():
    from app.tools import get_default_tools
    from app.user_profiles import _default_system_templates

    tools_text = _default_system_templates()["TOOLS.md"]
    tool_names = {
        tool["function"]["name"]
        for tool in get_default_tools(
            web_search_enabled=True,
            content_invitation_response_enabled=True,
        )
    }

    missing = sorted(name for name in tool_names if name not in tools_text)
    assert missing == []
    assert "图片理解能力" in tools_text
    assert "可以接收并理解用户在当前微信对话里发来的图片" in tools_text
    assert "不能发送或生成图片" in tools_text


def test_agent_context_ignores_legacy_user_profile_when_creating_files(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import read_agent_context

        profile_storage.write_file(
            "acc-legacy-ignored",
            "user_profile.md",
            """# User Profile

## Soul
旧 soul 内容

## User Preferences
- 喜欢简洁

## Long-term Memory
- 用户是工程师
""",
        )

        context = read_agent_context("acc-legacy-ignored", display_name="测试助手")

        assert "旧 soul 内容" not in context.blocks["SOUL"]
        assert "喜欢简洁" not in context.blocks["USER"]
        assert "用户是工程师" not in context.blocks["MEMORY"]
        assert "专属的陪伴" in context.blocks["SOUL"]
        # 新账号默认 SOUL（blank 模板）含"说话方式"语气细节（不客服腔等）。
        assert "说话方式" in context.blocks["SOUL"]
        assert "不用客服式" in context.blocks["SOUL"]


def test_agent_context_does_not_overwrite_existing_user_files(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import read_agent_context

        profile_storage.write_file("acc-existing", "SOUL.md", "custom soul")

        context = read_agent_context("acc-existing")

        assert context.blocks["SOUL"] == "custom soul"
        assert context.files["SOUL.md"]["created"] is False
        assert context.files["IDENTITY.md"]["created"] is True
        assert "RELATIONSHIP.md" not in context.files


def test_ensure_agent_context_files_skips_default_render_when_files_exist(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import (
            USER_CONTEXT_FILE_ORDER,
            ensure_agent_context_files,
        )

        for filename in USER_CONTEXT_FILE_ORDER:
            profile_storage.write_file("acc-existing-all", filename, f"# {filename}\n\ncustom\n")

        with patch(
            "app.user_profiles._default_user_context_templates",
            side_effect=AssertionError("default templates should not be rendered"),
        ):
            created = ensure_agent_context_files("acc-existing-all", display_name="测试助手")

    assert created == {filename: False for filename in USER_CONTEXT_FILE_ORDER}


def test_agent_context_repairs_empty_soul_file_with_blank_template(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import read_agent_context

        profile_storage.write_file("acc-empty-soul", "SOUL.md", "\n  \n")

        context = read_agent_context("acc-empty-soul")

        assert context.files["SOUL.md"]["created"] is True
        assert "专属的陪伴" in context.blocks["SOUL"]
        assert "{name_clause}" not in context.blocks["SOUL"]
        assert "{user_clause}" not in context.blocks["SOUL"]


def test_agent_context_default_assistant_name_not_written_to_soul(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import read_agent_context

        context = read_agent_context("acc-default-name", display_name="AI4ALL 助手")

        assert "AI4ALL 助手" not in context.blocks["SOUL"]
        assert "你是我" in context.blocks["SOUL"]


def test_missing_blank_soul_template_logs_error_and_falls_back(tmp_path, caplog):
    from app import user_profiles

    template_dir = tmp_path / "templates"
    template_dir.mkdir(parents=True, exist_ok=True)
    for name in ("xiaotaiyang", "xiaoyueya", "ju"):
        (template_dir / f"{name}.md").write_text("# SOUL\n\n{name_clause}\n", encoding="utf-8")

    caplog.set_level(logging.ERROR, logger="ai4all.user_profiles")
    with patch("app.user_profiles._SOUL_TEMPLATES_DIR", template_dir):
        templates = user_profiles._load_soul_templates()

    assert "个人 AI 陪伴与生活助理" in templates["blank"]
    assert any("soul template load failed name=blank" in record.message for record in caplog.records)


def test_existing_account_heartbeat_file_is_preserved_but_not_returned(fresh_db, tmp_path):
    s = fresh_db
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import read_agent_context

        # 非受管的额外文件（如历史 HEARTBEAT.md）入库后不应被 read_agent_context 返回，也不被删
        profile_storage.write_file("acc-old-heartbeat", "user_profile.md", "# User Profile\n")
        profile_storage.write_file("acc-old-heartbeat", "HEARTBEAT.md", "# HEARTBEAT\n旧账号备注")

        context = read_agent_context("acc-old-heartbeat")

        assert profile_storage.read_file("acc-old-heartbeat", "HEARTBEAT.md") == "# HEARTBEAT\n旧账号备注"
        assert "HEARTBEAT.md" not in context.files
        assert "HEARTBEAT" not in context.blocks


def test_context_file_path_routes_system_files_to_system_dir(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import context_file_path

        agents_path = context_file_path("any-account", "AGENTS.md")
        tools_path = context_file_path("any-account", "TOOLS.md")
        assert str(tmp_path / "system") in str(agents_path)
        assert str(tmp_path / "system") in str(tools_path)


def test_context_file_path_routes_user_files_to_account_dir(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import context_file_path

        soul_path = context_file_path("my-account", "SOUL.md")
        assert "my-account" in str(soul_path)
        assert str(tmp_path / "system") not in str(soul_path)


def test_context_file_path_rejects_unknown_file(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import context_file_path

        try:
            context_file_path("acc", "UNKNOWN.md")
        except ValueError as exc:
            assert "unsupported context file" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_context_file_path_rejects_account_level_heartbeat(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import context_file_path

        try:
            context_file_path("acc", "HEARTBEAT.md")
        except ValueError as exc:
            assert "unsupported context file" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_legacy_default_tools_file_is_upgraded(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import ensure_system_context_files

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        tools_path.write_text(
            """# TOOLS

- 你可以调用以下工具帮助用户管理提醒：
  - **create_reminder**：创建提醒（用户明确了时间和内容时调用）
  - **list_reminders**：列出用户当前所有待执行的提醒
  - **cancel_reminder**：取消一个已有的提醒
  - **update_reminder**：修改提醒的时间或内容
- 时间不明确时，先向用户确认具体日期和时间，再调用工具。
- 提醒只能发到**当前对话**——不要承诺发给其他联系人或通过其他渠道通知。
- 不要承诺工具之外的能力（如网络搜索、发图片、联系其他人等）。
""",
            encoding="utf-8",
        )

        created = ensure_system_context_files()

        updated = tools_path.read_text(encoding="utf-8")
        assert created["TOOLS.md"] is False
        assert "session_status" in updated
        assert "web_search" in updated
        assert "如网络搜索" not in updated


def test_previous_default_tools_file_without_session_status_is_upgraded(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import (
            _RELATIONSHIP_STATUS_TOOLS_SECTION,
            _default_system_templates,
            ensure_system_context_files,
        )

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        previous_default = _default_system_templates()["TOOLS.md"].replace(
            _RELATIONSHIP_STATUS_TOOLS_SECTION + "\n", ""
        )
        tools_path.write_text(previous_default.strip() + "\n", encoding="utf-8")

        created = ensure_system_context_files()

        updated = tools_path.read_text(encoding="utf-8")
        assert created["TOOLS.md"] is False
        assert updated.strip() == _default_system_templates()["TOOLS.md"].strip()


def test_previous_default_tools_file_without_image_capability_is_upgraded(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import (
            _PREV_DEFAULT_TOOLS_V2,
            _default_system_templates,
            ensure_system_context_files,
        )

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        tools_path.write_text(_PREV_DEFAULT_TOOLS_V2.strip() + "\n", encoding="utf-8")

        created = ensure_system_context_files()

        updated = tools_path.read_text(encoding="utf-8")
        assert created["TOOLS.md"] is False
        assert updated.strip() == _default_system_templates()["TOOLS.md"].strip()
        assert "图片理解能力" in updated
        assert "不能发送或生成图片" in updated


def test_custom_system_tools_file_is_not_overwritten(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import ensure_system_context_files

        system_dir = tmp_path / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        tools_path = system_dir / "TOOLS.md"
        custom = "# TOOLS\n\n- custom operator note"
        tools_path.write_text(custom + "\n", encoding="utf-8")

        created = ensure_system_context_files()

        assert created["TOOLS.md"] is False
        assert tools_path.read_text(encoding="utf-8").strip() == custom
