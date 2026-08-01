"""朝夕使命分配编排（agent_mission_and_orchestration_design.md §5）。

分配时机只有两处：① onboarding 完成时（新账号，见 app/turn_service.py）；② 存量账号
一次性回填（scripts/backfill_account_missions.py）。两处都只应调用
assign_mission_if_absent，不直接操作 app.products.zhaoxi.infrastructure.persistence.mission / app.products.zhaoxi.infrastructure.profiles 的底层写入。
"""
import hashlib
import logging
from typing import Optional

from app.products.zhaoxi.infrastructure.persistence.campaign import get_campaign_attribution
from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (
    get_account_creator_role_template_attribution,
)
from app.products.zhaoxi.infrastructure.persistence.mission import assign_mission, get_account_mission
from app.products.zhaoxi.domain.missions.registry import get_mission_template, list_mission_templates
from app.products.zhaoxi.infrastructure.profiles import write_context_file

logger = logging.getLogger("ai4all.mission_assignment")


def _pick_mission_id(account_id: str) -> str:
    """账号哈希轮询：同一账号结果稳定（幂等重试不换模板），且在已注册模板间大致均匀分布。"""
    templates = list_mission_templates()
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(templates)
    return templates[index].id


def resolve_mission_assignment_candidate(*, account_id: str) -> Optional[str]:
    """返回账号应分配的量化使命；自由使命模板账号明确返回 ``None``。"""
    if get_account_creator_role_template_attribution(account_id=account_id) is not None:
        return None
    attribution = get_campaign_attribution(account_id=account_id)
    return (attribution or {}).get("mission_id") or _pick_mission_id(account_id)


def assign_mission_if_absent(*, account_id: str) -> Optional[str]:
    """若账号尚未分配量化使命则分配并写入 MISSION.md。

    已分配时返回现有 mission_id；角色模板自由使命账号返回 ``None``。幂等，可安全地在
    onboarding 完成时调用，也可在存量账号回填脚本中重复调用。

    写入顺序刻意为「先写 MISSION.md prose，再写 account_mission DB 行」：
    _pick_mission_id 是账号哈希的纯函数，同账号任何时候重算结果都相同，所以即便进程
    在两步之间崩溃，下次调用仍会算出同一个 mission_id、重新执行两步（file 覆盖同样内容
    是幂等的，DB insert 靠 ON CONFLICT DO NOTHING 幂等）。反过来若先写 DB 后写文件，
    崩溃会导致 get_account_mission 提前判定「已分配」而永远跳过 MISSION.md 写入，
    留下 DB 有分配、文件仍是「暂无」占位符的不一致状态。
    """
    # creator role template 的 MISSION.md 已由注册快照写入；它不是量化使命，必须先于
    # existing/campaign/hash 判定，避免回填或重试给这类账号补上第二个使命。
    mission_id = resolve_mission_assignment_candidate(account_id=account_id)
    if mission_id is None:
        logger.info("quantified mission skipped account=%s source=creator_role_template", account_id)
        return None

    existing = get_account_mission(account_id=account_id)
    if existing is not None:
        return existing["mission_id"]

    # 营销活码若在注册时指定了 mission_id，优先命中；否则回退现有哈希随机分配
    # （campaign_codes_technical_design.md §4.1）。
    template = get_mission_template(mission_id)

    write_context_file(account_id, "MISSION.md", template.prose)
    assign_mission(account_id=account_id, mission_id=mission_id)

    logger.info("mission assigned account=%s mission_id=%s", account_id, mission_id)
    return mission_id
