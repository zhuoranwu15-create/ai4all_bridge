"""角色模板状态与审核理由的产品语言展示契约。"""

from app.products.zhaoxi.application.creator_role_template_localization import (
    creator_role_template_review_reason_display,
    creator_role_template_review_status_display,
    creator_role_template_status_display,
    parse_creator_role_template_review_categories,
)


def test_status_codes_are_localized_without_changing_internal_values():
    assert creator_role_template_status_display("rejected", "zh-CN") == "审核未通过"
    assert creator_role_template_status_display("rejected", "en-US") == "Review rejected"
    assert creator_role_template_status_display("rejected", "ja-JP") == "審査不承認"
    assert creator_role_template_review_status_display("passed", "zh-CN") == "审核通过"


def test_rejected_reason_uses_categories_instead_of_raw_llm_text():
    categories = '["prompt_injection", "unsafe_companion_role"]'

    displayed = creator_role_template_review_reason_display(
        categories,
        review_status="rejected",
        language="zh-CN",
    )

    assert "绕过或覆盖平台规则" in displayed
    assert "不安全的陪伴关系" in displayed
    assert "LLM raw reason" not in displayed


def test_unknown_or_malformed_historical_categories_use_generic_reason():
    for categories in ('["legacy_unknown"]', "not-json", None):
        assert creator_role_template_review_reason_display(
            categories,
            review_status="rejected",
            language="zh-CN",
        ) == "角色设定未通过审核，请修改内容后重试。"

    assert (
        creator_role_template_review_reason_display(
            '["prompt_injection"]', review_status="passed", language="zh-CN"
        )
        == ""
    )


def test_category_parser_deduplicates_and_ignores_invalid_shapes():
    assert parse_creator_role_template_review_categories(
        '["prompt_injection", "prompt_injection"]'
    ) == ("prompt_injection",)
    assert parse_creator_role_template_review_categories('{"bad": true}') == ()
