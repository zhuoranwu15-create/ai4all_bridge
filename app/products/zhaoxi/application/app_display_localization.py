"""朝夕 App API 中稳定机器码对应的产品语言展示投影。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.products.zhaoxi.application.product_localization import product_default_language
from app.products.zhaoxi.domain.missions.registry import MissionTemplate


_RELATIONSHIP_LABELS = {
    "zh-CN": {"friend": "朋友", "parent": "父母", "child": "子女", "sibling": "兄弟姐妹", "lover": "情侣", "partner": "伴侣", "custom": "自定义关系"},
    "en-US": {"friend": "Friend", "parent": "Parent", "child": "Child", "sibling": "Sibling", "lover": "Romantic partner", "partner": "Life partner", "custom": "Custom relationship"},
    "ja-JP": {"friend": "友だち", "parent": "親", "child": "子ども", "sibling": "きょうだい", "lover": "恋人", "partner": "パートナー", "custom": "カスタム関係"},
}

_TRAIT_LABELS = {
    "zh-CN": {"gentle": "温柔", "empathetic": "共情", "healing": "治愈", "patient": "耐心", "steady": "沉稳", "rational": "理性", "reliable": "可靠", "candid": "坦率", "lively": "活泼", "curious": "好奇", "energetic": "元气", "humorous": "幽默", "playful": "俏皮", "easygoing": "轻松", "delicate": "细腻", "thoughtful": "体贴", "independent": "独立", "decisive": "果断", "artistic": "文艺", "quiet": "安静", "talkative": "健谈", "practical": "务实", "optimistic": "乐观", "boundaried": "有边界感"},
    "en-US": {"gentle": "Gentle", "empathetic": "Empathetic", "healing": "Comforting", "patient": "Patient", "steady": "Steady", "rational": "Rational", "reliable": "Reliable", "candid": "Candid", "lively": "Lively", "curious": "Curious", "energetic": "Energetic", "humorous": "Humorous", "playful": "Playful", "easygoing": "Easygoing", "delicate": "Sensitive", "thoughtful": "Thoughtful", "independent": "Independent", "decisive": "Decisive", "artistic": "Artistic", "quiet": "Quiet", "talkative": "Talkative", "practical": "Practical", "optimistic": "Optimistic", "boundaried": "Respects boundaries"},
    "ja-JP": {"gentle": "優しい", "empathetic": "共感的", "healing": "癒やし", "patient": "辛抱強い", "steady": "落ち着き", "rational": "理性的", "reliable": "頼もしい", "candid": "率直", "lively": "活発", "curious": "好奇心旺盛", "energetic": "元気", "humorous": "ユーモア", "playful": "お茶目", "easygoing": "気さく", "delicate": "繊細", "thoughtful": "思いやり", "independent": "自立", "decisive": "決断力", "artistic": "芸術的", "quiet": "静か", "talkative": "話し好き", "practical": "現実的", "optimistic": "楽観的", "boundaried": "境界を尊重"},
}

_REPORT_LABELS = {
    "zh-CN": {"spam": "垃圾广告", "harassment": "骚扰辱骂", "threat": "威胁恐吓", "hate": "仇恨言论", "sexual": "色情低俗", "privacy": "侵犯隐私", "other": "其他"},
    "en-US": {"spam": "Spam or advertising", "harassment": "Harassment or abuse", "threat": "Threats or intimidation", "hate": "Hate speech", "sexual": "Sexual content", "privacy": "Privacy violation", "other": "Other"},
    "ja-JP": {"spam": "スパム・広告", "harassment": "嫌がらせ・暴言", "threat": "脅迫", "hate": "ヘイトスピーチ", "sexual": "性的・低俗な内容", "privacy": "プライバシー侵害", "other": "その他"},
}

_MISSION_TEXT = {
    "en-US": {
        "mission_001": {"display_name": "One Hundred Moments", "statement": "Record 100 moments from everyday life together", "bar": "Low: any moment worth remembering counts", "short_label": "everyday moments", "inquiry": "whether the present moment can last forever"},
        "mission_002": {"display_name": "Ten Encounters", "statement": "Record 10 moments of solitude in a crowd or shared smiles with strangers", "bar": "High: solitude in a crowd or a shared smile with a stranger", "short_label": "solitude in a crowd", "inquiry": "whether people can truly share one another's joys and sorrows"},
        "mission_003": {"display_name": "Heartbeats", "statement": "Record 100 heart-stirring moments together", "bar": "Low: a phrase, a glance, or a well-timed silence is enough if it moves the heart", "short_label": "heart-stirring moments", "inquiry": "whether a heartbeat is chance or destiny"},
        "mission_004": {"display_name": "Being Seen", "statement": "Record 30 small things quietly completed without anyone noticing", "bar": "Medium: overlooked everyday acts that deserve to be seen", "short_label": "small acts being seen", "inquiry": "whether being truly seen can make someone feel less tired"},
    },
    "ja-JP": {
        "mission_001": {"display_name": "百景", "statement": "日々の100の瞬間を一緒に記録する", "bar": "低：覚えておきたい瞬間なら何でもよい", "short_label": "日々の瞬間", "inquiry": "「今この瞬間は永遠になれるか」"},
        "mission_002": {"display_name": "十刻", "statement": "喧騒の中の孤独、または見知らぬ人と微笑み合う瞬間を10件記録する", "bar": "高：喧騒の中の孤独、または見知らぬ人と微笑み合う瞬間", "short_label": "喧騒の中の孤独", "inquiry": "人の喜びや悲しみは通じ合うのか"},
        "mission_003": {"display_name": "ときめき", "statement": "心が動く100の瞬間を一緒に記録する", "bar": "低：一言、視線、ちょうどよい沈黙など、心が動けば十分", "short_label": "ときめく瞬間", "inquiry": "ときめきは偶然か、それとも運命か"},
        "mission_004": {"display_name": "見つめる", "statement": "誰にも気づかれずにやり遂げた小さなことを30件記録する", "bar": "中：見過ごされても、気づかれる価値のある日常の小さなこと", "short_label": "気づかれた小さなこと", "inquiry": "本当に見てもらえたとき、人は少し楽になれるのか"},
    },
}


def _language(language: Optional[str], app_id: str) -> str:
    resolved = language or product_default_language(app_id)
    return resolved if resolved in _RELATIONSHIP_LABELS else "zh-CN"


def localized_resident_options(
    catalog: Dict[str, Any], *, language: Optional[str] = None, app_id: str = ZHAOXI_APP_ID
) -> Dict[str, Any]:
    """替换受控选项的 label，稳定 key、限制和头像字段保持不变。"""

    resolved = _language(language, app_id)
    result = dict(catalog)
    result["relationship_types"] = [
        {**item, "label": _RELATIONSHIP_LABELS[resolved].get(item["key"], item["label"])}
        for item in catalog.get("relationship_types", [])
    ]
    result["personality_traits"] = [
        {**item, "label": _TRAIT_LABELS[resolved].get(item["key"], item["label"])}
        for item in catalog.get("personality_traits", [])
    ]
    return result


def localized_report_options(
    payload: Dict[str, Any], *, language: Optional[str] = None, app_id: str = ZHAOXI_APP_ID
) -> Dict[str, Any]:
    """替换举报原因 label，reason_code 与校验版本保持不变。"""

    resolved = _language(language, app_id)
    return {
        **payload,
        "options": [
            {**item, "label": _REPORT_LABELS[resolved].get(item["reason_code"], item["label"])}
            for item in payload.get("options", [])
        ],
    }


def localized_mission_template(
    template: MissionTemplate, *, language: Optional[str] = None, app_id: str = ZHAOXI_APP_ID
) -> Dict[str, str]:
    """返回使命的展示文本；mission_id 与数值规则由调用方继续使用领域对象。"""

    resolved = _language(language, app_id)
    if resolved == "zh-CN":
        return {key: getattr(template, key) for key in ("display_name", "statement", "bar", "short_label", "inquiry")}
    return dict(_MISSION_TEXT.get(resolved, {}).get(template.id) or {
        key: getattr(template, key) for key in ("display_name", "statement", "bar", "short_label", "inquiry")
    })


__all__ = ["localized_mission_template", "localized_report_options", "localized_resident_options"]
