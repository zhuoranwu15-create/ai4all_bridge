"""朝夕使命模板的产品语言展示投影。"""
from __future__ import annotations

from typing import Dict, Optional

from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.products.zhaoxi.application.product_localization import product_default_language
from app.products.zhaoxi.domain.missions.registry import MissionTemplate


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
    return resolved if resolved in {"zh-CN", "en-US", "ja-JP"} else "zh-CN"


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


__all__ = ["localized_mission_template"]
