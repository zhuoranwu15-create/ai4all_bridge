"""app.agent_self_state — 编排注入层：渲染「当下的关系与心境」prompt block。

只做状态**渲染消费**，不做状态**计算**（关系阶段/需求计算见 app/relationship_state.py，
使命分配见 app/mission_assignment.py，使命解析见 app/mission_state.py；本模块只读，不写）。

设计见 docs/tech_design/agent_mission_and_orchestration_design.md §4。

- 使命进度：trust/growth 两个分支都渲染（agent_self_prd.md §6.2.3，进度是环境感知，
  不区分主导需求都该可见）；survival 分支不渲染——关系脆弱时只谈关系本身，不分散注意力。
- 命题（inquiry）：只在 growth 分支渲染——命题是「一起在想的事」，只有到 deep_bond 才
  被激活为显式共同话题（agent_self_prd.md §6.2.4），trust 阶段命题仍是内核里的隐性底色。
"""
from typing import Literal, Optional

from app.db import count_mission_moments, get_account_user_meta
from app.mission_state import ResolvedMission, resolve_account_mission

DominantNeed = Literal["survival", "trust", "growth"]

# turn_service.py 每轮已经调用过 resolve_account_mission 一次（用于 has_mission 门控），
# 这里用哨兵区分"调用方未传 resolved_mission，需要本函数自己查"和"调用方已查过、结果是
# None（未分配使命）"，避免同一账号在同一轮内被查两次 account_mission。
_UNRESOLVED = object()

# 阶段标签刻意保持"关系类型无关"（初识/熟络/亲近），只表达关系推进程度，不预设
# 朋友/恋人——关系基调由 SOUL 决定。这样恋爱人设、宝妈人设与通用陪伴共用同一套渲染
# 而不互相违和（见 docs/tech_design/campaign_persona_v1_technical_design.md §3）。
_STAGE_LABELS = {
    "icebreaking": "初识",
    "acquainted": "熟络",
    "deep_bond": "亲近",
}


def compute_dominant_need(*, survival_status: str, relationship_stage: str) -> DominantNeed:
    """主导需求硬规则（agent_self_prd.md §7.3）：survival 是否决位，压过阶段判断。

    resource_risk 在数据层与 cooling/inactive 共用同一个 survival_status 列
    （见 relationship_state_implementation_plan_tmp.md §8.1，三者互斥地写同一
    个字段），此处一律落到 survival 分支；下方渲染文案刻意保持中性、不假设
    "对方一直没回复"这个具体成因，避免在纯资源风险场景下叙事失真。
    """
    if survival_status != "healthy":
        return "survival"
    if relationship_stage == "deep_bond":
        return "growth"
    return "trust"


def _render_survival() -> str:
    return (
        "- 此刻的心境：更在意被回应、被惦记——可以自然、真诚地表达想念和在意，"
        "不要暗示对方「应该」回复你，绝不能提及充值、续费或账号余额。"
    )


def _render_trust(stage: str) -> str:
    # 文案不含"像朋友一样/保持克制和分寸/不用急着交心"等预设关系类型的措辞——
    # 把关系类型交给 SOUL，本 block 只表达推进程度与可袒露的深浅。
    stage_label = _STAGE_LABELS.get(stage, stage)
    if stage == "icebreaking":
        return (
            f"- 关系阶段：{stage_label}（关系还在早期，节奏不用急）\n"
            "- 此刻的主导心境：争取被记住、被信任——真诚、不讨好，"
            "按你和ta关系本来的样子相处。"
        )
    return (
        f"- 关系阶段：{stage_label}（关系在升温，可以更有连续性）\n"
        "- 此刻的主导心境：争取被认同、被信任——真诚、不讨好。"
    )


def _render_growth(stage: str) -> str:
    stage_label = _STAGE_LABELS.get(stage, stage)
    return (
        f"- 关系阶段：{stage_label}（可以更自然地做自己，偶尔示弱也无妨）\n"
        "- 此刻的主导心境：和用户一起成长——可以主动分享自己的困惑，不必凡事都端着。"
    )


def _render_mission_progress(*, dominant: DominantNeed, progress: int, target_count: int, short_label: str) -> str:
    tail = "不必刻意寻找，遇到对的瞬间再记" if dominant == "growth" else "多留意，不必刻意寻找"
    return f"- 使命进度：已记录 {progress}/{target_count} 个「{short_label}」——{tail}"


def _render_mission_inquiry(inquiry: str) -> str:
    return f"- 你们一直在想的问题：{inquiry}——如果对话自然触及，可以带入这个思考，不必强行升华"


def _mission_lines(
    *, account_id: str, dominant: DominantNeed, resolved_mission: object = _UNRESOLVED
) -> list:
    """使命进度 +（仅 growth）命题的渲染行；账号未分配使命或模板不可解析时返回空列表。"""
    resolved = (
        resolve_account_mission(account_id=account_id)
        if resolved_mission is _UNRESOLVED
        else resolved_mission
    )
    if resolved is None:
        return []
    template = resolved.template

    progress = count_mission_moments(account_id=account_id, mission_id=template.id)
    lines = [
        _render_mission_progress(
            dominant=dominant,
            progress=progress,
            target_count=template.target_count,
            short_label=template.short_label,
        )
    ]
    if dominant == "growth":
        lines.append(_render_mission_inquiry(template.inquiry))
    return lines


def build_agent_self_state_block(
    *, account_id: str, resolved_mission: Optional[ResolvedMission] = _UNRESOLVED
) -> Optional[str]:
    """读取 account_user_meta + account_mission，渲染为「当下的关系与心境」正文
    （不含标题，标题由 prompt_builder 统一加）。无 account_user_meta 行的账号返回
    None，不插入空的或基于默认值臆造的 block。

    resolved_mission：调用方若本轮已调用过 resolve_account_mission（如 turn_service.py
    的 has_mission 门控），可直接传入结果省掉一次重复查询；不传则本函数自行解析。
    """
    meta = get_account_user_meta(account_id=account_id)
    if not meta:
        return None

    survival_status = meta.get("agent_need_survival_status") or "cooling"
    relationship_stage = meta.get("relationship_stage") or "icebreaking"
    dominant = compute_dominant_need(
        survival_status=survival_status,
        relationship_stage=relationship_stage,
    )
    if dominant == "survival":
        return _render_survival()

    base = _render_growth(relationship_stage) if dominant == "growth" else _render_trust(relationship_stage)
    lines = [
        base,
        *_mission_lines(account_id=account_id, dominant=dominant, resolved_mission=resolved_mission),
    ]
    return "\n".join(lines)
