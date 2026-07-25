"""朝夕使命分配编排（agent_mission_and_orchestration_design.md §5）。

分配时机只有两处：① onboarding 完成时（新账号，见 app/turn_service.py）；② 存量账号
一次性回填（scripts/backfill_account_missions.py）。两处都只应调用
assign_mission_if_absent，不直接操作 app.db.mission / app.products.zhaoxi.infrastructure.profiles 的底层写入。
"""
import hashlib
import logging

from app.db.campaign import get_campaign_attribution
from app.db.mission import assign_mission, get_account_mission
from app.products.zhaoxi.domain.missions.registry import get_mission_template, list_mission_templates
from app.products.zhaoxi.infrastructure.profiles import write_context_file

logger = logging.getLogger("ai4all.mission_assignment")


def _pick_mission_id(account_id: str) -> str:
    """账号哈希轮询：同一账号结果稳定（幂等重试不换模板），且在已注册模板间大致均匀分布。"""
    templates = list_mission_templates()
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    index = int(digest, 16) % len(templates)
    return templates[index].id


def assign_mission_if_absent(*, account_id: str) -> str:
    """若账号尚未分配使命则分配一个并写入 MISSION.md；已分配则直接返回现有 mission_id。

    幂等，可安全地在 onboarding 完成时调用，也可在存量账号回填脚本中重复调用。

    写入顺序刻意为「先写 MISSION.md prose，再写 account_mission DB 行」：
    _pick_mission_id 是账号哈希的纯函数，同账号任何时候重算结果都相同，所以即便进程
    在两步之间崩溃，下次调用仍会算出同一个 mission_id、重新执行两步（file 覆盖同样内容
    是幂等的，DB insert 靠 ON CONFLICT DO NOTHING 幂等）。反过来若先写 DB 后写文件，
    崩溃会导致 get_account_mission 提前判定「已分配」而永远跳过 MISSION.md 写入，
    留下 DB 有分配、文件仍是「暂无」占位符的不一致状态。
    """
    existing = get_account_mission(account_id=account_id)
    if existing is not None:
        return existing["mission_id"]

    # 营销活码若在注册时指定了 mission_id，优先命中；否则回退现有哈希随机分配
    # （campaign_codes_technical_design.md §4.1）。
    attribution = get_campaign_attribution(account_id=account_id)
    mission_id = (attribution or {}).get("mission_id") or _pick_mission_id(account_id)
    template = get_mission_template(mission_id)

    write_context_file(account_id, "MISSION.md", template.prose)
    assign_mission(account_id=account_id, mission_id=mission_id)

    logger.info("mission assigned account=%s mission_id=%s", account_id, mission_id)
    return mission_id
