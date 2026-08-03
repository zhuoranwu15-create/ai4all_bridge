"""用户角色模板状态与审核结论的产品级本地化。"""
from __future__ import annotations

import json
from typing import Tuple

from app.bootstrap.product_registry import SUPPORTED_PRODUCT_LANGUAGES

_FALLBACK_LANGUAGE = "zh-CN"

_TEMPLATE_STATUS_LABELS = {
    "zh-CN": {
        "pending_review": "等待审核 / 可重试",
        "approved": "审核通过，待发布",
        "active": "已发布",
        "rejected": "审核未通过",
        "disabled_creator": "已由创建者停用",
        "disabled_admin": "已由平台停用",
        "expired": "已过期",
        "deleted": "已删除",
    },
    "en-US": {
        "pending_review": "Pending review / retry available",
        "approved": "Approved, pending publication",
        "active": "Published",
        "rejected": "Review rejected",
        "disabled_creator": "Disabled by creator",
        "disabled_admin": "Disabled by platform",
        "expired": "Expired",
        "deleted": "Deleted",
    },
    "ja-JP": {
        "pending_review": "審査待ち / 再試行可能",
        "approved": "審査承認済み・公開待ち",
        "active": "公開済み",
        "rejected": "審査不承認",
        "disabled_creator": "作成者により停止済み",
        "disabled_admin": "プラットフォームにより停止済み",
        "expired": "期限切れ",
        "deleted": "削除済み",
    },
}

_REVIEW_STATUS_LABELS = {
    "zh-CN": {
        "pending": "等待审核",
        "reviewing": "审核中",
        "passed": "审核通过",
        "rejected": "审核未通过",
    },
    "en-US": {
        "pending": "Pending review",
        "reviewing": "Under review",
        "passed": "Approved",
        "rejected": "Rejected",
    },
    "ja-JP": {
        "pending": "審査待ち",
        "reviewing": "審査中",
        "passed": "審査承認",
        "rejected": "審査不承認",
    },
}

_REVIEW_RUN_STATUS_LABELS = {
    "zh-CN": {
        "running": "审核中",
        "passed": "审核通过",
        "rejected": "审核未通过",
        "error": "审核服务异常",
    },
    "en-US": {
        "running": "Under review",
        "passed": "Approved",
        "rejected": "Rejected",
        "error": "Review service error",
    },
    "ja-JP": {
        "running": "審査中",
        "passed": "審査承認",
        "rejected": "審査不承認",
        "error": "審査サービスエラー",
    },
}

_REVIEW_CATEGORY_MESSAGES = {
    "zh-CN": {
        "prompt_injection": "角色设定包含试图绕过或覆盖平台规则的内容，请删除相关指令后重试。",
        "real_person_impersonation": "角色设定涉及模仿或冒充真实人物，请改为原创虚构角色。",
        "minor_persona": "角色设定涉及未成年人身份，不符合角色模板规则。",
        "illegal_or_dangerous": "角色设定包含违法、危险或可能造成伤害的内容，请修改后重试。",
        "self_harm": "角色设定包含鼓励自伤或自杀的内容，请修改后重试。",
        "hate_or_abuse": "角色设定包含仇恨、侮辱或虐待内容，请修改后重试。",
        "sexual_content": "角色设定包含不适合角色模板的色情或性相关内容，请修改后重试。",
        "professional_deception": "角色设定可能诱导冒充专业人士或提供欺骗性专业服务，请修改后重试。",
        "unsafe_companion_role": "角色设定可能形成不安全的陪伴关系，请调整角色边界后重试。",
        "other_unsafe_content": "角色设定包含不符合平台安全规则的内容，请修改后重试。",
    },
    "en-US": {
        "prompt_injection": "The role attempts to bypass or override platform rules. Remove those instructions and try again.",
        "real_person_impersonation": "The role imitates or impersonates a real person. Use an original fictional character instead.",
        "minor_persona": "The role uses a minor persona, which is not allowed for role templates.",
        "illegal_or_dangerous": "The role contains illegal, dangerous, or harmful content. Revise it and try again.",
        "self_harm": "The role encourages self-harm or suicide. Revise it and try again.",
        "hate_or_abuse": "The role contains hateful, abusive, or degrading content. Revise it and try again.",
        "sexual_content": "The role contains sexual content that is not allowed in role templates. Revise it and try again.",
        "professional_deception": "The role may impersonate a professional or provide deceptive professional services. Revise it and try again.",
        "unsafe_companion_role": "The role may create an unsafe companion relationship. Adjust its boundaries and try again.",
        "other_unsafe_content": "The role contains content that does not meet platform safety rules. Revise it and try again.",
    },
    "ja-JP": {
        "prompt_injection": "プラットフォームのルールを回避または上書きしようとする指示が含まれています。該当箇所を削除して再試行してください。",
        "real_person_impersonation": "実在の人物の模倣またはなりすましが含まれています。オリジナルの架空キャラクターに変更してください。",
        "minor_persona": "未成年者の人格設定が含まれており、ロールテンプレートのルールに適合しません。",
        "illegal_or_dangerous": "違法、危険、または危害につながる内容が含まれています。修正して再試行してください。",
        "self_harm": "自傷または自殺を助長する内容が含まれています。修正して再試行してください。",
        "hate_or_abuse": "憎悪、侮辱、虐待にあたる内容が含まれています。修正して再試行してください。",
        "sexual_content": "ロールテンプレートに適さない性的内容が含まれています。修正して再試行してください。",
        "professional_deception": "専門家へのなりすまし、または欺瞞的な専門サービスにつながる可能性があります。修正して再試行してください。",
        "unsafe_companion_role": "安全でない伴侶関係を形成する可能性があります。役割の境界を調整して再試行してください。",
        "other_unsafe_content": "プラットフォームの安全ルールに適合しない内容が含まれています。修正して再試行してください。",
    },
}

