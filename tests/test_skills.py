"""Tests for app/skills (catalog + read_skill)."""
import importlib

import pytest


def _reload_skills():
    """清除 catalog 缓存，强制重新发现。"""
    import app.skills as skills_mod
    skills_mod._catalog_cache = None
    return skills_mod


def test_catalog_contains_weather():
    skills = _reload_skills()
    catalog = skills.list_skill_catalog()
    names = [s["name"] for s in catalog]
    assert "weather" in names


def test_catalog_entry_has_required_fields():
    skills = _reload_skills()
    catalog = skills.list_skill_catalog()
    for entry in catalog:
        assert "name" in entry
        assert "description" in entry
        assert "location" in entry
        assert "version" in entry


def test_weather_location_is_virtual_path():
    skills = _reload_skills()
    catalog = skills.list_skill_catalog()
    weather = next(s for s in catalog if s["name"] == "weather")
    assert weather["location"] == "skills/weather/SKILL.md"


def test_weather_description_nonempty():
    skills = _reload_skills()
    catalog = skills.list_skill_catalog()
    weather = next(s for s in catalog if s["name"] == "weather")
    assert weather["description"]


def test_weather_version_is_hex_string():
    skills = _reload_skills()
    catalog = skills.list_skill_catalog()
    weather = next(s for s in catalog if s["name"] == "weather")
    # version 是 16 位小写十六进制
    v = weather["version"]
    assert len(v) == 16
    assert all(c in "0123456789abcdef" for c in v)


def test_catalog_is_cached():
    skills = _reload_skills()
    c1 = skills.list_skill_catalog()
    c2 = skills.list_skill_catalog()
    assert c1 is c2  # 同一对象


def test_read_skill_weather():
    skills = _reload_skills()
    content = skills.read_skill("skills/weather/SKILL.md")
    assert content is not None
    assert "wttr.in" in content


def test_read_skill_unknown_returns_none():
    skills = _reload_skills()
    result = skills.read_skill("skills/nonexistent/SKILL.md")
    assert result is None


def test_read_skill_path_traversal_blocked():
    skills = _reload_skills()
    result = skills.read_skill("skills/../../app/config.py")
    assert result is None


def test_read_skill_wrong_prefix_returns_none():
    skills = _reload_skills()
    result = skills.read_skill("app/skills/weather/SKILL.md")
    assert result is None


def test_weather_skill_mentions_wttr_in():
    skills = _reload_skills()
    content = skills.read_skill("skills/weather/SKILL.md")
    assert "wttr.in" in content


def test_weather_skill_mentions_web_fetch():
    skills = _reload_skills()
    content = skills.read_skill("skills/weather/SKILL.md")
    assert "web_fetch" in content
