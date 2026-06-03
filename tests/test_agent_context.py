from unittest.mock import MagicMock, patch


def _settings(tmp_path):
    s = MagicMock()
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.system_dir = str(tmp_path / "system")
    return s


def test_agent_context_user_files_created_from_legacy_profile(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import (
            USER_CONTEXT_FILE_ORDER,
            SYSTEM_CONTEXT_FILES,
            read_agent_context,
            user_profile_path,
        )

        profile_path = user_profile_path("acc-context")
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            """# User Profile

## Soul
旧 soul 内容

## User Preferences
- 喜欢简洁

## Long-term Memory
- 用户是工程师
""",
            encoding="utf-8",
        )

        context = read_agent_context("acc-context", display_name="测试助手")

        # User-level files live in account directory
        for filename in USER_CONTEXT_FILE_ORDER:
            assert (profile_path.parent / filename).exists()
            assert context.files[filename]["created"] is True

        # System-level files live in system directory, not account directory
        for filename in SYSTEM_CONTEXT_FILES:
            assert not (profile_path.parent / filename).exists()
            assert (tmp_path / "system" / filename).exists()

        assert "HEARTBEAT.md" not in context.files
        assert "HEARTBEAT" not in context.blocks
        assert "旧 soul 内容" in context.blocks["SOUL"]
        assert "喜欢简洁" in context.blocks["USER"]
        assert "用户是工程师" in context.blocks["MEMORY"]
        assert "测试助手" in context.blocks["IDENTITY"]
        # System blocks are populated from data/system/
        assert context.blocks["AGENTS"] != ""
        assert context.blocks["TOOLS"] != ""
        assert "web_search" in context.blocks["TOOLS"]
        assert "如网络搜索" not in context.blocks["TOOLS"]


def test_agent_context_does_not_overwrite_existing_user_files(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import read_agent_context, user_profile_path

        profile_path = user_profile_path("acc-existing")
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text("# User Profile\n", encoding="utf-8")
        (profile_path.parent / "SOUL.md").write_text("custom soul", encoding="utf-8")

        context = read_agent_context("acc-existing")

        assert context.blocks["SOUL"] == "custom soul"
        assert context.files["SOUL.md"]["created"] is False
        assert context.files["IDENTITY.md"]["created"] is True


def test_existing_account_heartbeat_file_is_preserved_but_not_returned(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import read_agent_context, user_profile_path

        profile_path = user_profile_path("acc-old-heartbeat")
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text("# User Profile\n", encoding="utf-8")
        heartbeat_path = profile_path.parent / "HEARTBEAT.md"
        heartbeat_path.write_text("# HEARTBEAT\n旧账号备注", encoding="utf-8")

        context = read_agent_context("acc-old-heartbeat")

        assert heartbeat_path.exists()
        assert heartbeat_path.read_text(encoding="utf-8") == "# HEARTBEAT\n旧账号备注"
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
        assert "web_search" in updated
        assert "如网络搜索" not in updated