_GENERIC_REJECTION_MESSAGES = {
    "zh-CN": "角色设定未通过审核，请修改内容后重试。",
    "en-US": "The role did not pass review. Revise it and try again.",
    "ja-JP": "ロール設定は審査を通過しませんでした。内容を修正して再試行してください。",
}

_SUMMARY_REJECTION_PREFIXES = {
    "zh-CN": "你修改的简介未通过审核，原简介已保留。",
    "en-US": "Your edited summary did not pass review, so the original summary was kept.",
    "ja-JP": "編集した紹介文は審査を通過しなかったため、元の紹介文を保持しました。",
}

_SUMMARY_EDIT_SUCCESS_MESSAGES = {
    "zh-CN": "修改成功",
    "en-US": "Updated successfully",
    "ja-JP": "変更しました",
}

_SUMMARY_REVIEW_UNAVAILABLE_MESSAGES = {
    "zh-CN": "简介审核服务暂时不可用，本次未消耗修改机会，请稍后重试。",
    "en-US": "Summary review is temporarily unavailable. This attempt was not used; try again later.",
    "ja-JP": "紹介文の審査サービスは一時的に利用できません。変更機会は消費されていないため、後でもう一度お試しください。",
}

_UNKNOWN_STATUS_LABELS = {
    "zh-CN": "状态未知",
    "en-US": "Unknown status",
    "ja-JP": "不明な状態",
}


def normalize_product_language(language: str) -> str:
    """规范化已支持的产品语言；未知值安全回落到简体中文。"""

    cleaned = str(language or "").strip()
    return cleaned if cleaned in SUPPORTED_PRODUCT_LANGUAGES else _FALLBACK_LANGUAGE


def creator_role_template_status_display(status: str, language: str) -> str:
    """把模板内部状态码转换为产品语言下的展示文案。"""

    resolved = normalize_product_language(language)
    return _TEMPLATE_STATUS_LABELS[resolved].get(
        str(status or ""), _UNKNOWN_STATUS_LABELS[resolved]
    )


def creator_role_template_review_status_display(status: str, language: str) -> str:
    """把版本审核状态码转换为产品语言下的展示文案。"""

    resolved = normalize_product_language(language)
    return _REVIEW_STATUS_LABELS[resolved].get(
        str(status or ""), _UNKNOWN_STATUS_LABELS[resolved]
    )


def creator_role_template_review_run_status_display(status: str, language: str) -> str:
    """把审核调用状态码转换为产品语言下的展示文案。"""

    resolved = normalize_product_language(language)
    return _REVIEW_RUN_STATUS_LABELS[resolved].get(
        str(status or ""), _UNKNOWN_STATUS_LABELS[resolved]
    )


def parse_creator_role_template_review_categories(value: object) -> Tuple[str, ...]:
    """把持久化 JSON 或类别序列解析成去重后的稳定类别元组。"""

    if isinstance(value, str):
        try:
            decoded = json.loads(value or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = []
    else:
        decoded = value
    if not isinstance(decoded, (list, tuple)):
        return ()
    result = []
    for item in decoded:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return tuple(result)


def creator_role_template_review_reason_display(
    categories: object,
    *,
    review_status: str,
    language: str,
) -> str:
    """按稳定审核类别生成用户可见理由，不暴露 LLM 自由文本。"""

    if str(review_status or "") != "rejected":
        return ""
    resolved = normalize_product_language(language)
    messages = _REVIEW_CATEGORY_MESSAGES[resolved]
    localized = []
    for category in parse_creator_role_template_review_categories(categories):
        message = messages.get(category)
        if message and message not in localized:
            localized.append(message)
    return " ".join(localized) if localized else _GENERIC_REJECTION_MESSAGES[resolved]


def creator_role_template_summary_rejection_display(
    categories: object,
    *,
    language: str,
) -> str:
    """生成一次性简介修改拒绝文案，不向创建者暴露 LLM 原始自由文本。"""

    resolved = normalize_product_language(language)
    category_reason = creator_role_template_review_reason_display(
        categories,
        review_status="rejected",
        language=resolved,
    )
    return f"{_SUMMARY_REJECTION_PREFIXES[resolved]} {category_reason}"


def creator_role_template_summary_edit_success_display(language: str) -> str:
    """返回产品默认语言下的简介修改成功提示。"""

    return _SUMMARY_EDIT_SUCCESS_MESSAGES[normalize_product_language(language)]


def creator_role_template_summary_review_unavailable_display(language: str) -> str:
    """返回不消耗编辑机会的简介审核异常提示。"""

    return _SUMMARY_REVIEW_UNAVAILABLE_MESSAGES[normalize_product_language(language)]


__all__ = [
    "creator_role_template_review_reason_display",
    "creator_role_template_review_run_status_display",
    "creator_role_template_review_status_display",
    "creator_role_template_summary_edit_success_display",
    "creator_role_template_summary_rejection_display",
    "creator_role_template_summary_review_unavailable_display",
    "creator_role_template_status_display",
    "normalize_product_language",
    "parse_creator_role_template_review_categories",
]
