from unittest.mock import MagicMock, patch


def _settings(tmp_path):
    s = MagicMock()
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.system_dir = str(tmp_path / "system")
    s.database_path = str(tmp_path / "test.db")
    return s


def test_plan_account_reset_matches_legacy_default_soul(tmp_path):
    from app.user_profiles import context_file_path
    from scripts.reset_legacy_default_souls import (
        OLD_DEFAULT_SOUL_BODY,
        plan_account_reset,
        render_candidate_diff,
    )

    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        path = context_file_path("acc-legacy", "SOUL.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# SOUL\n\n{OLD_DEFAULT_SOUL_BODY}\n", encoding="utf-8")

        candidate = plan_account_reset(account_id="acc-legacy")

    assert candidate is not None
    assert candidate.reason == "legacy_default"
    assert "专属的陪伴" in candidate.after
    diff = render_candidate_diff(candidate)
    assert "-你是这个微信账号的个人 AI 陪伴与生活助理" in diff
    assert "+你是我，是这个用户的专属的陪伴" in diff


def test_plan_account_reset_matches_empty_soul(tmp_path):
    from app.user_profiles import context_file_path
    from scripts.reset_legacy_default_souls import plan_account_reset

    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        path = context_file_path("acc-empty", "SOUL.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# SOUL\n\n", encoding="utf-8")

        candidate = plan_account_reset(account_id="acc-empty")

    assert candidate is not None
    assert candidate.reason == "empty"
    assert "专属的陪伴" in candidate.after


def test_plan_account_reset_skips_custom_soul(tmp_path):
    from app.user_profiles import context_file_path
    from scripts.reset_legacy_default_souls import plan_account_reset

    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        path = context_file_path("acc-custom", "SOUL.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# SOUL\n\n用户明确设定你是安静可靠的朋友。\n", encoding="utf-8")

        candidate = plan_account_reset(account_id="acc-custom")

    assert candidate is None


def test_plan_account_reset_missing_requires_flag(tmp_path):
    from scripts.reset_legacy_default_souls import plan_account_reset

    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s):
        assert plan_account_reset(account_id="acc-missing") is None
        candidate = plan_account_reset(account_id="acc-missing", include_missing=True)

    assert candidate is not None
    assert candidate.reason == "missing"
    assert "专属的陪伴" in candidate.after
