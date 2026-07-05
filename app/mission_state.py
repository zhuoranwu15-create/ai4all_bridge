"""app.mission_state — 已分配使命的解析：DB 行 + 已注册模板的单一事实源。

「有效使命」的判定必须在所有消费方（tooling 门控、agent_self_state 渲染、mission 工具、
Admin 视图）保持一致：**account_mission 行存在，且 mission_id 能解析到 app.mission_registry
已注册的模板，才算 has_mission=True**。仅查 DB 行存在（不校验模板可解析）会导致脏数据或
未来模板下线后，模型仍会看到 mission_status/record_mission_moment 工具，但一调用就报错——
这里把判定收敛成一处，其余模块一律调用本模块，不再各自重复"查 DB 再 try/except 模板"。
"""
import logging
from typing import List, NamedTuple, Optional

from app.db import count_mission_moments, get_account_mission, list_mission_moments
from app.mission_registry import MissionTemplate, get_mission_template

logger = logging.getLogger("ai4all.mission_state")


class ResolvedMission(NamedTuple):
    mission_id: str
    assigned_at: str
    template: MissionTemplate


def resolve_account_mission(*, account_id: str) -> Optional[ResolvedMission]:
    """返回已分配且模板可解析的使命；未分配、或 mission_id 未注册（脏数据/模板下线），
    统一返回 None——调用方不应区分这两种情况，均按"当前无有效使命"处理。
    """
    assignment = get_account_mission(account_id=account_id)
    if assignment is None:
        return None
    try:
        template = get_mission_template(assignment["mission_id"])
    except KeyError:
        logger.warning(
            "unresolved mission_id account=%s mission_id=%s", account_id, assignment["mission_id"]
        )
        return None
    return ResolvedMission(
        mission_id=assignment["mission_id"],
        assigned_at=assignment["assigned_at"],
        template=template,
    )


def has_resolved_mission(*, account_id: str) -> bool:
    """供 tooling 门控（has_mission）使用的便捷判定。"""
    return resolve_account_mission(account_id=account_id) is not None


class MissionSnapshot(NamedTuple):
    resolved: ResolvedMission
    progress: int
    recent: List[dict]


def snapshot_account_mission(*, account_id: str, recent_limit: int = 5) -> Optional[MissionSnapshot]:
    """resolve + progress + 最近记录一次性取齐，供 Admin 视图和 mission_status 工具共用
    （两者展示口径一致，只是 recent_moments 的字段裁剪不同，见各自调用方）。
    """
    resolved = resolve_account_mission(account_id=account_id)
    if resolved is None:
        return None
    progress = count_mission_moments(account_id=account_id, mission_id=resolved.mission_id)
    recent = list_mission_moments(account_id=account_id, mission_id=resolved.mission_id, limit=recent_limit)
    return MissionSnapshot(resolved=resolved, progress=progress, recent=recent)


def build_admin_mission_view(*, account_id: str) -> Optional[dict]:
    """`/admin/accounts/{account_id}/meta` 的 mission 字段（agent_mission_and_
    orchestration_design.md §7）。未分配或模板不可解析时返回 None（与 meta/
    relationship_view 一致的"无数据"表达）。
    """
    snapshot = snapshot_account_mission(account_id=account_id)
    if snapshot is None:
        return None
    resolved, progress, recent = snapshot
    return {
        "mission_id": resolved.mission_id,
        "display_name": resolved.template.display_name,
        "assigned_at": resolved.assigned_at,
        "progress": progress,
        "target_count": resolved.template.target_count,
        "progress_label": f"{progress}/{resolved.template.target_count}",
        "recent_moments": [m["content"] for m in recent],
    }
