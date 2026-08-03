from app.products.zhaoxi.application.app_display_localization import (
    localized_mission_template,
    localized_report_options,
    localized_resident_options,
)
from app.products.zhaoxi.domain.companion_world.onboarding_content import (
    intro_content_for_persona,
)
from app.products.zhaoxi.domain.companion_world.persona_catalog import options_catalog
from app.products.zhaoxi.domain.missions.registry import get_mission_template


def test_resident_option_keys_are_stable_while_labels_follow_language():
    source = options_catalog()
    zh = localized_resident_options(source, language="zh-CN")
    en = localized_resident_options(source, language="en-US")
    ja = localized_resident_options(source, language="ja-JP")

    for field in ("relationship_types", "personality_traits"):
        assert [item["key"] for item in zh[field]] == [item["key"] for item in en[field]]
        assert [item["key"] for item in zh[field]] == [item["key"] for item in ja[field]]
    assert zh["relationship_types"][0]["label"] == "朋友"
    assert en["relationship_types"][0]["label"] == "Friend"
    assert ja["relationship_types"][0]["label"] == "友だち"


def test_report_reason_codes_are_stable_while_labels_follow_language():
    source = {
        "version": 1,
        "options": [
            {"reason_code": "spam", "label": "垃圾广告", "details_required": False},
            {"reason_code": "other", "label": "其他", "details_required": True},
        ],
    }
    en = localized_report_options(source, language="en-US")
    ja = localized_report_options(source, language="ja-JP")

    assert [item["reason_code"] for item in en["options"]] == ["spam", "other"]
    assert en["options"][0]["label"] == "Spam or advertising"
    assert ja["options"][0]["label"] == "スパム・広告"
    assert en["version"] == source["version"]


def test_mission_id_is_external_to_localized_display_projection():
    template = get_mission_template("mission_001")
    zh = localized_mission_template(template, language="zh-CN")
    en = localized_mission_template(template, language="en-US")
    ja = localized_mission_template(template, language="ja-JP")

    assert zh["display_name"] == "百景"
    assert en["display_name"] == "One Hundred Moments"
    assert ja["display_name"] == "百景"
    assert template.id == "mission_001"


def test_new_resident_intro_content_follows_language_without_changing_persona_key():
    zh = intro_content_for_persona("linxiaoman", language="zh-CN")
    en = intro_content_for_persona("linxiaoman", language="en-US")
    ja = intro_content_for_persona("linxiaoman", language="ja-JP")

    assert zh and en and ja
    assert len({zh.welcome_message, en.welcome_message, ja.welcome_message}) == 3
    assert intro_content_for_persona("unknown", language="en-US") is None
