from unittest.mock import MagicMock, patch


def _settings(tmp_path):
    s = MagicMock()
    s.user_profiles_dir = str(tmp_path / "profiles")
    return s


def test_agent_context_files_created_from_legacy_profile(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        from app.user_profiles import (
            CONTEXT_FILE_ORDER,
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

        for filename in CONTEXT_FILE_ORDER:
            assert (profile_path.parent / filename).exists()
            assert context.files[filename]["created"] is True

        assert "HEARTBEAT.md" not in CONTEXT_FILE_ORDER
        assert not (profile_path.parent / "HEARTBEAT.md").exists()
        assert "HEARTBEAT.md" not in context.files
        assert "HEARTBEAT" not in context.blocks
        assert "旧 soul 内容" in context.blocks["SOUL"]
        assert "喜欢简洁" in context.blocks["USER"]
        assert "用户是工程师" in context.blocks["MEMORY"]
        assert "测试助手" in context.blocks["IDENTITY"]


def test_agent_context_does_not_overwrite_existing_files(tmp_path):
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
        assert context.files["AGENTS.md"]["created"] is True


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
