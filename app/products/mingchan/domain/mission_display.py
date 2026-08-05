"""鸣蝉居民使命的客户端展示形态。"""

from typing import Optional

MISSION_DISPLAY_COUNTABLE = "countable"
MISSION_DISPLAY_NARRATIVE = "narrative"

_NARRATIVE_PERSONA_KEYS = frozenset({"sichen"})


def mission_display_for_persona(persona_key: Optional[str]) -> str:
    """命理类居民使用叙事展示，其余居民使用可数展示。"""

    if str(persona_key or "").strip() in _NARRATIVE_PERSONA_KEYS:
        return MISSION_DISPLAY_NARRATIVE
    return MISSION_DISPLAY_COUNTABLE


__all__ = [
    "MISSION_DISPLAY_COUNTABLE",
    "MISSION_DISPLAY_NARRATIVE",
    "mission_display_for_persona",
]
