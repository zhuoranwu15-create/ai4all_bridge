"""主动消息分类（OutboundCategory）的单一数据源（registry）。

每个分类的所有维度集中声明在 CATEGORY_SPECS；policy / settings / serializers /
tools 全部从这里派生。新增一个主动消息类型 = 加一个 OutboundCategory 成员 +
一条 CategorySpec，无需再到 5 个文件手工同步。startup 期 validate_category_registry()
做一致性校验，杜绝漏配/重复配置导致的静默错配。

本模块为纯数据层，不依赖 settings/policy/db，避免循环 import。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, Optional, Tuple


class OutboundCategory(str, Enum):
    USER_REMINDER = "user_reminder"
    COMPANION_FOLLOWUP = "companion_followup"
    NEW_USER_REACTIVATION = "new_user_reactivation"
    CONTENT_INVITATION = "content_invitation"
    CONTENT_INVITATION_RESPONSE = "content_invitation_response"
    TASK_RESULT = "task_result"


@dataclass(frozen=True)
class CategorySpec:
    """一个主动消息分类的全部策略维度。

    exempt=True 表示豁免直通（不过总开关/分类开关/配额/时段/avoidance，policy fast-path
    直接放行），这类分类必须 daily_limit_setting=None、frequency_bucket=None、
    user_configurable=False。非豁免分类必须有 daily_limit_setting 与 frequency_bucket。
    """

    category: OutboundCategory
    sources: Tuple[str, ...]            # dispatch source -> 本分类（反推 SOURCE_CATEGORY_MAP）
    exempt: bool                        # 豁免直通
    daily_limit_setting: Optional[str]  # 全局日上限对应的 settings 字段名
    daily_limit_default: int            # 该字段缺省值
    user_configurable: bool             # 是否出现在用户主动消息设置（可开关 + 频次）
    frequency_bucket: Optional[str]     # 频次桶（当前与 value 一一对应）
    content_preference_check: bool      # 是否做内容主题偏好（blocked/cooldown）检查
    avoidance_window: bool              # 是否做 avoidance window（临近提醒）检查
    avoidance_check_companion: bool     # avoidance 内是否额外查 pending companion 冲突
    label: str                          # 后台/tool 展示名


CATEGORY_SPECS: Tuple[CategorySpec, ...] = (
    CategorySpec(
        category=OutboundCategory.USER_REMINDER,
        sources=("reminder", "reminder_change_confirmation"),
        exempt=True,
        daily_limit_setting=None,
        daily_limit_default=0,
        user_configurable=False,
        frequency_bucket=None,
        content_preference_check=False,
        avoidance_window=False,
        avoidance_check_companion=False,
        label="用户提醒",
    ),
    CategorySpec(
        category=OutboundCategory.COMPANION_FOLLOWUP,
        sources=("commitment", "account_check", "heartbeat"),
        exempt=False,
        daily_limit_setting="companion_followup_daily_limit",
        daily_limit_default=1,
        user_configurable=True,
        frequency_bucket="companion_followup",
        content_preference_check=False,
        avoidance_window=True,
        avoidance_check_companion=False,
        label="陪伴跟进",
    ),
    CategorySpec(
        category=OutboundCategory.NEW_USER_REACTIVATION,
        sources=("new_user_reactivation",),
        exempt=False,
        daily_limit_setting="new_user_reactivation_daily_limit",
        daily_limit_default=4,
        user_configurable=False,
        frequency_bucket="new_user_reactivation",
        content_preference_check=False,
        avoidance_window=True,
        avoidance_check_companion=False,
        label="新用户破冰唤回",
    ),
    CategorySpec(
        category=OutboundCategory.CONTENT_INVITATION,
        sources=("content_invitation",),
        exempt=False,
        daily_limit_setting="content_invitation_daily_limit",
        daily_limit_default=1,
        user_configurable=True,
        frequency_bucket="content_invitation",
        content_preference_check=True,
        avoidance_window=True,
        avoidance_check_companion=True,
        label="内容邀请",
    ),
    CategorySpec(
        category=OutboundCategory.CONTENT_INVITATION_RESPONSE,
        sources=("content_invitation_titles", "content_invitation_feedback"),
        exempt=True,
        daily_limit_setting=None,
        daily_limit_default=0,
        user_configurable=False,
        frequency_bucket=None,
        content_preference_check=False,
        avoidance_window=False,
        avoidance_check_companion=False,
        label="内容邀请回复",
    ),
    CategorySpec(
        category=OutboundCategory.TASK_RESULT,
        sources=("async_task_result",),
        exempt=True,
        daily_limit_setting=None,
        daily_limit_default=0,
        user_configurable=False,
        frequency_bucket=None,
        content_preference_check=False,
        avoidance_window=False,
        avoidance_check_companion=False,
        label="任务结果",
    ),
)


# ---------------------------------------------------------------------------
# 派生视图（全部从 CATEGORY_SPECS 生成，不再手工维护）
# ---------------------------------------------------------------------------

SPEC_BY_CATEGORY: Dict[OutboundCategory, CategorySpec] = {
    spec.category: spec for spec in CATEGORY_SPECS
}

# dispatch source 字符串 -> 分类
SOURCE_CATEGORY_MAP: Dict[str, OutboundCategory] = {
    src: spec.category for spec in CATEGORY_SPECS for src in spec.sources
}

# 豁免直通分类集合
EXEMPT_CATEGORIES: FrozenSet[OutboundCategory] = frozenset(
    spec.category for spec in CATEGORY_SPECS if spec.exempt
)

# 用户可在设置中开关 + 设频次的分类（value，保序）
PROACTIVE_SETTING_CATEGORIES: Tuple[str, ...] = tuple(
    spec.category.value for spec in CATEGORY_SPECS if spec.user_configurable
)

# 频次桶集合（保序去重）
PROACTIVE_FREQUENCY_BUCKETS: Tuple[str, ...] = tuple(
    dict.fromkeys(
        spec.frequency_bucket for spec in CATEGORY_SPECS if spec.frequency_bucket
    )
)

# category value -> 频次桶
CATEGORY_FREQUENCY_BUCKET: Dict[str, str] = {
    spec.category.value: spec.frequency_bucket
    for spec in CATEGORY_SPECS
    if spec.frequency_bucket
}

# category value -> 展示标签
CATEGORY_LABELS: Dict[str, str] = {
    spec.category.value: spec.label for spec in CATEGORY_SPECS
}


def spec_for(category: OutboundCategory) -> CategorySpec:
    """返回分类的 CategorySpec。未注册分类抛 KeyError（不应发生，registry 全覆盖 enum）。"""
    return SPEC_BY_CATEGORY[category]


def validate_category_registry() -> None:
    """startup 期一致性校验：enum/registry 对齐、source 唯一、豁免不变量自洽。

    任何不一致抛 ValueError，使漏配/重复配置在启动时即暴露，而非运行期静默错配。
    """
    spec_categories = {spec.category for spec in CATEGORY_SPECS}
    enum_categories = set(OutboundCategory)
    if spec_categories != enum_categories:
        missing = enum_categories - spec_categories
        extra = spec_categories - enum_categories
        raise ValueError(
            f"category registry 与 OutboundCategory 不一致 missing={missing} extra={extra}"
        )

    seen_sources: set = set()
    for spec in CATEGORY_SPECS:
        for src in spec.sources:
            if src in seen_sources:
                raise ValueError(f"category registry source 重复映射: {src!r}")
            seen_sources.add(src)
        if spec.exempt:
            # 豁免直通分类不参与配额/设置：相关字段必须为空，避免误配。
            if spec.daily_limit_setting or spec.frequency_bucket or spec.user_configurable:
                raise ValueError(
                    f"豁免分类不应配置 daily_limit/frequency_bucket/user_configurable: "
                    f"{spec.category.value}"
                )
        else:
            if not spec.daily_limit_setting:
                raise ValueError(f"非豁免分类缺少 daily_limit_setting: {spec.category.value}")
            if not spec.frequency_bucket:
                raise ValueError(f"非豁免分类缺少 frequency_bucket: {spec.category.value}")
        if spec.avoidance_check_companion and not spec.avoidance_window:
            raise ValueError(
                f"avoidance_check_companion 需 avoidance_window=True: {spec.category.value}"
            )
