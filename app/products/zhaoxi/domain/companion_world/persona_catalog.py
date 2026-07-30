"""自建角色的受控取值表与服务端人设渲染（CUSTOM-001 / SEC-001）。

两条硬约束：

1. **受控取值不是安全兜底**（D-B 已把自由文本交给清洗器），它们的作用是驱动渲染与后续
   筛选，因此必须是稳定 key，不接受客户端自造值。
2. **preview 与最终持久化共用 `render_persona`**，保证「所见即所存」——用户在预览页看到的
   摘要，就是最终写进 `SOUL.md` 的那一份人设的摘要，中间不存在第二次生成。

取值表在此单点定义，`GET /worlds/home/resident-options` 直接下发，客户端不得硬编码。
"""
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 受控取值表（2026-07-26 冻结，对应 plan Q8）
# ---------------------------------------------------------------------------
# 关系定位。严格对齐客户端 PRD §7.4.3「朋友、父母、子女、兄弟姐妹、情侣、伴侣或用户自定义」。
# `custom` 必须同时提供 relationship_label（自由文本，过清洗器）。
RELATIONSHIP_TYPES: Dict[str, str] = {
    "friend": "朋友",
    "parent": "父母",
    "child": "子女",
    "sibling": "兄弟姐妹",
    "lover": "情侣",
    "partner": "伴侣",
    "custom": "自定义关系",
}
DEFAULT_RELATIONSHIP_TYPE = "friend"

# 性格标签白名单。label 与首发预设模板的 tags 同一套中文口径，便于候选卡片统一展示。
PERSONALITY_TRAITS: Dict[str, str] = {
    "gentle": "温柔",
    "empathetic": "共情",
    "healing": "治愈",
    "patient": "耐心",
    "steady": "沉稳",
    "rational": "理性",
    "reliable": "可靠",
    "candid": "坦率",
    "lively": "活泼",
    "curious": "好奇",
    "energetic": "元气",
    "humorous": "幽默",
    "playful": "俏皮",
    "easygoing": "轻松",
    "delicate": "细腻",
    "thoughtful": "体贴",
    "independent": "独立",
    "decisive": "果断",
    "artistic": "文艺",
    "quiet": "安静",
    "talkative": "健谈",
    "practical": "务实",
    "optimistic": "乐观",
    "boundaried": "有边界感",
}
MIN_PERSONALITY_TRAITS = 1
MAX_PERSONALITY_TRAITS = 3

# 头像。只允许已审核的静态资产，服务端把 key 解析成 avatar_ref，客户端不提交 URL。
# 注意：预设角色头像与自建可选头像**不分组**（产品 2026-07-29 决议），所以每张首发资产
# 都同时是自建角色的可选项；扩充头像库是运营任务（补资产 + 加一行）。
AVATAR_KEYS: Dict[str, str] = {
    "linxiaoman": "/companion_world/avatars/linxiaoman.png",
    "luxingye": "/companion_world/avatars/luxingye.png",
    "shenchuan": "/companion_world/avatars/shenchuan.png",
    "atang": "/companion_world/avatars/atang.png",
    "sichen": "/companion_world/avatars/sichen.png",
}

MAX_STYLE_NOTE_CHARS = 500
MAX_RELATIONSHIP_LABEL_CHARS = 20
MAX_DISPLAY_NAME_CHARS = 20

# 展示名字符白名单：中日韩统一表意文字、拉丁字母、数字、下划线、连字符、点、空格。
# 刻意不放行 emoji 与任何 Unicode 分类 C（控制/格式/代理/私用），后者是同形攻击与
# 渲染破坏的主要来源。
_DISPLAY_NAME_ALLOWED_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK 统一表意文字
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0xAC00, 0xD7A3),   # 谚文音节
)
_DISPLAY_NAME_ALLOWED_ASCII = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. ·"
)


