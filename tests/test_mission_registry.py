"""app.products.zhaoxi.domain.missions.registry：使命模板注册表（agent_mission_and_orchestration_design.md §2.2/§3.1）。"""
import pytest

from app.products.zhaoxi.domain.missions.registry import get_mission_template, list_mission_templates


def test_list_returns_templates_in_order():
    templates = list_mission_templates()
    assert [t.id for t in templates] == [
        "mission_001", "mission_002", "mission_003", "mission_004",
    ]


def test_mission_001_hundred_moments_fields():
    template = get_mission_template("mission_001")
    assert template.slug == "hundred_moments"
    assert template.display_name == "百景"
    assert template.target_count == 100
    assert template.short_label == "生活瞬间"
    assert "当下即永恒" in template.inquiry


def test_mission_002_ten_solitudes_fields():
    template = get_mission_template("mission_002")
    assert template.slug == "ten_solitudes"
    assert template.display_name == "十刻"
    assert template.target_count == 10
    assert template.short_label == "喧嚣中的孤独"
    assert "悲欢" in template.inquiry


def test_unknown_mission_id_raises_keyerror():
    with pytest.raises(KeyError):
        get_mission_template("mission_999")


def test_mission_003_heartbeat_fields():
    template = get_mission_template("mission_003")
    assert template.slug == "heartbeat_moments"
    assert template.display_name == "心动"
    assert template.target_count == 100
    assert template.short_label == "心动瞬间"
    assert "心动" in template.inquiry


def test_mission_004_seen_fields():
    template = get_mission_template("mission_004")
    assert template.slug == "seen_moments"
    assert template.display_name == "看见"
    assert template.target_count == 30
    assert template.short_label == "被看见的小事"
    assert "看见" in template.inquiry


def test_prose_files_exist_and_are_non_empty():
    for template in list_mission_templates():
        assert template.prose_path.exists(), f"missing prose file for {template.id}"
        assert template.prose.strip(), f"empty prose for {template.id}"
        assert "# MISSION" in template.prose
