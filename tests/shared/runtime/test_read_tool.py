"""Tests for app/tools/read_handlers.py (受控 read 工具)."""
import pytest


def _handle(args):
    from app.tools.read_handlers import handle_read
    return handle_read(args, ctx=None)


def test_read_weather_skill_succeeds():
    result = _handle({"path": "skills/weather/SKILL.md"})
    assert result["status"] == "succeeded"
    assert "wttr.in" in result["content"]


def test_read_returns_metadata():
    result = _handle({"path": "skills/weather/SKILL.md"})
    assert "totalLines" in result
    assert result["totalLines"] > 0
    assert result["linesReturned"] > 0
    assert result["path"] == "skills/weather/SKILL.md"


def test_read_offset_and_limit():
    result = _handle({"path": "skills/weather/SKILL.md", "offset": 1, "limit": 3})
    assert result["status"] == "succeeded"
    assert result["linesReturned"] == 3
    assert "\n".join(result["content"].splitlines()) == result["content"]


def test_read_truncation_hint_when_file_longer_than_limit():
    result = _handle({"path": "skills/weather/SKILL.md", "offset": 1, "limit": 2})
    if result["totalLines"] > 2:
        assert result["truncated"] is True
        assert result["continuationHint"] is not None
        assert "offset=3" in result["continuationHint"]


def test_read_no_truncation_hint_when_whole_file():
    result = _handle({"path": "skills/weather/SKILL.md", "offset": 1, "limit": 400})
    assert result["truncated"] is False
    assert result["continuationHint"] is None


def test_read_path_required():
    result = _handle({"path": ""})
    assert result["status"] == "failed"
    assert "path" in result["error"]


def test_read_wrong_prefix_blocked():
    result = _handle({"path": "app/config.py"})
    assert result["status"] == "failed"
    assert "skills/" in result["error"]


def test_read_path_traversal_blocked():
    result = _handle({"path": "skills/../../app/config.py"})
    assert result["status"] == "failed"


def test_read_nonexistent_skill_fails():
    result = _handle({"path": "skills/nonexistent/SKILL.md"})
    assert result["status"] == "failed"
    assert "available" in result


def test_read_blocked_shows_available_locations():
    result = _handle({"path": "app/config.py"})
    assert "available" in result
    assert any("skills/weather/SKILL.md" in loc for loc in result["available"])
