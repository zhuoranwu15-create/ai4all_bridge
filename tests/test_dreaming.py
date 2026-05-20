from pathlib import Path
from unittest.mock import MagicMock, patch


TODAY = "2026-05-18"
YESTERDAY = "2026-05-17"


def _settings(tmp_path: Path):
    s = MagicMock()
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.llm_api_key = "fake-key"
    return s


def _write_daily(tmp_path: Path, account_id: str, date_str: str, content: str):
    from app.user_profiles import _safe_account_dir_name

    path = tmp_path / "profiles" / _safe_account_dir_name(account_id) / "memory" / f"{date_str}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_list_recent_daily_memory_oldest_to_newest(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s), patch("app.dreaming.settings", s):
        from app.dreaming import list_recent_daily_memory

        _write_daily(tmp_path, "acc", TODAY, "# 2026-05-18\n\n- 今天")
        _write_daily(tmp_path, "acc", YESTERDAY, "# 2026-05-17\n\n- 昨天")

        records = list_recent_daily_memory(account_id="acc", today=TODAY, days=2)

    assert [record["date"] for record in records] == [YESTERDAY, TODAY]
    assert "昨天" in records[0]["content"]
    assert "今天" in records[1]["content"]


def test_run_dreaming_skips_without_daily_notes(tmp_path):
    s = _settings(tmp_path)
    with patch("app.user_profiles.settings", s), patch("app.dreaming.settings", s):
        from app.dreaming import run_dreaming

        result = run_dreaming(account_id="acc-empty", today=TODAY)

    assert result["status"] == "skipped"
    assert result["reason"] == "no_daily_notes"


def test_run_dreaming_skips_nothing_result(tmp_path):
    s = _settings(tmp_path)
    with (
        patch("app.user_profiles.settings", s),
        patch("app.dreaming.settings", s),
        patch("app.dreaming._distill_sync", return_value="NOTHING"),
    ):
        from app.dreaming import run_dreaming, read_long_term_memory

        _write_daily(tmp_path, "acc-nothing", TODAY, "# 2026-05-18\n\n- 一次性测试")
        result = run_dreaming(account_id="acc-nothing", today=TODAY)
        memory = read_long_term_memory("acc-nothing")

    assert result["status"] == "skipped"
    assert result["reason"] == "nothing_to_promote"
    assert memory == "- 暂无"


def test_run_dreaming_updates_memory_md(tmp_path):
    s = _settings(tmp_path)
    distilled = "- 用户是算法工程师\n- 用户正在做 AI 陪伴产品"
    with (
        patch("app.user_profiles.settings", s),
        patch("app.dreaming.settings", s),
        patch("app.dreaming._distill_sync", return_value=distilled),
    ):
        from app.dreaming import run_dreaming, read_long_term_memory

        _write_daily(tmp_path, "acc-update", TODAY, "# 2026-05-18\n\n- 用户是算法工程师")
        result = run_dreaming(account_id="acc-update", today=TODAY)
        memory = read_long_term_memory("acc-update")

    assert result["status"] == "updated"
    assert result["source_files"][0]["date"] == TODAY
    assert "算法工程师" in memory
    assert "AI 陪伴产品" in memory


def test_admin_dreaming_endpoint(client, test_settings, tmp_path):
    from app.user_profiles import _safe_account_dir_name

    channel_account_id = "acc-admin-dream"
    account_id = "sk-admin-dream"
    safe_id = _safe_account_dir_name(account_id)
    memory_dir = Path(test_settings.user_profiles_dir) / safe_id / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / f"{TODAY}.md").write_text("# 2026-05-18\n\n- 用户喜欢简洁", encoding="utf-8")

    with patch("app.main.date_cls") as mock_date, patch(
        "app.dreaming._distill_sync",
        return_value="- 用户喜欢简洁回复",
    ):
        mock_date.today.return_value.isoformat.return_value = TODAY
        res = client.post(
            f"/openclaw/turn",
            json={
                "account_id": channel_account_id,
                "session_key": account_id,
                "sender_id": "sender-test",
                "chat_type": "private",
                "message_type": "text",
                "message_id": "m-admin-dream",
                "text": "hello",
            },
            headers={"Authorization": "Bearer test-secret"},
        )
        assert res.status_code == 200

        res = client.post(
            f"/admin/accounts/{account_id}/dreaming?days=7",
            headers={"Authorization": "Bearer test-admin"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "updated"
    assert data["memory_chars"] > 0
