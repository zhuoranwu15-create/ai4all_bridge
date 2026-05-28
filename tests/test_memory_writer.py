"""Tests for memory_writer module and read_daily_notes in user_profiles."""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TURNS = [
    {"role": "user", "content": "我是一名工程师"},
    {"role": "assistant", "content": "明白，我会记住的"},
]

TODAY = "2026-05-17"


def _make_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.user_profiles_dir = str(tmp_path / "profiles")
    s.llm_api_key = "fake-key"
    return s


# ---------------------------------------------------------------------------
# test_memory_file_path
# ---------------------------------------------------------------------------

class TestMemoryFilePath:
    def test_memory_file_path(self, tmp_path):
        s = _make_settings(tmp_path)
        with patch("app.memory_writer.settings", s):
            from app.memory_writer import memory_file_path
            p = memory_file_path("test@user", TODAY)
        # safe dir name replaces @ with _
        assert "memory" in str(p)
        assert TODAY in str(p)
        assert str(p).endswith(".md")
        # Should be inside profiles dir
        assert str(tmp_path) in str(p)


# ---------------------------------------------------------------------------
# test_raw_daily_notes_written
# ---------------------------------------------------------------------------

class TestWriteMemoryRawArchive:
    def test_raw_turn_content_is_written_without_llm_extraction(self, tmp_path):
        s = _make_settings(tmp_path)
        s.llm_api_key = ""
        with (
            patch("app.memory_writer.settings", s),
            patch("app.llm.generate_completion") as mock_generate_completion,
        ):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(
                write_memory(
                    "user1",
                    TURNS,
                    TODAY,
                    session_id=42,
                    user_message_id="msg-user-1",
                    assistant_message_id="msg-ai-1",
                    sent_at="2026-05-17T10:00:00+08:00",
                    modality="text",
                )
            )
            p = memory_file_path("user1", TODAY)

        mock_generate_completion.assert_not_called()
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert f"# {TODAY}" in content
        assert "## turn msg-ai-1" in content
        assert "- session_id: 42" in content
        assert "- user_message_id: msg-user-1" in content
        assert "- assistant_message_id: msg-ai-1" in content
        assert "- modality: text" in content
        assert "User:\n我是一名工程师" in content
        assert "AI:\n明白，我会记住的" in content


# ---------------------------------------------------------------------------
# test_new_file_has_date_header
# ---------------------------------------------------------------------------

class TestWriteMemoryHeader:
    def test_new_file_has_date_header(self, tmp_path):
        s = _make_settings(tmp_path)
        with patch("app.memory_writer.settings", s):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(
                write_memory(
                    "user2",
                    TURNS,
                    TODAY,
                    user_message_id="msg-user-2",
                    sent_at="2026-05-17T10:01:00+08:00",
                )
            )
            p = memory_file_path("user2", TODAY)
        content = p.read_text(encoding="utf-8")
        assert content.startswith(f"# {TODAY}\n\n## turn msg-user-2")


# ---------------------------------------------------------------------------
# test_append_to_existing_file
# ---------------------------------------------------------------------------

class TestWriteMemoryAppend:
    def test_append_to_existing_file(self, tmp_path):
        s = _make_settings(tmp_path)
        existing_content = f"# {TODAY}\n\n- 旧条目\n"

        with patch("app.memory_writer.settings", s):
            from app.memory_writer import memory_file_path
            p = memory_file_path("user3", TODAY)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(existing_content, encoding="utf-8")

        with patch("app.memory_writer.settings", s):
            from app.memory_writer import write_memory
            asyncio.run(
                write_memory(
                    "user3",
                    TURNS,
                    TODAY,
                    assistant_message_id="reply-3",
                    sent_at="2026-05-17T10:02:00+08:00",
                )
            )

        content = p.read_text(encoding="utf-8")
        # Old content preserved
        assert "旧条目" in content
        # New content appended
        assert "## turn reply-3" in content
        assert "我是一名工程师" in content


# ---------------------------------------------------------------------------
# test_empty_turns_no_write
# ---------------------------------------------------------------------------

class TestWriteMemoryEmptyTurns:
    def test_empty_turns_no_write(self, tmp_path):
        s = _make_settings(tmp_path)
        with patch("app.memory_writer.settings", s):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(write_memory("user4", [], TODAY))
            p = memory_file_path("user4", TODAY)

        assert not p.exists()

    def test_blank_visible_turns_no_write(self, tmp_path):
        s = _make_settings(tmp_path)
        with patch("app.memory_writer.settings", s):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(
                write_memory(
                    "user4",
                    [
                        {"role": "system", "content": "hidden"},
                        {"role": "user", "content": "   "},
                    ],
                    TODAY,
                )
            )
            p = memory_file_path("user4", TODAY)

        assert not p.exists()


# ---------------------------------------------------------------------------
# test_write_error_does_not_crash
# ---------------------------------------------------------------------------

class TestWriteMemoryWriteError:
    def test_write_error_does_not_crash(self, tmp_path):
        s = _make_settings(tmp_path)
        with (
            patch("app.memory_writer.settings", s),
            patch(
                "app.memory_writer._append_to_memory",
                side_effect=RuntimeError("disk full"),
            ),
        ):
            from app.memory_writer import write_memory
            # Should NOT raise
            asyncio.run(write_memory("user5", TURNS, TODAY))


# ---------------------------------------------------------------------------
# test_read_daily_notes
# ---------------------------------------------------------------------------

class TestReadDailyNotes:
    def _setup_memory_file(self, tmp_path: Path, account_id: str, date_str: str, content: str):
        """Helper: write a memory file into the tmp profiles dir."""
        from app.user_profiles import _safe_account_dir_name
        mem_dir = tmp_path / "profiles" / _safe_account_dir_name(account_id) / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / f"{date_str}.md").write_text(content, encoding="utf-8")

    def test_read_daily_notes_today_and_yesterday(self, tmp_path):
        s = _make_settings(tmp_path)
        self._setup_memory_file(tmp_path, "userA", TODAY, f"# {TODAY}\n\n- 今天的备注")
        yesterday = "2026-05-16"
        self._setup_memory_file(tmp_path, "userA", yesterday, f"# {yesterday}\n\n- 昨天的备注")

        with patch("app.user_profiles.settings", s):
            from app.user_profiles import read_daily_notes
            result = read_daily_notes("userA", TODAY)

        assert "今天的备注" in result
        assert "昨天的备注" in result

    def test_read_daily_notes_missing_files(self, tmp_path):
        s = _make_settings(tmp_path)
        with patch("app.user_profiles.settings", s):
            from app.user_profiles import read_daily_notes
            result = read_daily_notes("userB", TODAY)
        assert result == ""

    def test_read_daily_notes_only_today(self, tmp_path):
        s = _make_settings(tmp_path)
        self._setup_memory_file(tmp_path, "userC", TODAY, f"# {TODAY}\n\n- 只有今天")

        with patch("app.user_profiles.settings", s):
            from app.user_profiles import read_daily_notes
            result = read_daily_notes("userC", TODAY)

        assert "只有今天" in result

    def test_read_daily_notes_only_yesterday(self, tmp_path):
        s = _make_settings(tmp_path)
        yesterday = "2026-05-16"
        self._setup_memory_file(tmp_path, "userD", yesterday, f"# {yesterday}\n\n- 只有昨天")

        with patch("app.user_profiles.settings", s):
            from app.user_profiles import read_daily_notes
            result = read_daily_notes("userD", TODAY)

        assert "只有昨天" in result
