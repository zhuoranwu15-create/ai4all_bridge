"""朝夕关系状态确定性更新：turn 级与天级共用的纯函数 + 编排入口。

见 docs/tech_design/relationship_state_implementation_plan_tmp.md §7/§8.1。
本模块只做**确定性**更新（消息阈值、连续天数、资源风险）；relationship_stage 的
acquainted→deep_bond 以及 trust/growth 由天级 LLM 负责（Phase C），不在此处。
所有写入统一走 app.db.update_account_user_meta_relationship，不碰 companion 字段。
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.db import (
    SHELL_MICROS_PER_SHELL,
    count_inbound_messages,
    get_account_user_meta,
    get_wallet_balance_shell_micros,
    list_recent_inbound_message_dates,
    update_account_user_meta_relationship,
)
from app.agent_runtime.llm.service import generate_completion
from app.agent_runtime.llm.providers import TASK_RELATIONSHIP_STATE, tier_for_task
from app.products.zhaoxi.application.prompts.relationship import (
    build_relationship_eval_prompt,
    parse_relationship_payload,
)

logger = logging.getLogger("ai4all.relationship_state")

# 资源风险阈值：欠费超过 500 贝壳（余额低于 -500 贝壳）。
RESOURCE_RISK_THRESHOLD_MICROS = -500 * SHELL_MICROS_PER_SHELL

# relationship_stage 破冰→相识的累计入站消息阈值。
STAGE_ACQUAINTED_INBOUND_THRESHOLD = 30

# 默认值（与 DB 默认、§3 保持一致）。
_DEFAULT_STAGE = "icebreaking"
_DEFAULT_SURVIVAL = "cooling"
_DEFAULT_TRUST = "building"
_DEFAULT_GROWTH = "not_started"

_ACTIVITY_STATES = {"healthy", "cooling", "inactive"}

# 短中文标签（§3 表），用于 Admin 可读视图渲染。
_STAGE_VIEW_LABELS = {"icebreaking": "破冰", "acquainted": "相识", "deep_bond": "挚友/热恋"}
_SURVIVAL_VIEW_LABELS = {
    "healthy": "健康",
    "cooling": "冷却",
    "inactive": "失活",
    "resource_risk": "资源风险",
}
_TRUST_VIEW_LABELS = {"building": "建立中", "stable": "稳定", "damaged": "受损"}
_GROWTH_VIEW_LABELS = {"not_started": "未开始", "emerging": "有苗头", "stable": "稳定发生"}


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------
def compute_stage_threshold(*, current_stage: str, inbound_count: int) -> str:
    """仅当 icebreaking 且累计入站消息 > 30 时升级到 acquainted；否则保持不变。

    不降级 acquainted/deep_bond——更高阶段由天级 LLM 维护。
    """
    if current_stage == "icebreaking" and inbound_count > STAGE_ACQUAINTED_INBOUND_THRESHOLD:
        return "acquainted"
    return current_stage


def _window_dates(snapshot_date: str, n: int) -> set:
    """[snapshot_date-(n-1), snapshot_date] 闭区间的 n 个北京自然日（YYYY-MM-DD）。"""
    end = date.fromisoformat(snapshot_date)
    return {(end - timedelta(days=i)).isoformat() for i in range(n)}


def compute_survival_status(
    *,
    current_status: str,
    inbound_dates: List[str],
    snapshot_date: str,
    balance_micros: Optional[int],
) -> str:
    """按 §8.1 计算 agent_need_survival_status：转移规则 + 资源风险叠加。

    - 资源风险（余额低于阈值）独立叠加，优先级最高，覆盖活跃态。
    - 活跃态按下列顺序取首个命中，全不命中保留当前活跃态：
      最近 30 日无入站→inactive；最近 2 日均有入站→healthy；
      当前 healthy 且最近 3 日无入站→cooling。
    - current 为 resource_risk（叠加态、非活跃态）时，回退中性默认 cooling 再按规则重算，
      以便资源风险解除后能重新得出 healthy/cooling/inactive。
    """
    if balance_micros is not None and balance_micros < RESOURCE_RISK_THRESHOLD_MICROS:
        return "resource_risk"

    effective = current_status if current_status in _ACTIVITY_STATES else _DEFAULT_SURVIVAL
    dates = set(inbound_dates or [])
    if not (dates & _window_dates(snapshot_date, 30)):
        return "inactive"
    if _window_dates(snapshot_date, 2) <= dates:
        return "healthy"
    if effective == "healthy" and not (dates & _window_dates(snapshot_date, 3)):
        return "cooling"
    return effective


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------
def maybe_update_relationship_state_after_turn(*, account_id: str) -> Dict[str, Any]:
    """turn 后确定性更新（非阻塞、失败只记日志）：

    1. icebreaking 且累计入站 > 30 → acquainted。
    2. 余额低于资源风险阈值 → resource_risk（只叠加、不清除；清除由天级任务负责）。
    不做 deep_bond / trust / growth / survival 连续天数更新（属天级任务）。
    """
    summary: Dict[str, Any] = {"account_id": account_id, "changed": False}
    try:
        meta = get_account_user_meta(account_id=account_id)
        current_stage = (meta or {}).get("relationship_stage") or _DEFAULT_STAGE
        current_survival = (meta or {}).get("agent_need_survival_status") or _DEFAULT_SURVIVAL

        stage_update: Optional[str] = None
        if current_stage == "icebreaking":
            inbound_count = count_inbound_messages(account_id=account_id)
            new_stage = compute_stage_threshold(
                current_stage=current_stage, inbound_count=inbound_count
            )
            if new_stage != current_stage:
                stage_update = new_stage

        survival_update: Optional[str] = None
        balance = get_wallet_balance_shell_micros(account_id=account_id)
        if (
            balance is not None
            and balance < RESOURCE_RISK_THRESHOLD_MICROS
            and current_survival != "resource_risk"
        ):
            survival_update = "resource_risk"

        if stage_update is None and survival_update is None:
            return summary

        update_account_user_meta_relationship(
            account_id=account_id,
            relationship_stage=stage_update,
            agent_need_survival_status=survival_update,
        )
        summary.update(
            changed=True,
            relationship_stage=stage_update,
            agent_need_survival_status=survival_update,
        )
        return summary
    except Exception as err:  # noqa: BLE001 - turn 后台任务不得影响主回复
        logger.warning(
            "post-turn relationship update failed account=%s error=%s", account_id, err
        )
        summary["error"] = str(err)
        return summary


def apply_daily_deterministic_relationship(
    *, account_id: str, snapshot_date: str, current_meta: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """天级确定性更新：计算并写入 relationship_stage 与 agent_need_survival_status。

    返回更新后的四个关系状态（trust/growth 原样透传，留待天级 LLM 更新），
    供调用方写入每日快照。不抛出由 LLM 字段引发的副作用。
    """
    current_stage = (current_meta or {}).get("relationship_stage") or _DEFAULT_STAGE
    current_survival = (current_meta or {}).get("agent_need_survival_status") or _DEFAULT_SURVIVAL
    trust = (current_meta or {}).get("agent_need_trust_status") or _DEFAULT_TRUST
    growth = (current_meta or {}).get("agent_need_growth_status") or _DEFAULT_GROWTH

    inbound_count = count_inbound_messages(account_id=account_id)
    new_stage = compute_stage_threshold(
        current_stage=current_stage, inbound_count=inbound_count
    )

    since_date = (date.fromisoformat(snapshot_date) - timedelta(days=29)).isoformat()
    inbound_dates = list_recent_inbound_message_dates(
        account_id=account_id, since_date=since_date
    )
    balance = get_wallet_balance_shell_micros(account_id=account_id)
    new_survival = compute_survival_status(
        current_status=current_survival,
        inbound_dates=inbound_dates,
        snapshot_date=snapshot_date,
        balance_micros=balance,
    )

    update_account_user_meta_relationship(
        account_id=account_id,
        relationship_stage=new_stage,
        agent_need_survival_status=new_survival,
    )
    return {
        "relationship_stage": new_stage,
        "agent_need_survival_status": new_survival,
        "agent_need_trust_status": trust,
        "agent_need_growth_status": growth,
    }


# ---------------------------------------------------------------------------
# 天级 LLM（Phase C）：relationship_stage(仅 acquainted→deep_bond) / trust / growth
# ---------------------------------------------------------------------------
def merge_llm_relationship(
    *, deterministic: Dict[str, Any], llm_out: Dict[str, Optional[str]]
) -> Dict[str, Any]:
    """把 LLM 结果合并进确定性结果，返回最终四个关系状态。

    - relationship_stage：仅放行 acquainted→deep_bond，其余保留 deterministic（禁越级/降级）。
    - trust / growth：LLM 给出合法值则采用，否则（None）保留当前值。
    - survival：恒取 deterministic，LLM 不参与（§8.2）。
    """
    det_stage = deterministic["relationship_stage"]
    llm_stage = llm_out.get("relationship_stage")
    stage = "deep_bond" if (det_stage == "acquainted" and llm_stage == "deep_bond") else det_stage

    trust = llm_out.get("agent_need_trust_status") or deterministic["agent_need_trust_status"]
    growth = llm_out.get("agent_need_growth_status") or deterministic["agent_need_growth_status"]
    return {
        "relationship_stage": stage,
        "agent_need_survival_status": deterministic["agent_need_survival_status"],
        "agent_need_trust_status": trust,
        "agent_need_growth_status": growth,
    }


def classify_relationship_state_llm(
    *, messages: List[Dict[str, Any]], current_state: Dict[str, Any]
) -> Dict[str, Optional[str]]:
    """调默认 LLM 评估关系三字段，返回归一化结果（字段值为合法枚举或 None）。

    镜像 classify_companion_type：无消息或 LLM/JSON 失败时抛异常。
    """
    if not messages:
        raise ValueError("messages are required")
    prompt = build_relationship_eval_prompt(messages=messages, current_state=current_state)
    raw = generate_completion(
        [{"role": "user", "content": prompt}],
        tier=tier_for_task(TASK_RELATIONSHIP_STATE),
    )
    return parse_relationship_payload(raw)


def apply_daily_llm_relationship(
    *,
    account_id: str,
    deterministic: Dict[str, Any],
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """天级 LLM 更新：classify → merge → 写入 stage/trust/growth（不碰 survival）→ 返回最终四值。

    LLM 失败向上抛出，由调用方（scheduler）兜底保留确定性结果。
    """
    llm_out = classify_relationship_state_llm(messages=messages, current_state=deterministic)
    merged = merge_llm_relationship(deterministic=deterministic, llm_out=llm_out)
    update_account_user_meta_relationship(
        account_id=account_id,
        relationship_stage=merged["relationship_stage"],
        agent_need_trust_status=merged["agent_need_trust_status"],
        agent_need_growth_status=merged["agent_need_growth_status"],
    )
    return merged


# ---------------------------------------------------------------------------
# Admin 可读视图（Phase D）：由 DB 状态渲染，不写回 profile_storage
# ---------------------------------------------------------------------------
def render_relationship_view(meta: Optional[Dict[str, Any]]) -> str:
    """把账号关系四状态渲染成人类可读的 RELATIONSHIP markdown 视图。

    meta 为 None（账号尚未生成 meta）时按 DB 默认值渲染。供 Admin /meta 即时展示，
    不作为 source of truth、也不写回 profile_storage。
    """
    m = meta or {}
    stage = m.get("relationship_stage") or _DEFAULT_STAGE
    survival = m.get("agent_need_survival_status") or _DEFAULT_SURVIVAL
    trust = m.get("agent_need_trust_status") or _DEFAULT_TRUST
    growth = m.get("agent_need_growth_status") or _DEFAULT_GROWTH
    return (
        "# RELATIONSHIP\n\n"
        "## 关系阶段\n\n"
        f"- 当前阶段：{_STAGE_VIEW_LABELS.get(stage, stage)}\n\n"
        "## 需求满足情况\n\n"
        f"- 生存 / 活跃：{_SURVIVAL_VIEW_LABELS.get(survival, survival)}\n"
        f"- 信任与尊重：{_TRUST_VIEW_LABELS.get(trust, trust)}\n"
        f"- 共同成长：{_GROWTH_VIEW_LABELS.get(growth, growth)}\n"
    )
