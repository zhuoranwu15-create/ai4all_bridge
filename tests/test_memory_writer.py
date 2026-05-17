"""Tests for memory_writer module and read_daily_notes in user_profiles."""
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

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
# test_nothing_response_does_not_write_file
# ---------------------------------------------------------------------------

class TestWriteMemoryNothingResponse:
    def test_nothing_response_does_not_write_file(self, tmp_path):
        s = _make_settings(tmp_path)
        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", return_value="NOTHING"),
        ):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(write_memory("user1", TURNS, TODAY))
            p = memory_file_path("user1", TODAY)
        assert not p.exists()

    def test_nothing_case_insensitive(self, tmp_path):
        s = _make_settings(tmp_path)
        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", return_value="nothing"),
        ):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(write_memory("user1", TURNS, TODAY))
            p = memory_file_path("user1", TODAY)
        assert not p.exists()

    def test_nothing_with_whitespace(self, tmp_path):
        s = _make_settings(tmp_path)
        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", return_value="  NOTHING  "),
        ):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(write_memory("user1", TURNS, TODAY))
            p = memory_file_path("user1", TODAY)
        assert not p.exists()


# ---------------------------------------------------------------------------
# test_extracted_content_is_written
# ---------------------------------------------------------------------------

class TestWriteMemoryContentWritten:
    def test_extracted_content_is_written(self, tmp_path):
        s = _make_settings(tmp_path)
        extracted = "- 用户喜欢简洁\n- 用户是工程师"
        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", return_value=extracted),
        ):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(write_memory("user1", TURNS, TODAY))
            p = memory_file_path("user1", TODAY)
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert "用户喜欢简洁" in content
        assert "用户是工程师" in content

    def test_new_file_has_date_header(self, tmp_path):
        s = _make_settings(tmp_path)
        extracted = "- 用户喜欢简洁"
        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", return_value=extracted),
        ):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(write_memory("user2", TURNS, TODAY))
            p = memory_file_path("user2", TODAY)
        content = p.read_text(encoding="utf-8")
        assert TODAY in content


# ---------------------------------------------------------------------------
# test_append_to_existing_file
# ---------------------------------------------------------------------------

class TestWriteMemoryAppend:
    def test_append_to_existing_file(self, tmp_path):
        s = _make_settings(tmp_path)
        existing_content = f"# {TODAY}\n\n- 旧条目\n"
        new_lines = "- 新条目一\n- 新条目二"

        with patch("app.memory_writer.settings", s):
            from app.memory_writer import memory_file_path
            p = memory_file_path("user3", TODAY)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(existing_content, encoding="utf-8")

        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", return_value=new_lines),
        ):
            from app.memory_writer import write_memory
            asyncio.run(write_memory("user3", TURNS, TODAY))

        content = p.read_text(encoding="utf-8")
        # Old content preserved
        assert "旧条目" in content
        # New content appended
        assert "新条目一" in content
        assert "新条目二" in content


# ---------------------------------------------------------------------------
# test_empty_turns_no_write
# ---------------------------------------------------------------------------

class TestWriteMemoryEmptyTurns:
    def test_empty_turns_no_write(self, tmp_path):
        s = _make_settings(tmp_path)
        call_count = {"n": 0}

        def fake_extract(turns):
            call_count["n"] += 1
            return "- something"

        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", side_effect=fake_extract),
        ):
            from app.memory_writer import write_memory, memory_file_path
            asyncio.run(write_memory("user4", [], TODAY))
            p = memory_file_path("user4", TODAY)

        assert call_count["n"] == 0
        assert not p.exists()


# ---------------------------------------------------------------------------
# test_llm_error_does_not_crash
# ---------------------------------------------------------------------------

class TestWriteMemoryLLMError:
    def test_llm_error_does_not_crash(self, tmp_path):
        s = _make_settings(tmp_path)

        def raise_error(turns):
            raise RuntimeError("LLM exploded")

        with (
            patch("app.memory_writer.settings", s),
            patch("app.memory_writer._extract_sync", side_effect=raise_error),
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