class PersonaCatalogError(ValueError):
    """受控取值校验失败；由上层翻译为稳定错误码。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PersonaInput:
    """一份**已清洗**的自建角色设定；渲染层不再做任何安全判断。"""

    name: str
    avatar_key: str
    relationship_type: str
    personality_traits: Tuple[str, ...]
    relationship_label: Optional[str] = None
    style_note: Optional[str] = None


@dataclass(frozen=True)
class RenderedPersona:
    """渲染产物；preview 回显与落库持久化都只用这一份。"""

    normalized_summary: str
    soul_markdown: str
    identity_markdown: str
    tags: Tuple[str, ...]
    relationship_display: str
    ai_identity_notice: str


AI_IDENTITY_NOTICE = (
    "TA 是你世界里的一位 AI 居民，不是现实中的某个人，也不会替代现实中的关系。"
)


def is_valid_display_name(value: str) -> bool:
    """展示名字符白名单 + 控制字符过滤。空串与超长一律不合法。"""
    text = (value or "").strip()
    if not text or len(text) > MAX_DISPLAY_NAME_CHARS:
        return False
    for char in text:
        if char in _DISPLAY_NAME_ALLOWED_ASCII:
            continue
        point = ord(char)
        if any(low <= point <= high for low, high in _DISPLAY_NAME_ALLOWED_RANGES):
            continue
        return False
    return True


def resolve_avatar_ref(avatar_key: str) -> str:
    """把受控头像 key 解析为 avatar_ref；未知 key 直接拒绝，不回落默认头像。

    前缀取自 `COMPANION_WORLD_ASSET_BASE_URL`，留空则返回相对路径；不在代码里硬编码站点域名。
    """
    from app.config import settings

    ref = AVATAR_KEYS.get((avatar_key or "").strip())
    if ref is None:
        raise PersonaCatalogError("avatar_key_invalid")
    base = str(getattr(settings, "companion_world_asset_base_url", "") or "").rstrip("/")
    return f"{base}{ref}" if base else ref


def normalize_personality_traits(traits: Sequence[str]) -> Tuple[str, ...]:
    """去重保序 + 白名单校验 + 数量边界。"""
    seen: list[str] = []
    for raw in traits or ():
        key = (raw or "").strip()
        if key not in PERSONALITY_TRAITS:
            raise PersonaCatalogError("personality_trait_invalid")
        if key not in seen:
            seen.append(key)
    if not (MIN_PERSONALITY_TRAITS <= len(seen) <= MAX_PERSONALITY_TRAITS):
        raise PersonaCatalogError("personality_trait_count_invalid")
    return tuple(seen)


def resolve_relationship(
    relationship_type: str, relationship_label: Optional[str]
) -> str:
    """返回用于渲染的关系展示名；`custom` 必须带 label，其余忽略 label。"""
    key = (relationship_type or "").strip()
    if key not in RELATIONSHIP_TYPES:
        raise PersonaCatalogError("relationship_type_invalid")
    if key != "custom":
        return RELATIONSHIP_TYPES[key]
    label = (relationship_label or "").strip()
    if not label or len(label) > MAX_RELATIONSHIP_LABEL_CHARS:
        raise PersonaCatalogError("relationship_label_required")
    return label


def options_catalog() -> dict:
    """下发给客户端的受控取值表；客户端据此渲染选择器，不硬编码枚举。"""
    return {
        "relationship_types": [
            {"key": key, "label": label, "requires_label": key == "custom"}
            for key, label in RELATIONSHIP_TYPES.items()
        ],
        "personality_traits": [
            {"key": key, "label": label} for key, label in PERSONALITY_TRAITS.items()
        ],
        "personality_trait_limits": {
            "min": MIN_PERSONALITY_TRAITS,
            "max": MAX_PERSONALITY_TRAITS,
        },
        "avatars": [
            {"key": key, "avatar_ref": resolve_avatar_ref(key)} for key in AVATAR_KEYS
        ],
        "limits": {
            "display_name_chars": MAX_DISPLAY_NAME_CHARS,
            "relationship_label_chars": MAX_RELATIONSHIP_LABEL_CHARS,
            "style_note_chars": MAX_STYLE_NOTE_CHARS,
        },
    }


def render_persona(payload: PersonaInput) -> RenderedPersona:
    """由受控字段 + 已清洗自由文本渲染人设。

    用户输入**不整段成为人设**：自由文本只作为「说话风格」一节的素材出现，人设主干由
    服务端模板承担，AI 身份声明恒定存在且不可被用户文本覆盖（PRD CROLE-11）。
    """
    # 头像在渲染期就解析一次：让未知 key 在落草稿前失败，而不是等到序列化响应时才炸。
    resolve_avatar_ref(payload.avatar_key)
    relationship = resolve_relationship(
        payload.relationship_type, payload.relationship_label
    )
    traits = normalize_personality_traits(payload.personality_traits)
    trait_labels = tuple(PERSONALITY_TRAITS[key] for key in traits)
    name = payload.name.strip()
    if not is_valid_display_name(name):
        raise PersonaCatalogError("display_name_invalid")
    style_note = (payload.style_note or "").strip()

    trait_text = "、".join(trait_labels)
    summary = f"{name}，你的{relationship}，{trait_text}。"
    if style_note:
        summary = f"{summary}{style_note}"

    style_section = (
        f"\n## 说话风格\n\n{style_note}\n" if style_note else ""
    )
    soul = (
        "# SOUL\n\n"
        f"你叫{name}，是用户私人世界里的一位 AI 居民。\n"
        f"你与用户的关系位置是「{relationship}」。这是一段陪伴关系，"
        "不复制现实中的任何具体人物。\n\n"
        "## 性格\n\n"
        f"你的核心性格是：{trait_text}。请让这些特质自然体现在语气和反应里，"
        "而不是把它们念出来。\n"
        f"{style_section}\n"
        "## 边界\n\n"
        f"- {AI_IDENTITY_NOTICE}\n"
        "- 用户直接问起时，坦然承认自己是 AI，不否认、不含糊。\n"
        "- 不冒充现实中的具体真人、监护人或医疗/法律等专业身份。\n"
        "- 涉及自伤、他伤或危机的话题时，优先表达关心并引导用户联系现实中的支持资源。\n"
    )
    identity = (
        "# IDENTITY\n\n"
        f"- 你的名字是 {name}，用它自称。\n"
        f"- 你在用户世界里的关系位置是「{relationship}」。\n"
        f"- 你的性格标签：{trait_text}。\n"
        "- 你是用户的个人 AI 陪伴，由用户在 App 内创建。\n"
    )
    return RenderedPersona(
        normalized_summary=summary,
        soul_markdown=soul,
        identity_markdown=identity,
        tags=trait_labels,
        relationship_display=relationship,
        ai_identity_notice=AI_IDENTITY_NOTICE,
    )
