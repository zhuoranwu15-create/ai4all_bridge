import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.time_utils import (
    beijing_now,
    beijing_daypart_str,
    beijing_weekday_str,
    format_history_timestamp,
)
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

if TYPE_CHECKING:
    from app.agent_runtime.ports import MemorySink

from app.agent_self_state import build_agent_self_state_block
from app.channels import CHANNEL_APP, CHANNEL_WEB, CHANNEL_WEIXIN, ChannelCapability, get_channel_capability
from app.config import settings
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    clear_session_messages,
    connect as db_connect,
    count_inbound_messages_for_account,
    get_account_id_for_session_key,
    get_account_onboarding_state,
    get_active_content_invitation,
    get_daily_usage,
    get_duplicate_reply,
    get_platform_user_id_for_account,
    increment_session_turn_count,
    reserve_daily_quota,
    confirm_daily_quota,
    rollback_daily_quota,
    insert_debug_trace,
    insert_message,
    list_context_messages_for_session,
    list_recent_context_messages_for_session,
    mark_message_moderation_blocked,
    process_referral_message_for_account,
    record_chat_usage_charge,
    record_image_understanding_charge,
    resolve_account_id_for_inbound_channel_identity,
    resolve_effective_quota_limits,
    resolve_node_for_account,
    set_account_onboarding_state,
    upsert_channel_binding,
)
from app.identity import ResolvedIdentity, identity_response_metadata, resolve_openclaw_identity
from app.image_understanding import describe_image
from app.llm import generate_reply, generate_reply_with_tools, resolve_active_llm_provider
from app.llm_providers import TASK_MAIN_REPLY, tier_for_task
from app.db.campaign import get_campaign_attribution
from app.mission_assignment import assign_mission_if_absent
from app.mission_state import resolve_account_mission
from app.llm_providers import LLMProviderConfig
from app.memory_writer import write_memory
from app.relationship_state import maybe_update_relationship_state_after_turn
from app.moderation.sensitive_words import check_sync_guard
from app.moderation.service import (
    create_sync_block_task,
    enqueue_message_for_moderation,
    screen_inbound_message_sync,
)
from app.context_window import compute_floor_count, trim_history_rows
from app.prompt_builder import ContextBlock, PromptBuilder, extract_section
from app.proactive.store.account_state import ensure_account_state
from app.rate_limiter import rate_limiter
from app.schemas import MediaPayload, OpenClawTurnRequest, OpenClawTurnResponse
from app.tools import get_default_tools, iter_specs
from app.turn_context import TurnContext
from app.session_lifecycle import business_day_for, get_or_create_account_active_session_with_dreaming
from app.onboarding import (
    apply_extracted_onboarding_info,
    build_onboarding_prompt_context,
    extract_onboarding_info_async,
    is_onboarding_active,
    next_onboarding_state,
    ONBOARDING_COMPLETE,
    ONBOARDING_PENDING,
    ONBOARDING_STEP1_SENT,
    ONBOARDING_STEP2_SENT,
    ONBOARDING_STEP3_SENT,
    ONBOARDING_WELCOME_TEXT,
)
from app import node_gateway
from app.user_profiles import (
    ensure_agent_context_files,
    ensure_user_profile,
    read_agent_context,
    read_user_profile,
)


logger = logging.getLogger("ai4all.turn_service")

_SPECIAL_COMMANDS = {"#重置会话", "#状态"}
# 组装期取"水位线之后全部消息"的宽松上限（rolling 开）。正常态由后台 chunk 压缩把水位线之后 token
# 维持在预算内、条数远低于此；此值仅作极端（压缩长期失败 + 大量碎消息）下的防爆兜底。
_ABOVE_WATERMARK_FETCH_LIMIT = 2000
# 组装期能容忍原文尾窗超预算多少倍：仅当后台压缩长期落后时才由此硬顶从最旧端丢弃（floor 仍护最近），
# 防 prompt 无界膨胀。正常态压缩把尾窗维持在预算内，永不触发。
_HARD_CEILING_BUDGET_MULTIPLIER = 2
_GENERATION_ERROR_REPLY = "我这边刚刚有点卡住了，你可以稍后再发我一次。"

# TDAI 记忆功能（recall + 主动 search 工具）的消息数自然灰度：一旦某账号累计 inbound
# 消息数越过 tdai_memory_min_messages 阈值，即固化进该集合，后续 turn 直接短路、不再
# COUNT。阈值是启动期 Settings（改需重启），eligibility 在固定阈值下单调递增，故进程内
# sticky 缓存始终一致。
_tdai_memory_eligible_accounts: set = set()


def _tdai_memory_volume_eligible(account_id: str) -> bool:
    """账号累计 inbound 消息数是否达到 TDAI 记忆灰度阈值（含进程内 sticky 缓存）。

    阈值 <= 0 表示关闭该通道（只认 allowlist），恒返回 False。
    """
    threshold = int(getattr(settings, "tdai_memory_min_messages", 0) or 0)
    if threshold <= 0:
        return False
    if account_id in _tdai_memory_eligible_accounts:
        return True
    count = count_inbound_messages_for_account(account_id=account_id)
    if count >= threshold:
        _tdai_memory_eligible_accounts.add(account_id)
        return True
    return False


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _record_timing(timings: Dict[str, int], key: str, started_at: float) -> int:
    value = _elapsed_ms(started_at)
    timings[key] = value
    return value


def _schedule_on_loop(loop: asyncio.AbstractEventLoop, coro) -> bool:
    """把一个协程线程安全地调度到后台事件循环执行；返回是否成功挂上。

    shutdown 竞态：``loop`` 在 startup 写入后可能先于本调用被关闭，``call_soon_threadsafe``
    对已关闭 loop 会抛 ``RuntimeError``。这里先查 ``is_closed()`` 兜住常态，再对「检查后到
    调用间被关闭」的窗口 catch ``RuntimeError``；两种情况都主动 ``close()`` 掉未被调度的协程，
    既避免 "coroutine was never awaited" 告警，也记一条 warning 让 after-turn 工作被跳过可观测。
    """
    if loop.is_closed():
        coro.close()
        logger.warning("after-turn task skipped: background loop closed (shutdown)")
        return False
    try:
        loop.call_soon_threadsafe(loop.create_task, coro)
        return True
    except RuntimeError as err:
        # loop 在 is_closed() 检查之后、调度之前被关闭（关停竞态窗口）。
        coro.close()
        logger.warning(
            "after-turn task skipped: background loop closed mid-schedule (%s)", err
        )
        return False


def _log_turn_timing(
    *,
    ctx: "ChannelTurnInput",
    timings: Dict[str, int],
    started_at: float,
    status: str,
    account_id: Optional[str] = None,
    session: Optional[str] = None,
    message_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Emit one structured-enough timing line per inbound turn without message text."""
    logger.info(
        "openclaw_turn timing account=%s session=%s message_id=%s status=%s "
        "total_ms=%s reply_ready_ms=%s timings=%s error=%s",
        account_id,
        session or ctx.identity.session_key,
        message_id or ctx.message_id or ctx.event_id,
        status,
        _elapsed_ms(started_at),
        timings.get("reply_ready_ms"),
        timings,
        error,
    )

# 检测用户是否在表达"更新主动消息设置"的意图。命中时主对话首轮强制对应工具，避免
# DeepSeek 先反问确认。**仅供主对话 turn 路径调用**：通用 LLM 入口 generate_reply_with_tools
# 不再内置此判定，主动消息生成路径也不调用它，从而不会把内嵌的历史聊天文本误判成当前
# 意图（曾导致强制一个该路径 tools 不含的工具 → DeepSeek 400）。
_PROACTIVE_COUNT_RE = r"(?:[0-9０-９]+|[一二两三四五六七八九十]+)"
_PROACTIVE_UPDATE_RE = re.compile(
    # frequency: "每天最多1条" / "总共2条" / "一周3次"
    rf"(每天|每日|一天|总共|一周|每周)\s*(最多|最少|只|就)?\s*发?\s*{_PROACTIVE_COUNT_RE}\s*(条|次)"
    # e.g. "条数改为3" / "上限设为2" / "改为3条"
    rf"|(条数|上限|频次).{{0,6}}(改为|设为|调整为|改|设|调|限|调整).{{0,8}}{_PROACTIVE_COUNT_RE}"
    rf"|(改为|设为|调整为|改成|设成).{{0,6}}{_PROACTIVE_COUNT_RE}.{{0,4}}(条|次)"
    rf"|{_PROACTIVE_COUNT_RE}.{{0,5}}(条|次).{{0,8}}(就够|就行|为限|上限|够了)"
    # on/off/mute
    r"|别(再|继续)?(主动|发).{0,10}(消息|找|发)"
    r"|(关掉?|开启?|暂停|停止|恢复).{0,6}主动"
    # action verbs only (exclude noun "设置")
    r"|主动消息.{0,10}(关闭|开启|暂停|停止|修改|调整|改为|设为|限制|减少|增加)"
    r"|(?:主动消息|主动|找我|联系我).{0,8}(?:多|少)发.{0,4}(?:点|些|次|条)"
    r"|(?:多|少)发.{0,4}(?:点|些|次|条).{0,8}(?:主动消息|主动找我|找我|联系我)"
    r"|总(共|量).{0,8}(条数|上限|改为|设为|[0-9０-９])",
    re.IGNORECASE,
)


def _infer_proactive_update_tool_choice(user_text: str) -> Any:
    """命中"主动设置更新"意图时返回强制 tool_choice dict，否则返回 "auto"。"""
    if _PROACTIVE_UPDATE_RE.search(str(user_text or "")):
        logger.debug("proactive_update_intent detected, forcing tool_choice")
        return {"type": "function", "function": {"name": "update_proactive_message_settings"}}
    return "auto"


def _ensure_pending_onboarding_question(reply: str) -> str:
    cleaned = (reply or "").strip()
    if "称呼你" in cleaned or "叫你" in cleaned or "喊你" in cleaned:
        return cleaned
    if not cleaned:
        return ONBOARDING_WELCOME_TEXT
    return f"{cleaned}\n\n{ONBOARDING_WELCOME_TEXT}"


def _extract_onboarding_info_sync(*, user_text: str, current_state: str) -> dict:
    return asyncio.run(
        extract_onboarding_info_async(
            user_text=user_text,
            current_state=current_state,
        )
    )


def _extract_ai_name_from_context(blocks: dict) -> Optional[str]:
    import re
    identity = blocks.get("IDENTITY", "")
    m = re.search(r"AI 名字[:：]\s*(.+)", identity)
    if m:
        return m.group(1).strip()
    return None


def _extract_user_name_from_context(blocks: dict) -> Optional[str]:
    import re
    user = blocks.get("USER", "")
    m = re.search(r"用户称呼[:：]\s*(.+)", user)
    if m:
        return m.group(1).strip()
    return None


def _count_user_name_asks(turn_count: int, state: str) -> int:
    """Approximate how many times we've asked the user's name so far."""
    if state in {ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT, ONBOARDING_COMPLETE}:
        return 1
    return 0


def _tool_instructions(
    *,
    active_content_invitation: Optional[dict] = None,
) -> str:
    if not active_content_invitation:
        return ""

    title_count = len(active_content_invitation.get("title_items") or [])
    instructions = [
        "## 当前内容邀请",
        "",
        f"- 当前存在待回应内容邀请：id={active_content_invitation['id']}，topic={active_content_invitation['topic']}，标题数={title_count}。",
        "- 本轮提供内容邀请回复工具：send_content_invitation_titles、record_content_invitation_feedback。",
    ]
    return "\n".join(instructions)


def _tool_name(schema: Dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")


def _summarize_history_rows(history_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    session_ids = [
        int(row["session_id"])
        for row in history_rows
        if row.get("session_id") is not None
    ]
    return {
        "count": len(history_rows),
        "session_count": len(set(session_ids)),
        "messages": [
            {
                "id": row.get("id"),
                "message_id": row.get("message_id"),
                "session_id": row.get("session_id"),
                "role": row.get("role"),
                "chars": len(str(row.get("content") or "")),
            }
            for row in history_rows
        ],
    }


# default_when_flag -> (available 时的 reason, disabled 时的 reason)；新增按 flag 门控的
# 工具只需在此加一行，不必再手写一对 elif 分支（曾经是两条要手动保持同步的 elif 链）。
_TOOL_GATE_REASONS: Dict[str, tuple] = {
    "web_search_enabled": ("web_search_enabled", "web_search_disabled"),
    "content_invitation_response_enabled": ("active_content_invitation", "no_active_content_invitation"),
    "has_mission": ("has_mission", "no_mission_assigned"),
    "tdai_search_enabled": ("tdai_search_enabled", "tdai_search_disabled"),
}

# 「产生未来投递」的工具：为提醒/承诺埋下将来主动外呼的动作。渠道不支持主动消息
# （ChannelCapability.supports_proactive=False，如 Web V1）时从工具集剔除——否则模型会
# 承诺一条永不送达的提醒（§8.3）。微信 supports_proactive=True，集合原样不变（字节级等价）。
_PROACTIVE_DELIVERY_TOOLS: frozenset = frozenset(
    {"create_reminder", "create_commitment", "update_reminder", "list_reminders", "cancel_reminder"}
)


def _build_tooling_envelope(
    *,
    onboarding_active: bool,
    web_search_enabled: bool,
    active_content_invitation: Optional[dict],
    has_mission: bool,
    tdai_search_enabled: bool = False,
    text: str,
    include_tool_instructions: bool,
    cap: ChannelCapability,
) -> Dict[str, Any]:
    """Return tool schemas plus debug metadata for the main chat tool set.

    ``cap`` 携带渠道能力：``supports_proactive=False`` 的渠道剔除「产生未来投递」工具
    （见 ``_PROACTIVE_DELIVERY_TOOLS``）。微信 cap 全 True → 工具集与历史逐字节一致。
    """
    content_invitation_enabled = bool(active_content_invitation)
    tools: List[Dict[str, Any]] = []
    available: List[Dict[str, Any]] = []
    disabled: List[Dict[str, Any]] = []
    enabled_names: set[str] = set()

    if not onboarding_active:
        tools = get_default_tools(
            web_search_enabled=web_search_enabled,
            content_invitation_response_enabled=content_invitation_enabled,
            has_mission=has_mission,
            tdai_search_enabled=tdai_search_enabled,
        )
        if not cap.supports_proactive:
            tools = [s for s in tools if _tool_name(s) not in _PROACTIVE_DELIVERY_TOOLS]
        enabled_names = {_tool_name(schema) for schema in tools}

    for spec in iter_specs():
        if spec.default_when_flag == "never":
            continue
        if onboarding_active:
            disabled.append(
                {
                    "name": spec.name,
                    "group": spec.group,
                    "reason": "onboarding_active",
                }
            )
            continue
        if spec.name in enabled_names:
            reason = _TOOL_GATE_REASONS.get(spec.default_when_flag, ("default", "disabled"))[0]
            available.append(
                {
                    "name": spec.name,
                    "group": spec.group,
                    "reason": reason,
                    "schema": spec.schema,
                }
            )
            continue
        reason = _TOOL_GATE_REASONS.get(spec.default_when_flag, ("default", "disabled"))[1]
        disabled.append(
            {
                "name": spec.name,
                "group": spec.group,
                "reason": reason,
            }
        )

    first_round_tool_choice: Any = "none" if onboarding_active else _infer_proactive_update_tool_choice(text)
    return {
        "mode": "plain" if onboarding_active else "tools",
        "tools": tools,
        "available_tools": available,
        "disabled_tools": disabled,
        "available_tool_names": [_tool_name(schema) for schema in tools],
        "first_round_tool_choice": first_round_tool_choice,
        "max_tool_rounds": int(getattr(settings, "llm_max_tool_rounds", 3) or 3),
        "include_tool_instructions": bool(include_tool_instructions and not onboarding_active),
        "web_search_enabled": web_search_enabled,
        "content_invitation_response_enabled": content_invitation_enabled,
        "active_content_invitation_id": active_content_invitation.get("id") if active_content_invitation else None,
        "has_mission": has_mission,
    }


def _wrap_current_message_envelope(messages: List[Dict[str, Any]], message_type: str) -> None:
    """将 messages 里最后一条 user 消息包裹进 typed envelope（原地修改）。

    格式：<current_message type="...">原文</current_message>
    落库的 content 不变，只在 LLM feed 里加标注。
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            content = messages[i].get("content") or ""
            safe_type = (message_type or "text").replace('"', "")
            messages[i] = dict(messages[i])  # 避免修改 history 列表里的原对象
            messages[i]["content"] = (
                f'<current_message type="{safe_type}">\n{content}\n</current_message>'
            )
            return


def build_turn_llm_input(
    *,
    account_id: str,
    account: Dict[str, Any],
    session: Dict[str, Any],
    profile: Dict[str, Any],
    text: str,
    today: str,
    current_time: Optional[str] = None,
    onboarding_state: str = "",
    onboarding_active: bool,
    web_search_enabled: bool,
    tdai_search_enabled: bool = False,
    force_web_search_enabled: Optional[bool] = None,
    onboarding_pre_written: Optional[Dict[str, Any]] = None,
    onboarding_pre_extracted: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    include_tool_instructions: bool = True,
    debug_dry_run: bool = False,
    llm_provider: Optional[LLMProviderConfig] = None,
    message_type: str = "text",
    extra_blocks: Optional[List[ContextBlock]] = None,
    cap: Optional[ChannelCapability] = None,
    channel: str = CHANNEL_WEIXIN,
) -> Dict[str, Any]:
    """Build the exact LLM input envelope for a chat turn.

    This helper has no persistence side effects: callers that need onboarding
    pre-writes or message insertion must do that before invoking it.

    ``cap`` 选定渠道能力（工具集裁剪等）；缺省回落微信 cap，故既有调用方（debug/脚本/
    测试）行为逐字节不变。``channel`` 为渠道真实值（如 openclaw-weixin/native），决定
    AGENTS/TOOLS 渠道变体与 IDENTITY 播种口径；缺省 weixin 亦字节级等价现状。
    """
    _now = now or beijing_now()
    cap = cap or get_channel_capability(CHANNEL_WEIXIN)
    selected_llm_provider = llm_provider or resolve_active_llm_provider(tier_for_task(TASK_MAIN_REPLY))
    current_session_id = int(session["id"])
    # L0 原始尾窗：统一编排后改为 **session-scoped**（不再跨 session）——轮转即真正重置原文，
    # 跨 session 的长期连续性交给 rolling_summary（其 seed 为上一段 dreaming 的 carryover）。
    _rolling_enabled = getattr(settings, "llm_rolling_summary_enabled", True)
    _budget = int(getattr(settings, "llm_context_token_budget", 0) or 0)
    _watermark = int(session.get("rolling_summary_upto_id") or 0)
    if _rolling_enabled:
        # 水位线不变量：摘要覆盖到水位线，原文 = 水位线**之后**的全部消息——**永不丢弃尚未进摘要
        # 的消息**（关掉「已丢出窗口但还没压缩」的信息缺口）。故按 after_id=水位线 取**其后全部**消息
        # （而非"最近 N 条"——否则水位线老、其后消息条数超过 N 时最老的未摘要消息会漏掉）。取数上限
        # 设一个宽松值；正常态由后台 chunk 压缩把水位线之后 token 维持在预算内、条数远低于此。
        # 预算不靠组装期丢弃维持；若压缩持续失败致 raw 无界，用 2× 预算硬顶兜底（floor 仍护最近）。
        pool = list_context_messages_for_session(
            session_id=current_session_id,
            account_id=account_id,
            after_id=_watermark,
            limit=_ABOVE_WATERMARK_FETCH_LIMIT,
        )
        if not pool:
            # 兜底：水位线 ≥ 最新消息（异常）→ 至少保留最后一条原文。
            pool = list_recent_context_messages_for_session(
                session_id=current_session_id,
                account_id=account_id,
                limit=1,
            )
        effective_budget = _budget * _HARD_CEILING_BUDGET_MULTIPLIER if _budget > 0 else 0
    else:
        # rolling 关闭（无摘要兜底）：取最近 N 条 + 按 token 预算从最旧端整条丢弃 + 硬底，防 prompt 无界。
        pool = list_recent_context_messages_for_session(
            session_id=current_session_id,
            account_id=account_id,
            limit=settings.llm_context_messages,
        )
        effective_budget = _budget
    # 最近「硬底」：距 now ≤ N 分钟 且 ≤ M 轮 的原文永不被丢弃（硬底优先于 token 预算，可短暂超）。
    floor_count = compute_floor_count(
        pool,
        now=_now,
        floor_minutes=int(getattr(settings, "llm_context_floor_minutes", 15) or 0),
        floor_turns=int(getattr(settings, "llm_context_floor_turns", 10) or 0),
    )
    # 单消息硬上限 + （仅在需要时）token 裁剪。tool_evidence / history_metadata 基于裁剪后的 kept_rows。
    trim_result = trim_history_rows(
        pool,
        token_budget=effective_budget,
        per_message_max_chars=int(getattr(settings, "llm_context_message_max_chars", 0) or 0),
        min_keep=floor_count,
    )
    kept_rows = trim_result["kept"]
    # L0 历史时间戳：给历史 user 轮的 content 前缀绝对时间戳 `[周一 2026-07-06 11:39]`
    # （仅作用于喂 LLM 的副本，落库 content 不变；assistant 不盖）。当前一轮的 user 消息已
    # 由 <current_message> envelope + 运行时块覆盖 now，故跳过它，避免与 runtime 冗余——
    # 生产路径当前消息已预插入、是 kept_rows 里最后一条 user 行；debug_dry_run 时当前文本另行
    # 追加（见下方），history 内全是历史消息，故不跳过。前缀在 token/字符裁剪之后注入，几乎不占预算。
    _history_timestamp_enabled = getattr(settings, "llm_history_timestamp_enabled", True)
    _skip_current_user_idx = None
    if _history_timestamp_enabled and not debug_dry_run:
        for _i in range(len(kept_rows) - 1, -1, -1):
            if kept_rows[_i].get("role") == "user":
                _skip_current_user_idx = _i
                break
    history = []
    _history_timestamped_count = 0
    for _i, row in enumerate(kept_rows):
        content = row["content"]
        if (
            _history_timestamp_enabled
            and row.get("role") == "user"
            and _i != _skip_current_user_idx
        ):
            ts = format_history_timestamp(row.get("created_at"))
            if ts:
                content = f"[{ts}]\n{content}"
                _history_timestamped_count += 1
        history.append({"role": row["role"], "content": content})
    if getattr(settings, "llm_tool_evidence_replay_enabled", True):
        from app.tool_evidence_replay import inject_tool_evidence_replay
        # history 由 kept_rows 构建，inject 内部 zip(history, rows) 需 1:1 对齐，故同传 kept_rows。
        history = inject_tool_evidence_replay(
            history,
            kept_rows,
            account_id,
            enabled=True,
            max_turns=int(getattr(settings, "llm_tool_evidence_turns", 2)),
            max_result_chars=int(getattr(settings, "llm_tool_evidence_max_result_chars", 1500)),
        )
    file_profile = read_user_profile(account_id)
    soul = extract_section(file_profile, "Soul")
    user_prefs = extract_section(file_profile, "User Preferences")
    long_term_memory = extract_section(file_profile, "Long-term Memory")
    agent_context = read_agent_context(
        account_id,
        display_name=account.get("display_name"),
        channel=channel,
    )
    # 统一编排：carryover 不再作为独立 block 注入，而是在 session 轮转时 seed 进新 session 的
    # rolling_summary（见 session_lifecycle），此后会话内溢出继续 merge 进同一条水位线。
    # 因此这里只注入单一 rolling_summary（【更早对话摘要】），不再单独处理 carryover / 抑制逻辑。
    rolling_summary = None
    if getattr(settings, "llm_rolling_summary_enabled", True):
        rolling_summary = (session.get("rolling_summary") or "").strip() or None
    history_metadata = _summarize_history_rows(kept_rows)
    carryover_metadata = {
        # carryover 源文仍存在 session 上（dreaming 产出），但已折叠进 rolling 水位线，不再单独注入。
        "source_chars": len(session.get("carryover_summary") or ""),
        "included": False,
        "suppressed_by_history": False,
        "folded_into_rolling": True,
    }

    metadata: Dict[str, Any] = {
        "history_count": len(history),
        "history_cross_session": False,
        "history_session_count": history_metadata["session_count"],
        "history_floor_count": floor_count,
        "history": history_metadata,
        "history_dropped_count": trim_result["metrics"]["dropped_count"],
        "history_watermark_upto_id": _watermark,
        "history_truncated_count": trim_result["metrics"]["truncated_count"],
        "history_timestamped_count": _history_timestamped_count,
        "history_est_tokens": trim_result["metrics"]["kept_est_tokens"],
        "rolling_summary_included": bool(rolling_summary),
        "soul_chars": len(soul),
        "user_prefs_chars": len(user_prefs),
        "long_term_memory_chars": len(long_term_memory),
        "agent_context": agent_context.metadata(),
        "system_prompt_override": bool(profile.get("system_prompt")),
        "style": profile.get("style"),
        "display_name": account.get("display_name"),
        "onboarding_pre_written": onboarding_pre_written or {},
        "onboarding_active": onboarding_active,
        "onboarding_state": onboarding_state,
        "carryover_summary_included": False,
        "carryover_summary_folded_into_rolling": True,
        "carryover": carryover_metadata,
    }
    if debug_dry_run:
        metadata["debug_dry_run"] = True

    active_content_invitation = None
    if not onboarding_active and include_tool_instructions:
        active_content_invitation = get_active_content_invitation(
            account_id=account_id,
            now=_now.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        )
        metadata["active_content_invitation_id"] = (
            active_content_invitation["id"] if active_content_invitation else None
        )
    # has_mission 门控 mission_status/record_mission_moment 两个工具（未分配使命的存量
    # 账号、onboarding 中、或 mission_id 不可解析（脏数据/模板下线）都不出现，不暴露
    # "调了也只会失败"的工具，见 §6.3 与 app.mission_state.resolve_account_mission）。
    resolved_mission = None if onboarding_active else resolve_account_mission(account_id=account_id)
    has_mission = resolved_mission is not None
    metadata["has_mission"] = has_mission
    tooling = _build_tooling_envelope(
        onboarding_active=onboarding_active,
        web_search_enabled=web_search_enabled,
        active_content_invitation=active_content_invitation,
        has_mission=has_mission,
        tdai_search_enabled=tdai_search_enabled,
        text=text,
        include_tool_instructions=include_tool_instructions,
        cap=cap,
    )

    # Onboarding 期间尚未分配使命/关系状态未成形，跳过注入（agent_self_prd.md §4.4）。
    agent_self_state_text = (
        None
        if onboarding_active
        else build_agent_self_state_block(account_id=account_id, resolved_mission=resolved_mission)
    )

    onboarding_ctx = ""
    if onboarding_active:
        # 营销活码归因（若有）：话术自由文本 override + 是否强制指定了 SOUL 人设
        # （campaign_codes_technical_design.md §4.2/§4.3）。
        campaign_attribution = get_campaign_attribution(account_id=account_id)
        onboarding_ctx = build_onboarding_prompt_context(
            state=onboarding_state,
            user_name=_extract_user_name_from_context(agent_context.blocks),
            ai_name=_extract_ai_name_from_context(agent_context.blocks),
            persona=(onboarding_pre_written or {}).get("persona"),
            user_name_ask_count=_count_user_name_asks(session.get("turn_count", 0), onboarding_state),
            persona_ask_count=0 if onboarding_state != ONBOARDING_STEP3_SENT else 1,
            needs_confirmation=bool((onboarding_pre_extracted or {}).get("needs_confirmation")),
            onboarding_script_override=(campaign_attribution or {}).get("onboarding_script_variant"),
            has_forced_soul_preset=bool((campaign_attribution or {}).get("soul_preset_key")),
            has_forced_ai_name=bool((campaign_attribution or {}).get("ai_name_preset")),
        )

    from app.skills import list_skill_catalog

    builder = PromptBuilder()
    _tool_surface_enabled = getattr(settings, "llm_tool_surface_prompt_enabled", True)
    _skills_enabled = getattr(settings, "llm_skills_prompt_enabled", True)
    skill_catalog = list_skill_catalog() if _skills_enabled and not onboarding_active else None
    build_result = builder.assemble(
        display_name=account.get("display_name"),
        soul=soul,
        user_prefs=user_prefs,
        long_term_memory=long_term_memory,
        daily_notes=None,
        rolling_summary=rolling_summary,
        system_prompt_override=profile.get("system_prompt"),
        style=profile.get("style"),
        agent_context=agent_context.blocks,
        onboarding_context=onboarding_ctx,
        agent_self_state=agent_self_state_text,
        today=today,
        current_time=current_time,
        weekday=beijing_weekday_str(_now),
        daypart=beijing_daypart_str(_now),
        model_name=selected_llm_provider.model,
        tools=tooling["available_tool_names"] if _tool_surface_enabled else None,
        skills=skill_catalog or None,
        tool_instructions=(
            None if onboarding_active or not include_tool_instructions else _tool_instructions(
                active_content_invitation=active_content_invitation,
            )
        ),
        extra_blocks=extra_blocks or None,
        reply_presentation=cap.reply_presentation,
    )
    system_prompt = build_result.prompt
    # 由 builder 自产元数据，替代历史写死的僵尸字段（今后若 wire daily notes 自动正确）。
    metadata["daily_notes_loaded"] = build_result.included("daily_notes")
    metadata["daily_notes_chars"] = build_result.final_chars("daily_notes")
    prompt_blocks = build_result.as_dict()
    metadata["prompt_blocks"] = prompt_blocks
    metadata["block_metrics"] = prompt_blocks
    metadata["tooling"] = {
        key: value
        for key, value in tooling.items()
        if key != "tools"
    }
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    if debug_dry_run and text.strip():
        messages.append({"role": "user", "content": text.strip()})
        metadata["dry_run_user_text_included"] = True
    if getattr(settings, "llm_current_message_envelope_enabled", False):
        _wrap_current_message_envelope(messages, message_type=message_type)
    metadata["messages_count"] = len(messages)
    metadata["include_tool_instructions"] = bool(include_tool_instructions and not onboarding_active)
    metadata["web_search_enabled"] = web_search_enabled
    metadata["web_search_forced"] = force_web_search_enabled is not None

    return {
        "history": history,
        "system_prompt": system_prompt,
        "messages": messages,
        "metadata": metadata,
        "prompt_blocks": prompt_blocks,
        "history_metadata": history_metadata,
        "carryover": carryover_metadata,
        "tooling": tooling,
        "tools": tooling["tools"],
        "active_content_invitation": active_content_invitation,
        "agent_context": agent_context,
    }

def _turn_message_raw(
    *,
    source: str,
    identity,
    account_id: str,
    binding: dict,
    raw_payload: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> dict:
    metadata = {
        "source": source,
        "channel": identity.channel,
        "channel_binding_id": binding.get("id"),
        "channel_account_id": identity.channel_account_id,
        "openclaw_session_key": identity.session_key,
        "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        "sender_id": identity.sender_id,
        "chat_id": identity.chat_id,
        "identity": identity_response_metadata(identity, account_id),
    }
    if raw_payload is not None:
        metadata["raw_payload"] = raw_payload
    if extra:
        metadata.update(extra)
    return metadata


def _send_tool_final_reply(
    *,
    identity,
    account_id: str,
    openclaw_session_key: str,
    reply: str,
    reply_message_id: str,
) -> Dict[str, Any]:
    """Send a tool-turn final reply out of band, bypassing OpenClaw's sync reply timeout."""
    to_user_id = (identity.chat_id or identity.sender_id or "").strip()
    if not to_user_id:
        raise ValueError("tool final reply target is empty")
    return node_gateway.node_send_text(
        node_id=resolve_node_for_account(account_id) or (settings.default_node_id or None),
        to_user_id=to_user_id,
        text=reply,
        gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
        account_id=identity.channel_account_id,
        session_key=openclaw_session_key,
        idempotency_key=f"tool-final-{account_id}-{reply_message_id}",
        channel=identity.channel,
    )


def _debug_trace_account_ids() -> set[str]:
    raw = getattr(settings, "debug_trace_account_ids", "") or ""
    return {item.strip() for item in raw.split(",") if item.strip()}


def _is_debug_trace_account(account_id: str) -> bool:
    return account_id in _debug_trace_account_ids()


def _get_nested_text(value: dict, *path: str) -> Optional[str]:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if current is None:
        return None
    text = str(current).strip()
    return text or None


def _openclaw_id_diagnostics(payload: OpenClawTurnRequest) -> dict:
    """Return non-sensitive upstream id candidates for turn dedupe debugging."""
    raw = payload.raw if isinstance(payload.raw, dict) else {}
    ctx = raw.get("ctx") if isinstance(raw.get("ctx"), dict) else {}
    event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    ai4all_bridge = raw.get("ai4all_bridge") if isinstance(raw.get("ai4all_bridge"), dict) else {}
    account_candidates = (
        ai4all_bridge.get("account_candidates")
        if isinstance(ai4all_bridge.get("account_candidates"), dict)
        else {}
    )
    return {
        "payload_message_id": payload.message_id,
        "payload_event_id": payload.event_id,
        "ctx_run_id": _get_nested_text(raw, "ctx", "runId"),
        "ctx_session_id": _get_nested_text(raw, "ctx", "sessionId"),
        "ctx_message_id": _get_nested_text(raw, "ctx", "messageId"),
        "ctx_turn_id": _get_nested_text(raw, "ctx", "turnId"),
        "event_id": event.get("id"),
        "event_message_id": event.get("messageId") or event.get("message_id"),
        "event_msg_id": event.get("msgId") or event.get("msg_id"),
        "event_new_msg_id": event.get("newMsgId") or event.get("NewMsgId"),
        "bridge_session_id": account_candidates.get("sessionId"),
        "raw_keys": sorted(raw.keys()),
        "ctx_keys": sorted(ctx.keys()),
        "event_keys": sorted(event.keys()),
    }


@dataclass
class _TurnSetup:
    """阶段A（解析+守卫）的产物，下游 B/C/D 只读使用。"""
    account_id: str
    identity: Any
    account: Dict[str, Any]
    session: Dict[str, Any]
    binding: Dict[str, Any]
    profile: Dict[str, Any]
    sender_id: Optional[str]
    message_id: Optional[str]
    openclaw_session_key: str
    today: str
    business_day: str
    now: datetime
    profile_path: Any
    debug_trace_enabled: bool
    onboarding_state: str
    onboarding_active: bool
    # D-09 下半刀：daily 配额上限（per-account override 或 settings 默认），阶段B 原子预占用。
    effective_daily: int
    llm_provider: LLMProviderConfig
    # 渠道能力（onboarding/active-scope/工具/TDAI/投递 的单一开关来源）。微信 cap 全 True，
    # 下游按 cap 分支后取值与历史逐字节一致；Web 等渠道由此获得保守行为而无需散落的 if channel==。
    cap: ChannelCapability


@dataclass
class _InboundResult:
    """阶段B（入站持久化+筛查）的产物。inserted_id 必为非 None（去重已早返回）。"""
    text: str
    inserted_id: int
    image_described: bool
    image_understanding_failed: bool
    inbound_screen: Any
    inbound_blocked: bool
    # D-09 下半刀：本轮的配额预占 token（阶段B 预占，阶段D 按计费谓词 confirm/rollback）。
    # None = 未预占（不应发生，因阶段B 预占失败会早返回 rate_limited）。
    quota_reservation_id: Optional[str] = None


@dataclass
class _ReplyResult:
    """阶段C（解析回复）的产物。debug_metadata 为跨 C/D 有意累积的可变态。"""
    reply: str
    generation_error: Optional[str]
    normal_reply_generated: bool
    tool_names: List[str]
    onboarding_pre_extracted: Optional[dict]
    system_prompt: Optional[str]
    llm_messages: List[Any]
    debug_metadata: Dict[str, Any]
    llm_provider: Optional[LLMProviderConfig]


def _prepare_turn(
    ctx: "ChannelTurnInput",
    *,
    started_at: float,
) -> Union[_TurnSetup, OpenClawTurnResponse]:
    """阶段A（账号级守卫+初始化）：session/binding 初始化、onboarding welcome、disabled、
    限流、首次去重。身份/账号解析与 unbound 收口已由渠道 adapter 完成（见
    build_channel_input_from_openclaw）。命中守卫直接返回 OpenClawTurnResponse；否则返回 _TurnSetup。"""
    identity = ctx.identity
    account_id = ctx.account_id
    cap = ctx.cap
    openclaw_session_key = identity.session_key
    id_diagnostics = ctx.inbound_diagnostics
    sender_id = identity.sender_id
    message_id = ctx.message_id
    if not message_id:
        logger.warning(
            "openclaw_turn missing_message_id account=%s channel=%s channel_account=%s "
            "session=%s id_diagnostics=%s",
            account_id,
            identity.channel,
            identity.channel_account_id,
            openclaw_session_key,
            id_diagnostics,
        )

    now = beijing_now()
    today = now.date().isoformat()
    business_day = business_day_for(
        now,
        start_hour=int(
            getattr(settings, "conversation_session_business_day_start_hour", 4)
        ),
    )

    session_state = get_or_create_account_active_session_with_dreaming(
        account_id=account_id,
        channel=identity.channel,
        sender_id=sender_id,
        sender_name=ctx.sender_name,
        chat_id=identity.chat_id,
        business_day=business_day,
        active_session_key=cap.active_session_key,
        update_account_channel=identity.channel not in {CHANNEL_APP, CHANNEL_WEB},
        memory_sink=ctx.memory_sink,
    )
    binding = upsert_channel_binding(
        account_id=account_id,
        channel=identity.channel,
        session_key=openclaw_session_key,
        channel_account_id=identity.channel_account_id,
        sender_id=sender_id,
        chat_id=identity.chat_id,
        raw_identity=identity_response_metadata(identity, account_id),
    )
    account = session_state["account"]
    session = session_state["session"]
    profile_path = ensure_user_profile(account_id)
    ensure_agent_context_files(
        account_id, display_name=account.get("display_name"), channel=identity.channel
    )
    ensure_account_state(account_id=account_id)
    debug_trace_enabled = _is_debug_trace_account(account_id)

    onboarding_state = get_account_onboarding_state(account_id=account_id)
    # onboarding 由渠道能力开关（微信 True == 历史 channel=="openclaw-weixin"）。
    onboarding_channel_enabled = cap.onboarding_enabled
    onboarding_active = onboarding_channel_enabled and is_onboarding_active(onboarding_state)

    # When the user's first inbound message arrives and onboarding hasn't started yet,
    # send the welcome proactively and absorb this message (no AI reply). The 5-second
    # timer at binding time always fails because the user peer isn't known until now.
    welcome_to_user_id = identity.chat_id or sender_id
    if onboarding_channel_enabled and onboarding_state == ONBOARDING_PENDING and welcome_to_user_id:
        try:
            # 多机:统一经 node_send_text 按归属节点即时发(本机直调/远程 push)。
            # 发送失败由下方 except 捕获 → fall through 正常处理本条消息(状态仍 pending,下条再触发)。
            node_gateway.node_send_text(
                node_id=resolve_node_for_account(account_id) or (settings.default_node_id or None),
                to_user_id=welcome_to_user_id,
                text=ONBOARDING_WELCOME_TEXT,
                gateway_timeout_ms=settings.openclaw_gateway_call_timeout_ms,
                account_id=identity.channel_account_id,
                # 固定幂等键:与 binding-timer welcome 共用,网关去重防并发重复欢迎。
                idempotency_key=f"onboarding-welcome-{account_id}",
                session_key=openclaw_session_key,
                channel=identity.channel,
            )
            set_account_onboarding_state(account_id=account_id, state=ONBOARDING_STEP1_SENT)
            logger.info(
                "onboarding welcome sent on first inbound message account=%s target=%s sender=%s chat=%s",
                account_id, welcome_to_user_id, sender_id, identity.chat_id,
            )
            # 本轮被吸收、不走下面的 _persist_and_screen_inbound / LLM 回复落库，
            # 这里补写这一问一答，否则 messages 表会漏掉用户的第一条消息和欢迎语。
            try:
                insert_message(
                    account_id=account_id,
                    session_id=session["id"],
                    message_id=message_id,
                    reply_to_message_id=None,
                    direction="inbound",
                    role="user",
                    message_type=ctx.message_type,
                    content=ctx.text or "",
                    raw=_turn_message_raw(
                        source="openclaw_turn",
                        identity=identity,
                        account_id=account_id,
                        binding=binding,
                        raw_payload=ctx.raw,
                        extra={"message_id": message_id, "onboarding_action": "absorbed_first_message"},
                    ),
                )
                insert_message(
                    account_id=account_id,
                    session_id=session["id"],
                    message_id=None,
                    reply_to_message_id=message_id,
                    direction="outbound",
                    role="assistant",
                    message_type="text",
                    content=ONBOARDING_WELCOME_TEXT,
                    raw=_turn_message_raw(
                        source="onboarding_welcome",
                        identity=identity,
                        account_id=account_id,
                        binding=binding,
                        extra={"onboarding_action": "welcome_sent_on_first_message"},
                    ),
                )
            except Exception as log_err:
                logger.error(
                    "onboarding welcome message logging failed account=%s error=%s",
                    account_id, log_err,
                )
            latency_ms = int((time.monotonic() - started_at) * 1000)
            return OpenClawTurnResponse(
                status="ok",
                no_reply=True,
                metadata={
                    **identity_response_metadata(identity, account_id),
                    "latency_ms": latency_ms,
                    "onboarding_action": "welcome_sent_on_first_message",
                    "onboarding_welcome_to_user_id": welcome_to_user_id,
                },
            )
        except Exception as err:
            logger.warning(
                "onboarding welcome failed on first inbound message account=%s target=%s error=%s; "
                "falling through to normal turn",
                account_id, welcome_to_user_id, err,
            )

    if account.get("status") == "disabled":
        logger.info(
            "openclaw_turn ignored disabled account account=%s session=%s",
            account_id,
            openclaw_session_key,
        )
        return OpenClawTurnResponse(
            status="disabled",
            no_reply=True,
            metadata=identity_response_metadata(identity, account_id),
        )

    # 绑定有效且账号 active 后再记录正文，未绑定/已解绑/disabled 均已在上方返回。
    logger.info(
        "openclaw_turn text account=%s session=%s message_id=%s text=%r",
        account_id,
        openclaw_session_key,
        message_id,
        ctx.text,
    )

    quota_limits = resolve_effective_quota_limits(
        account_id=account_id,
        default_daily=settings.rate_limit_daily,
        default_rpm=settings.rate_limit_rpm,
    )
    effective_rpm = int(quota_limits["rpm_limit"])
    effective_daily = int(quota_limits["daily_limit"])

    effective_rpm_window_seconds = max(
        float(getattr(settings, "rate_limit_rpm_window_seconds", 60.0) or 60.0),
        0.001,
    )

    # D-09：RPM 按真人聚合，多居民共享一套滑窗。check_rpm 是与 web IP-keyed 调用共享的通用限流器，
    # 不能内部解析，故在此把 account_id 解析成 platform_user subject（孤儿号回退 account_id）后传入。
    quota_subject = get_platform_user_id_for_account(account_id=account_id) or account_id
    if effective_rpm > 0 and not rate_limiter.check_rpm(
        quota_subject,
        effective_rpm,
        window_seconds=effective_rpm_window_seconds,
    ):
        logger.info(
            "openclaw_turn rpm_limited account=%s limit=%s window_seconds=%s message_id=%s "
            "id_diagnostics=%s",
            account_id,
            effective_rpm,
            effective_rpm_window_seconds,
            message_id,
            id_diagnostics,
        )
        return OpenClawTurnResponse(
            status="rate_limited",
            reply=settings.rate_limit_rpm_message,
            metadata={**identity_response_metadata(identity, account_id), "reason": "rpm"},
        )

    if effective_daily > 0:
        current_count = get_daily_usage(account_id=account_id, date=today)
        if current_count >= effective_daily:
            logger.info(
                "openclaw_turn daily_limited account=%s count=%s", account_id, current_count
            )
            return OpenClawTurnResponse(
                status="rate_limited",
                reply=settings.rate_limit_daily_message,
                metadata={
                    **identity_response_metadata(identity, account_id),
                    "reason": "daily",
                    "count": current_count,
                },
            )

    duplicate_reply = get_duplicate_reply(
        account_id=account_id,
        reply_to_message_id=message_id,
    )
    if duplicate_reply:
        latency_ms = int((time.monotonic() - started_at) * 1000)
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply,
            metadata={**identity_response_metadata(identity, account_id), "latency_ms": latency_ms},
        )

    return _TurnSetup(
        account_id=account_id,
        identity=identity,
        account=account,
        session=session,
        binding=binding,
        profile=session_state.get("profile") or {},
        sender_id=sender_id,
        message_id=message_id,
        openclaw_session_key=openclaw_session_key,
        today=today,
        business_day=business_day,
        now=now,
        profile_path=profile_path,
        debug_trace_enabled=debug_trace_enabled,
        onboarding_state=onboarding_state,
        onboarding_active=onboarding_active,
        effective_daily=effective_daily,
        llm_provider=resolve_active_llm_provider(tier_for_task(TASK_MAIN_REPLY)),
        cap=cap,
    )


def _persist_and_screen_inbound(
    ctx: "ChannelTurnInput",
    setup: _TurnSetup,
    *,
    started_at: float,
    timings: Dict[str, int],
) -> Union[_InboundResult, OpenClawTurnResponse]:
    """阶段B：text 规范化+图片理解、插入入站消息(+插入后去重早返回)、入站审核 screen、
    referral、图片计费。返回 _InboundResult；插入后去重命中则返回 OpenClawTurnResponse。"""
    account_id = setup.account_id
    identity = setup.identity
    binding = setup.binding
    session = setup.session
    message_id = setup.message_id
    today = setup.today

    text = (ctx.text or "").strip()
    # 图片轮：调 VL 产出多维描述，合成进 user 历史（支撑图后追问 C 场景），
    # 再走主链路按人设接话；VL 失败/总开关关闭则走兜底，跳过主模型（红线：不瞎猜）。
    image_described = False
    image_understanding_failed = False
    if ctx.message_type == "image":
        image_started = time.monotonic()
        caption = text
        description = None
        if settings.image_understanding_enabled:
            media = ctx.media
            if media is not None and (media.data_base64 or media.path or media.url):
                # 来源优先级（多机字节 > 单机本地路径 > 远程 URL）在 describe_image 内部统一。
                description = describe_image(
                    image_b64=media.data_base64,
                    image_format=media.format,
                    image_path=media.path,
                    image_url=media.url,
                    caption=caption,
                )
        if description:
            text = f"{caption}\n[用户发来一张图片：{description}]".strip()
            image_described = True
        else:
            image_understanding_failed = True
            text = caption or "[图片]"
        _record_timing(timings, "image_understanding_ms", image_started)
    elif not text and ctx.message_type == "voice":
        text = "[voice message]"

    inbound_db_started = time.monotonic()
    quota_reservation_id: Optional[str] = None
    with db_connect() as conn:
        inserted_id = insert_message(
            account_id=account_id,
            session_id=session["id"],
            message_id=message_id,
            reply_to_message_id=None,
            direction="inbound",
            role="user",
            message_type=ctx.message_type,
            content=text,
            raw=_turn_message_raw(
                source="openclaw_turn",
                identity=identity,
                account_id=account_id,
                binding=binding,
                raw_payload=ctx.raw,
                extra={
                    "message_id": message_id,
                    "message_type": ctx.message_type,
                },
            ),
            conn=conn,
        )
        if inserted_id is not None:
            # D-09 下半刀：去重(insert_message)先于预占、同一 conn（重试不吃配额）。原子预占取代
            # 旧的「入站即 +1」——仅真正成功计费的 turn 在阶段D confirm 计入 message_count，
            # moderation 拦截/模型失败/特殊命令一律 rollback（退款矩阵）。崩溃悬挂由 TTL 回收。
            quota_reservation_id = reserve_daily_quota(
                account_id=account_id,
                date=today,
                limit=setup.effective_daily,
                conn=conn,
            )
    _record_timing(timings, "inbound_db_ms", inbound_db_started)
    if inserted_id is None:
        duplicate_reply = get_duplicate_reply(
            account_id=account_id,
            reply_to_message_id=message_id,
        )
        latency_ms = _elapsed_ms(started_at)
        return OpenClawTurnResponse(
            status="duplicate",
            reply=duplicate_reply or "刚刚这条消息我已经收到啦。",
            metadata={**identity_response_metadata(identity, account_id), "latency_ms": latency_ms},
        )
    if quota_reservation_id is None:
        # 在途竞争到满：阶段A 读检已放行，但并发预占抢占了最后名额 → 拒本轮（本条已入库）。
        # 常见「已满」在阶段A 无插入即拒；此分支仅覆盖罕见并发 race。
        logger.info(
            "openclaw_turn daily_limited(reserve) account=%s message_id=%s", account_id, message_id
        )
        latency_ms = _elapsed_ms(started_at)
        return OpenClawTurnResponse(
            status="rate_limited",
            reply=settings.rate_limit_daily_message,
            metadata={
                **identity_response_metadata(identity, account_id),
                "reason": "daily",
                "latency_ms": latency_ms,
            },
        )

    # 入站内容同步筛查（阿里云云审核为主 + 本地红线补充）。命中即停止本轮回复并进入人工队列。
    # 阿里云未开启时内部回退第一阶段异步审核并放行；筛查自身异常时 fail-open（放行本轮回复）。
    inbound_screen = None
    inbound_moderation_started = time.monotonic()
    try:
        inbound_content_kind = "voice_transcript" if ctx.message_type == "voice" else ctx.message_type
        inbound_screen = screen_inbound_message_sync(
            message_db_id=int(inserted_id),
            account_id=account_id,
            session_id=int(session["id"]),
            content_kind=inbound_content_kind,
            text=text,
            media=ctx.media,
            source_message_id=message_id,
            metadata={
                "message_type": ctx.message_type,
                "image_described": image_described,
                "image_understanding_failed": image_understanding_failed,
            },
        )
    except Exception as err:
        logger.exception(
            "inbound moderation screen failed account=%s message_db_id=%s error=%s",
            account_id,
            inserted_id,
            err,
        )
        inbound_screen = None
    finally:
        _record_timing(timings, "inbound_moderation_ms", inbound_moderation_started)
    inbound_blocked = bool(inbound_screen is not None and not inbound_screen.allowed)
    if inbound_blocked:
        # 命中风险的入站原文打审核标记：从后续所有 LLM 上下文/记忆/turn 计数中剔除，避免下一轮被重新喂给模型。
        try:
            mark_message_moderation_blocked(
                message_db_id=int(inserted_id),
                account_id=account_id,
            )
        except Exception as err:
            logger.exception(
                "mark inbound moderation blocked failed account=%s message_db_id=%s error=%s",
                account_id,
                inserted_id,
                err,
            )

    referral_started = time.monotonic()
    try:
        process_referral_message_for_account(
            account_id=account_id,
            message_db_id=int(inserted_id),
        )
    except Exception as err:
        logger.exception(
            "referral message processing failed account=%s message_db_id=%s error=%s",
            account_id,
            inserted_id,
            err,
        )
    finally:
        _record_timing(timings, "referral_ms", referral_started)

    # VL 成功后记一次独立的图片理解成本事件（固定贝壳，带总开关，与 chat 扣费相互独立）。
    if image_described:
        image_charge_started = time.monotonic()
        try:
            record_image_understanding_charge(
                account_id=account_id,
                source_id=message_id or str(inserted_id),
                idempotency_key=f"image-understanding-{account_id}-{message_id or inserted_id}",
                model=settings.image_understanding_model,
                metadata={
                    "message_id": message_id,
                    "session_id": int(session["id"]),
                },
            )
        except Exception as err:
            logger.exception(
                "image understanding charge failed account=%s message_id=%s error=%s",
                account_id,
                message_id,
                err,
            )
        finally:
            _record_timing(timings, "image_charge_ms", image_charge_started)

    return _InboundResult(
        text=text,
        inserted_id=int(inserted_id),
        image_described=image_described,
        image_understanding_failed=image_understanding_failed,
        inbound_screen=inbound_screen,
        inbound_blocked=inbound_blocked,
        quota_reservation_id=quota_reservation_id,
    )


def _resolve_turn_reply(
    ctx: "ChannelTurnInput",
    setup: _TurnSetup,
    inbound: _InboundResult,
    *,
    background_loop: Optional[asyncio.AbstractEventLoop],
    force_web_search_enabled: Optional[bool],
    timings: Dict[str, int],
) -> _ReplyResult:
    """阶段C：决定本轮回复来源（inbound_blocked / #重置 / #状态 / 图片失败 / 正常聊天）。
    正常分支内做 onboarding 预抽取、build_turn_llm_input、构建 TurnContext 并调 LLM。"""
    account_id = setup.account_id
    account = setup.account
    session = setup.session
    binding = setup.binding
    identity = setup.identity
    sender_id = setup.sender_id
    message_id = setup.message_id
    openclaw_session_key = setup.openclaw_session_key
    today = setup.today
    business_day = setup.business_day
    now = setup.now
    profile = setup.profile
    profile_path = setup.profile_path
    debug_trace_enabled = setup.debug_trace_enabled
    onboarding_state = setup.onboarding_state
    onboarding_active = setup.onboarding_active
    llm_provider = setup.llm_provider
    text = inbound.text
    inbound_blocked = inbound.inbound_blocked
    image_understanding_failed = inbound.image_understanding_failed

    generation_error = None
    normal_reply_generated = False
    tool_names_used: List[str] = []
    onboarding_pre_extracted = None
    system_prompt = None
    llm_messages = []
    web_search_enabled_for_turn = (
        bool(force_web_search_enabled)
        if force_web_search_enabled is not None
        else bool(getattr(settings, "web_search_enabled", False))
    )
    debug_metadata = {
        "trace_kind": "ai4all_turn",
        "debug_trace_enabled": debug_trace_enabled,
        "web_search_enabled": web_search_enabled_for_turn,
        "web_search_forced": force_web_search_enabled is not None,
        "identity": identity_response_metadata(identity, account_id),
        "channel_binding_id": binding["id"],
        "channel": identity.channel,
        "session_key": openclaw_session_key,
        "openclaw_session_key": openclaw_session_key,
        "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        "sender_id": sender_id,
        "message_type": ctx.message_type,
        "today": today,
        "business_day": business_day,
        "session_business_day": session.get("business_day"),
        "session_turn_count": session.get("turn_count"),
        "session_carryover_chars": len(session.get("carryover_summary") or ""),
    }
    if inbound_blocked:
        # 入站命中风险：不调用主模型，返回固定安全话术；本轮不计费、不推进 onboarding。
        reply = str(
            getattr(settings, "moderation_inbound_blocked_reply_text", "")
            or "这个话题我不太方便继续，我们换个轻松点的聊聊吧～"
        )
    elif text == "#重置会话":
        clear_session_messages(session_id=session["id"])
        reply = "已重置当前会话。"
    elif text == "#状态":
        reply = (
            f"当前会话正常。account_id={account_id}, "
            f"active_session_key={ACCOUNT_ACTIVE_SESSION_KEY}, "
            f"openclaw_session_key={openclaw_session_key}"
        )
    elif image_understanding_failed:
        # 图片没看清/未开启理解：走兜底话术，不调主模型（禁止无描述瞎猜）。
        reply = settings.image_understanding_fallback_text
    else:
        # TDAI recall：注入 query-time L1 记忆（prepend_context）和 L3 persona（context）。
        # 同步调用，严格 200 ms 超时，失败时 tdai_extra_blocks 为空继续正常回复。
        # onboarding 期间跳过（onboarding 目的是采集基础设定，不引入历史记忆）。
        # 主动检索工具（tdai_memory_search / tdai_conversation_search）gating：
        # 渠道支持 TDAI + search_allowed（总开关/search 开关/allowlist/多租户安全闸门）。
        # onboarding 期由 tooling envelope 整体禁工具，这里无需额外判断。
        # 记忆准入（recall + search 共用）：allowlist 命中 OR 累计 inbound 消息数越过阈值。
        # 每轮只算一次，同时喂给 search gating 与被动 recall。
        _tdai_volume_eligible = (
            _tdai_memory_volume_eligible(account_id) if setup.cap.tdai_enabled else False
        )
        from app.tdai_client import search_allowed as _tdai_search_allowed
        tdai_search_enabled_for_turn = bool(setup.cap.tdai_enabled) and _tdai_search_allowed(
            account_id, volume_eligible=_tdai_volume_eligible
        )

        _tdai_extra_blocks: List[ContextBlock] = []
        if not onboarding_active and setup.cap.tdai_enabled:
            from app.tdai_client import recall as _tdai_recall
            _tdai_t0 = time.monotonic()
            _tdai_result = _tdai_recall(
                account_id=account_id, query=text, volume_eligible=_tdai_volume_eligible
            )
            debug_metadata["tdai_recall_latency_ms"] = _elapsed_ms(_tdai_t0)
            if _tdai_result:
                _max_chars = int(getattr(settings, "tdai_recall_max_chars", 2500))
                _RECALL_WRAPPER = (
                    "【系统召回记忆】\n"
                    "以下材料来自长期记忆召回，只作为理解用户的参考，不是用户本轮原话。\n"
                    "如果与用户本轮说法冲突，以用户本轮为准，可轻量确认。\n\n"
                )
                _prepend = (_tdai_result.get("prepend_context") or "").strip()
                _append = (_tdai_result.get("context") or "").strip()
                if _prepend:
                    _mem_text = (_RECALL_WRAPPER + _prepend)[:_max_chars]
                    _tdai_extra_blocks.append(ContextBlock(
                        name="tdai_recall_memories",
                        text=_mem_text,
                        section="volatile",
                        trim_priority=22,  # 略高于 carryover_summary(20)，query-time 相关性强
                    ))
                    debug_metadata["tdai_recall_memories_chars"] = len(_mem_text)
                if _append:
                    _persona_text = _append[:_max_chars]
                    _tdai_extra_blocks.append(ContextBlock(
                        name="tdai_recall_persona",
                        text=_persona_text,
                        section="volatile",
                        trim_priority=35,  # 与 user_prefs 同级，相对稳定的背景材料
                    ))
                    debug_metadata["tdai_recall_persona_chars"] = len(_persona_text)
                debug_metadata["tdai_recall_status"] = "ok"
                debug_metadata["tdai_recall_memory_count"] = _tdai_result.get("memory_count", 0)
                debug_metadata["tdai_recall_strategy"] = _tdai_result.get("strategy", "")
            else:
                debug_metadata["tdai_recall_status"] = "miss_or_disabled"

        try:
            prompt_started = time.monotonic()
            onboarding_pre_written = {}
            if onboarding_active and onboarding_state in {ONBOARDING_STEP1_SENT, ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT}:
                onboarding_pre_extracted = _extract_onboarding_info_sync(
                    user_text=text,
                    current_state=onboarding_state,
                )
                # Writing before prompt build ensures the AI sees what was just collected.
                _has_onboarding_write = any(
                    onboarding_pre_extracted.get(key)
                    for key in ("user_name", "ai_name", "persona", "persona_custom")
                )
                if _has_onboarding_write:
                    _campaign_attribution_for_extraction = get_campaign_attribution(account_id=account_id)
                    onboarding_pre_written = apply_extracted_onboarding_info(
                        account_id=account_id,
                        extracted=onboarding_pre_extracted,
                        current_state=onboarding_state,
                        has_forced_soul_preset=bool(
                            (_campaign_attribution_for_extraction or {}).get("soul_preset_key")
                        ),
                        has_forced_ai_name=bool(
                            (_campaign_attribution_for_extraction or {}).get("ai_name_preset")
                        ),
                    )
            llm_input = build_turn_llm_input(
                account_id=account_id,
                account=account,
                session=session,
                profile=profile,
                text=text,
                today=today,
                current_time=now.strftime("%H:%M"),
                onboarding_state=onboarding_state,
                onboarding_active=onboarding_active,
                onboarding_pre_written=onboarding_pre_written,
                onboarding_pre_extracted=onboarding_pre_extracted,
                web_search_enabled=web_search_enabled_for_turn,
                tdai_search_enabled=tdai_search_enabled_for_turn,
                force_web_search_enabled=force_web_search_enabled,
                now=now,
                llm_provider=llm_provider,
                message_type=ctx.message_type,
                # 接缝①：域层注入块（ctx.extra_blocks，L3 等）在前 + tdai 召回块在后。
                # 两者皆空 → None → assemble 既有 no-op 分支（form-A 逐字不变）。
                extra_blocks=([*ctx.extra_blocks, *_tdai_extra_blocks] or None),
                cap=setup.cap,
                channel=setup.identity.channel,
            )
            history = llm_input["history"]
            system_prompt = llm_input["system_prompt"]
            llm_messages = llm_input["messages"]
            tooling = llm_input["tooling"]
            debug_metadata.update(llm_input["metadata"])

            ctx = TurnContext(
                account_id=account_id,
                account=account,
                session=session,
                identity=identity,
                binding=binding,
                message_id=message_id,
                text=text,
                today=today,
                business_day=business_day,
                profile_path=profile_path,
                debug_trace_enabled=debug_trace_enabled,
                onboarding_state=onboarding_state,
                onboarding_active=onboarding_active,
                recent_messages=history,
                background_loop=background_loop,
                web_search_enabled=web_search_enabled_for_turn,
                tdai_search_enabled=tdai_search_enabled_for_turn,
            )
            _record_timing(timings, "prompt_build_ms", prompt_started)

            generation_started = time.monotonic()
            _round_traces: Optional[List[Dict]] = None
            if onboarding_active:
                try:
                    reply = generate_reply(
                        user_text=text,
                        history=history,
                        system_prompt=system_prompt,
                        messages=llm_messages,
                        provider=llm_provider,
                    )
                finally:
                    _record_timing(timings, "reply_generation_ms", generation_started)
                if onboarding_state == ONBOARDING_PENDING:
                    reply = _ensure_pending_onboarding_question(reply)
            else:
                def _on_tool_detected(tool_names: List[str]) -> None:
                    # 记录本轮实际触发的工具名（进 debug_metadata + 回传 tool_names）。
                    cleaned = [
                        str(name).strip()
                        for name in (tool_names or [])
                        if str(name or "").strip()
                    ]
                    if cleaned:
                        tool_names_used.extend(cleaned)

                _round_traces = [] if debug_trace_enabled else None
                try:
                    reply, generation_error = generate_reply_with_tools(
                        user_text=text,
                        history=history,
                        system_prompt=system_prompt,
                        tools=llm_input["tools"],
                        ctx=ctx,
                        first_round_tool_choice=tooling["first_round_tool_choice"],
                        messages=llm_messages,
                        provider=llm_provider,
                        on_tool_detected=_on_tool_detected,
                        round_trace_collector=_round_traces,
                    )
                finally:
                    _record_timing(timings, "reply_generation_ms", generation_started)
            if _round_traces:
                debug_metadata["rounds"] = _round_traces
                debug_metadata["round_count"] = len(_round_traces)
            if generation_error and not reply:
                reply = _GENERATION_ERROR_REPLY
                # 用户将真实收到「卡住了」兜底回复(生成失败且无可用回复)。ERROR 级 → 经 ai4all
                # 命名空间的 Feishu handler 推送 FEISHU_ALERT_WEBHOOK_URL(同签名 5min 冷却+脱敏)。
                # 这里补的是 generate_reply_with_tools 返回 error 字符串的静默路径(超时/空响应/
                # 限流/工具轮超限等);LLM 直接抛异常的路径已由下方 except 的 logger.exception 覆盖。
                logger.error(
                    "user received generation fallback reply account=%s generation_error=%s",
                    account_id,
                    generation_error,
                )
            normal_reply_generated = generation_error is None
            if tool_names_used:
                debug_metadata["tool_names_used"] = tool_names_used
        except Exception as err:
            logger.exception("reply generation failed: %s", err)
            generation_error = str(err)
            reply = _GENERATION_ERROR_REPLY

    return _ReplyResult(
        reply=reply,
        generation_error=generation_error,
        normal_reply_generated=normal_reply_generated,
        tool_names=tool_names_used,
        onboarding_pre_extracted=onboarding_pre_extracted,
        system_prompt=system_prompt,
        llm_messages=llm_messages,
        debug_metadata=debug_metadata,
        llm_provider=llm_provider,
    )


@dataclass
class _AfterTurnContext:
    """after-turn 后台工作所需的最小上下文快照（由 _finalize_turn 组装）。

    hook 只读本对象、不回写主链路状态；新增 after-turn 工作只需在 _AFTER_TURN_HOOKS 注册一个
    hook，无需改动 _finalize_turn 主体。
    """
    account_id: str
    text: str
    reply: str
    business_day: str
    session: Dict[str, Any]
    message_id: Optional[str]
    reply_message_id: str
    message_type: str
    identity: ResolvedIdentity
    binding: Dict[str, Any]
    openclaw_session_key: str
    normal_reply_generated: bool
    cap: ChannelCapability
    moderation_blocked: bool
    onboarding_active: bool
    image_understanding_failed: bool


def _after_turn_write_memory(atx: "_AfterTurnContext"):
    """turn 后记忆写入（当日记忆压缩上游）。should_run_after_turn 已在上游门控，无额外条件。"""
    return write_memory(
        account_id=atx.account_id,
        turns=[
            {"role": "user", "content": atx.text},
            {"role": "assistant", "content": atx.reply},
        ],
        today=atx.business_day,
        session_id=int(atx.session["id"]),
        user_message_id=atx.message_id,
        assistant_message_id=atx.reply_message_id,
        modality=atx.message_type,
        extra_metadata={
            "channel": atx.identity.channel,
            "channel_binding_id": atx.binding["id"],
            "openclaw_session_key": atx.openclaw_session_key,
            "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        },
    )


def _after_turn_relationship_state(atx: "_AfterTurnContext"):
    """turn 后确定性关系状态更新（30 条阈值 + 资源风险）；失败在函数内部记日志。"""
    return asyncio.to_thread(
        maybe_update_relationship_state_after_turn,
        account_id=atx.account_id,
    )


def _after_turn_rolling_summary(atx: "_AfterTurnContext"):
    """P3 Token 压力滚动摘要（默认关）：仅在开关开时挂任务，避免无谓调度。"""
    if not getattr(settings, "llm_rolling_summary_enabled", False):
        return None
    from app.context_summarizer import maybe_update_rolling_summary
    return asyncio.to_thread(
        maybe_update_rolling_summary,
        account_id=atx.account_id,
        session_id=int(atx.session["id"]),
    )


def _after_turn_tdai_capture(atx: "_AfterTurnContext"):
    """TDAI capture：把本轮 user/assistant 可见文本喂给 TDAI L0→L1→L2→L3 pipeline。跳过条件：
    非正常回复、TDAI 关闭、渠道 cap 关闭、出站红线拦截、onboarding 期间、图片理解失败兜底。
    先判开关再构造协程，避免关闭时创建未被 await 的 coroutine。"""
    if not atx.normal_reply_generated:
        return None
    if not (
        getattr(settings, "tdai_enabled", False)
        and getattr(settings, "tdai_capture_enabled", True)
        and atx.cap.tdai_enabled
        and not atx.moderation_blocked
        and not atx.onboarding_active
        and not atx.image_understanding_failed
    ):
        return None
    from app.tdai_client import capture_turn as _tdai_capture
    return _tdai_capture(
        account_id=atx.account_id,
        session_id=int(atx.session["id"]),
        user_content=atx.text,
        # reply 在未触发出站红线时 == result.reply（原始 LLM 回复）
        assistant_content=atx.reply,
    )


# after-turn 后台工作注册表：每个 hook 读 _AfterTurnContext、返回待调度协程或 None（跳过）。
# 列表顺序即调度顺序，与历史逐条 call_soon_threadsafe 顺序一致；新增 after-turn 工作在此注册即可。
_AFTER_TURN_HOOKS = [
    ("write_memory", _after_turn_write_memory),
    ("relationship_state", _after_turn_relationship_state),
    ("rolling_summary", _after_turn_rolling_summary),
    ("tdai_capture", _after_turn_tdai_capture),
]


def _dispatch_after_turn(
    atx: "_AfterTurnContext",
    *,
    background_loop: Optional[asyncio.AbstractEventLoop],
    timings: Dict[str, int],
    started_at: float,
) -> None:
    """统一 after-turn 后台派发：按 _AFTER_TURN_HOOKS 逐个构造协程并经 _schedule_on_loop 挂到
    后台事件循环。``background_loop`` 为 None（独立进程/脚本/显式 None）时整体跳过并记 warning。
    单个 hook 构造异常被隔离（记日志后继续），不影响其余 after-turn 工作。"""
    if background_loop is None:
        # 无后台事件循环：after-turn 记忆写入等会被跳过，记 warning 让数据丢失可观测。
        logger.warning(
            "after-turn work skipped: no background loop (daily memory not run) account=%s",
            atx.account_id,
        )
    else:
        for name, hook in _AFTER_TURN_HOOKS:
            try:
                coro = hook(atx)
            except Exception as err:
                logger.exception(
                    "after-turn hook %s build failed account=%s error=%s",
                    name,
                    atx.account_id,
                    err,
                )
                continue
            if coro is not None:
                _schedule_on_loop(background_loop, coro)
    _record_timing(timings, "after_turn_enqueue_ms", started_at)


def _finalize_turn(
    ctx: "ChannelTurnInput",
    setup: _TurnSetup,
    inbound: _InboundResult,
    result: _ReplyResult,
    *,
    latency_ms: int,
    background_loop: Optional[asyncio.AbstractEventLoop],
    timings: Dict[str, int],
) -> OpenClawTurnResponse:
    """阶段D：出站同步审核守卫（就地兜底覆写 reply）、debug trace、出站持久化+turn_count、
    出站审核入队、计费、onboarding 状态推进、after-turn 派发、构建响应。"""
    account_id = setup.account_id
    identity = setup.identity
    session = setup.session
    binding = setup.binding
    message_id = setup.message_id
    openclaw_session_key = setup.openclaw_session_key
    business_day = setup.business_day
    profile_path = setup.profile_path
    debug_trace_enabled = setup.debug_trace_enabled
    onboarding_active = setup.onboarding_active
    onboarding_state = setup.onboarding_state
    inserted_id = inbound.inserted_id
    inbound_blocked = inbound.inbound_blocked
    inbound_screen = inbound.inbound_screen
    text = inbound.text
    reply = result.reply
    generation_error = result.generation_error
    normal_reply_generated = result.normal_reply_generated
    tool_names = result.tool_names
    onboarding_pre_extracted = result.onboarding_pre_extracted
    system_prompt = result.system_prompt
    llm_messages = result.llm_messages
    debug_metadata = result.debug_metadata
    active_llm_provider = (
        result.llm_provider.redacted()
        if result.llm_provider is not None
        else resolve_active_llm_provider(tier_for_task(TASK_MAIN_REPLY)).redacted()
    )
    active_llm_model = str(active_llm_provider.get("model") or "")
    debug_metadata.setdefault("llm_provider_id", active_llm_provider.get("id"))
    debug_metadata.setdefault("llm_protocol", active_llm_provider.get("protocol"))

    outbound_guard_started = time.monotonic()
    reply_message_id = f"reply-{uuid.uuid4()}"
    moderation_reply_metadata: Dict[str, Any] = {}
    sync_decision = check_sync_guard(
        account_id=account_id,
        text=reply,
        direction="outbound",
        content_kind="text",
        source_type="generated_reply",
        source_id=reply_message_id,
    )
    if not sync_decision.allowed:
        original_reply = reply
        try:
            blocked_task = create_sync_block_task(
                account_id=account_id,
                session_id=int(session["id"]),
                source_type="generated_reply",
                source_id=reply_message_id,
                direction="outbound",
                content_kind="text",
                text=original_reply,
                decision=sync_decision,
                metadata={
                    "reply_to_message_id": message_id,
                    "message_db_id": inserted_id,
                },
            )
        except Exception as err:
            logger.exception(
                "sync moderation block task failed account=%s reply_message_id=%s error=%s",
                account_id,
                reply_message_id,
                err,
            )
            blocked_task = None
        reply = str(getattr(settings, "moderation_safe_fallback_text", "") or "这条内容我不能继续发送，我们换个安全的话题吧。")
        moderation_reply_metadata = {
            "moderation_blocked": True,
            "moderation_task_id": blocked_task.get("id") if blocked_task else None,
            "moderation_risk_level": sync_decision.level,
            "moderation_categories": sync_decision.categories,
        }
        debug_metadata.update(moderation_reply_metadata)

    # 入站被拦截：在出站消息上标注入站审核信息，便于排查与审计（任务已由同步筛查创建）。
    if inbound_blocked and inbound_screen is not None:
        moderation_reply_metadata.update(
            {
                "moderation_inbound_blocked": True,
                "moderation_task_id": inbound_screen.task_id,
                "moderation_risk_level": inbound_screen.level,
                "moderation_categories": inbound_screen.categories,
                "moderation_degraded": inbound_screen.degraded,
            }
        )
        debug_metadata.update(moderation_reply_metadata)
    _record_timing(timings, "outbound_sync_guard_ms", outbound_guard_started)

    trace_id = None
    outbound_inserted_id = None
    outbound_db_started = time.monotonic()
    with db_connect() as conn:
        if debug_trace_enabled:
            trace_id = f"trace-{uuid.uuid4()}"
            insert_debug_trace(
                trace_id=trace_id,
                account_id=account_id,
                session_id=session["id"],
                message_id=message_id,
                source="ai4all",
                llm_model=active_llm_model,
                system_prompt=system_prompt,
                messages=llm_messages,
                reply=reply,
                metadata=debug_metadata,
                latency_ms=latency_ms,
                error=generation_error,
                conn=conn,
            )

        outbound_raw_extra = {
            "message_id": reply_message_id,
            "reply_to_message_id": message_id,
        }
        if tool_names:
            outbound_raw_extra["tool_names_used"] = tool_names
            outbound_raw_extra["delivery_mode"] = "out_of_band_tool_final"
        outbound_raw_extra.update(moderation_reply_metadata)
        outbound_inserted_id = insert_message(
            account_id=account_id,
            session_id=session["id"],
            message_id=reply_message_id,
            reply_to_message_id=message_id,
            direction="outbound",
            role="assistant",
            message_type="text",
            content=reply,
            raw=_turn_message_raw(
                source="ai4all_sync_reply",
                identity=identity,
                account_id=account_id,
                binding=binding,
                extra=outbound_raw_extra,
            ),
            latency_ms=latency_ms,
            error=generation_error,
            conn=conn,
        )

        if not generation_error and not inbound_blocked and text and text not in _SPECIAL_COMMANDS:
            increment_session_turn_count(session_id=int(session["id"]), conn=conn)
    _record_timing(timings, "outbound_db_ms", outbound_db_started)

    if debug_trace_enabled:
        logger.info(
            "debug trace recorded trace_id=%s account=%s session=%s message_id=%s messages=%s prompt_chars=%s",
            trace_id,
            account_id,
            openclaw_session_key,
            message_id,
            len(llm_messages),
            len(system_prompt or ""),
        )

    # 已被同步红线拦截的回复，原文已由 create_sync_block_task 记录成审核任务；
    # 此时 reply 只是安全兜底文案，无需再为它创建一条 machine_passed 任务，避免队列里同一条回复出现两个 task。
    if (
        outbound_inserted_id is not None
        and not moderation_reply_metadata.get("moderation_blocked")
        and not inbound_blocked
    ):
        outbound_moderation_started = time.monotonic()
        try:
            enqueue_message_for_moderation(
                message_db_id=int(outbound_inserted_id),
                account_id=account_id,
                session_id=int(session["id"]),
                direction="outbound",
                content_kind="text",
                text=reply,
                media=None,
                source_message_id=reply_message_id,
                metadata={
                    "reply_to_message_id": message_id,
                },
            )
        except Exception as err:
            logger.exception(
                "outbound moderation enqueue failed account=%s message_db_id=%s error=%s",
                account_id,
                outbound_inserted_id,
                err,
            )
        finally:
            _record_timing(timings, "outbound_moderation_enqueue_ms", outbound_moderation_started)

    billing_result = None
    # D-09 下半刀：配额 confirm/rollback 与钱包计费共用同一谓词——「配额消耗 ⟺ 钱包计费」。
    should_charge = bool(
        not generation_error and normal_reply_generated and text and text not in _SPECIAL_COMMANDS
    )
    if should_charge:
        billing_started = time.monotonic()
        try:
            billing_result = record_chat_usage_charge(
                account_id=account_id,
                model=active_llm_model,
                messages=llm_messages,
                reply=reply,
                source_type="chat_turn",
                source_id=reply_message_id,
                idempotency_key=f"chat-turn-{account_id}-{message_id or inserted_id}",
                metadata={
                    "message_id": message_id,
                    "reply_message_id": reply_message_id,
                    "session_id": int(session["id"]),
                    "estimated": True,
                },
            )
        except Exception as err:
            logger.exception("chat usage charge failed account=%s reply=%s error=%s", account_id, reply_message_id, err)
        finally:
            _record_timing(timings, "billing_ms", billing_started)

    # D-09 下半刀：配额结算（退款矩阵）。成功计费 → confirm（计入 daily_usage.message_count）；
    # 否则 rollback（moderation 拦截/模型失败/特殊命令/未计费 onboarding 一律不扣）。reservation 为
    # None（未预占）或行已被 TTL 回收时 confirm/rollback 皆 no-op。本段异常已吞（不影响回复投递），
    # 崩溃/异常跳过本段的悬挂预占交 reservation TTL 兜底。
    try:
        if should_charge:
            confirm_daily_quota(reservation_id=inbound.quota_reservation_id)
        else:
            rollback_daily_quota(reservation_id=inbound.quota_reservation_id)
    except Exception as err:
        logger.exception(
            "daily quota settle failed account=%s reservation=%s should_charge=%s error=%s",
            account_id,
            inbound.quota_reservation_id,
            should_charge,
            err,
        )

    # Advance onboarding state synchronously after reply so onboarding completion
    # does not depend on the after-turn background loop.
    if not generation_error and normal_reply_generated and onboarding_active:
        onboarding_advance_started = time.monotonic()
        if onboarding_state == "pending":
            try:
                set_account_onboarding_state(account_id=account_id, state=ONBOARDING_STEP1_SENT)
                logger.info("onboarding state advanced account=%s pending -> step1_sent", account_id)
            except Exception as err:
                logger.error("onboarding state set failed account=%s error=%s", account_id, err)
        elif onboarding_pre_extracted is not None:
            try:
                # session.turn_count is pre-increment; normal flow reaches step2 at count=2.
                # Any higher count means we've already held for one confirmation turn.
                _confirmation_ask_count = (
                    max(0, int(session.get("turn_count", 0)) - 2)
                    if onboarding_state == ONBOARDING_STEP2_SENT
                    else 0
                )
                # 强制 AI 身份账号：step1_sent 收到用户称呼后无更多可问，直接完成 onboarding（§4.4）。
                _attribution_for_advance = get_campaign_attribution(account_id=account_id)
                new_state = next_onboarding_state(
                    current_state=onboarding_state,
                    extracted=onboarding_pre_extracted,
                    user_name_ask_count=0,
                    persona_ask_count=0,
                    confirmation_ask_count=_confirmation_ask_count,
                    has_forced_soul_preset=bool(
                        (_attribution_for_advance or {}).get("soul_preset_key")
                    ),
                    has_forced_ai_name=bool(
                        (_attribution_for_advance or {}).get("ai_name_preset")
                    ),
                )
                if new_state != onboarding_state:
                    set_account_onboarding_state(account_id=account_id, state=new_state)
                    logger.info(
                        "onboarding state advanced account=%s %s -> %s",
                        account_id,
                        onboarding_state,
                        new_state,
                    )
                    if new_state == ONBOARDING_COMPLETE:
                        # 使命分配与 SOUL/IDENTITY 首次生成同一时机（agent_mission_and_
                        # orchestration_design.md §5.1）；幂等，失败不影响本轮回复。
                        try:
                            assign_mission_if_absent(account_id=account_id)
                        except Exception as err:
                            logger.error("mission assignment failed account=%s error=%s", account_id, err)
            except Exception as err:
                logger.exception("onboarding state advance failed account=%s error=%s", account_id, err)
        _record_timing(timings, "onboarding_advance_ms", onboarding_advance_started)

    should_run_after_turn = (
        not generation_error
        and not inbound_blocked
        and text
        and text not in _SPECIAL_COMMANDS
    )
    # commitment 抽取已改为工具调用（create_commitment，见 app/tools/commitment_handlers.py），
    # 不再无条件跑隐藏分类器；extract_commitment_from_turn 保留供参考/单测，不在此处调度。
    # after-turn 后台工作统一由 _dispatch_after_turn（+_AFTER_TURN_HOOKS 注册表）派发，
    # _schedule_on_loop 是唯一挂载口（含 shutdown 竞态防护）。
    if should_run_after_turn:
        after_turn_enqueue_started = time.monotonic()
        _dispatch_after_turn(
            _AfterTurnContext(
                account_id=account_id,
                text=text,
                reply=reply,
                business_day=business_day,
                session=session,
                message_id=message_id,
                reply_message_id=reply_message_id,
                message_type=ctx.message_type,
                identity=identity,
                binding=binding,
                openclaw_session_key=openclaw_session_key,
                normal_reply_generated=normal_reply_generated,
                cap=setup.cap,
                moderation_blocked=bool(moderation_reply_metadata.get("moderation_blocked")),
                onboarding_active=onboarding_active,
                image_understanding_failed=inbound.image_understanding_failed,
            ),
            background_loop=background_loop,
            timings=timings,
            started_at=after_turn_enqueue_started,
        )

    response_metadata = {
        **identity_response_metadata(identity, account_id),
        "channel_binding_id": binding["id"],
        "account_active_session_key": ACCOUNT_ACTIVE_SESSION_KEY,
        "message_type": ctx.message_type,
        "latency_ms": latency_ms,
        "user_profile_path": str(profile_path),
        "debug_trace_id": trace_id,
        "billing": {
            "charged": bool(billing_result and billing_result.get("ledger")),
            "estimated": True,
            "amount_shells": (
                billing_result["ledger"]["amount_shells"]
                if billing_result and billing_result.get("ledger")
                else None
            ),
            "balance_shells": (
                billing_result["wallet"]["balance_shells"]
                if billing_result and billing_result.get("wallet")
                else None
            ),
        },
    }
    # 带外投递（工具轮最终回复绕过 OpenClaw 同步超时）仅对声明 supports_out_of_band_tool_final
    # 的渠道启用（微信 True → 走历史带外路径）。不支持的渠道（如 Web V1 纯同步）**绝不** node_send_text，
    # 直接落到下方同步返回，由调用方拿 reply。
    if tool_names:
        response_metadata["tool_names_used"] = tool_names
        if setup.cap.supports_out_of_band_tool_final:
            try:
                send_result = _send_tool_final_reply(
                    identity=identity,
                    account_id=account_id,
                    openclaw_session_key=openclaw_session_key,
                    reply=reply,
                    reply_message_id=reply_message_id,
                )
                response_metadata["delivery_mode"] = "out_of_band_tool_final"
                response_metadata["gateway_message_id"] = (
                    send_result.get("messageId") or send_result.get("message_id")
                )
                logger.info(
                    "tool_final_reply_sent account=%s tools=%s reply_message_id=%s gateway_message_id=%s",
                    account_id,
                    tool_names,
                    reply_message_id,
                    response_metadata["gateway_message_id"],
                )
                return OpenClawTurnResponse(
                    status="ok",
                    no_reply=True,
                    metadata=response_metadata,
                )
            except Exception as err:
                response_metadata["delivery_mode"] = "sync_response_fallback"
                response_metadata["tool_final_send_error"] = str(err)
                logger.warning(
                    "tool_final_reply send failed account=%s tools=%s reply_message_id=%s error=%s",
                    account_id,
                    tool_names,
                    reply_message_id,
                    err,
                )

    return OpenClawTurnResponse(
        status="ok",
        reply=reply,
        metadata=response_metadata,
    )


@dataclass
class ChannelTurnInput:
    """渠道无关的一次 turn 规范化输入——``run_turn_for_account`` 的唯一入参。

    身份/账号解析已由渠道 adapter 完成（微信见 ``build_channel_input_from_openclaw``）：
    ``account_id``/``cap``/``identity`` 是 adapter 产出，核心不再重复解析、也不再触碰任何
    渠道 DTO（OpenClaw 等）。Phase 1 的 ``/web/turn`` 只需构造本对象即可复用同一核心，
    无需伪造 ``OpenClawTurnRequest``。``identity`` 沿用 ``ResolvedIdentity``（其字段
    channel/session_key/sender_id/chat_id 本就渠道通用），阶段 B/C/D 的 ``setup.identity.*``
    读法因此保持不变。
    """
    account_id: str
    cap: ChannelCapability
    identity: ResolvedIdentity
    message_id: Optional[str]
    event_id: Optional[str]
    message_type: str
    text: Optional[str]
    media: Optional[MediaPayload]
    raw: Dict[str, Any]
    sender_name: Optional[str]
    background_loop: Optional[asyncio.AbstractEventLoop] = None
    force_web_search_enabled: Optional[bool] = None
    # 原 _openclaw_id_diagnostics 结果（渠道相关，对核心不透明），仅供日志。
    inbound_diagnostics: Dict[str, Any] = field(default_factory=dict)
    # 域层注入的外部 context 块（L3 等，ADR §7.3 接缝①）。WeChat 入口不填 → 默认空 →
    # 组装时退化 no-op；form-B（App）入口由 platform composition 读取并渲染 L3 后填入。
    extra_blocks: List[ContextBlock] = field(default_factory=list)
    # 可选 typed memory 出向接缝；form-A 默认 None，Companion World App 由组合根注入。
    memory_sink: Optional["MemorySink"] = None


def run_turn_for_account(ctx: ChannelTurnInput) -> OpenClawTurnResponse:
    """渠道无关的一次 turn 编排入口。入参 ``ctx`` 已由渠道 adapter 完成身份/账号解析与
    入口守卫（非私聊/unbound，见 ``build_channel_input_from_openclaw``）。编排脊柱：账号级
    守卫+初始化 → 入站持久化+筛查 → 解析回复 → 终结。各阶段细节见对应 _prepare_turn/
    _persist_and_screen_inbound/_resolve_turn_reply/_finalize_turn。"""
    background_loop = ctx.background_loop
    force_web_search_enabled = ctx.force_web_search_enabled
    started_at = time.monotonic()
    timings: Dict[str, int] = {}
    id_diagnostics = ctx.inbound_diagnostics
    # 入口只记 metadata，不记正文。身份/账号已由 adapter 解析，非私聊/unbound 已在入口收口。
    # 正文日志移到 disabled 检查之后（见 _prepare_turn）。
    logger.info(
        "openclaw_turn received channel=%s session=%s sender=%s type=%s "
        "message_id=%s event_id=%s ctx_run_id=%s ctx_session_id=%s raw_keys=%s",
        ctx.identity.channel,
        ctx.identity.session_key,
        ctx.identity.sender_id,
        ctx.message_type,
        ctx.message_id,
        ctx.event_id,
        id_diagnostics.get("ctx_run_id"),
        id_diagnostics.get("ctx_session_id"),
        id_diagnostics.get("raw_keys"),
    )

    prepare_started = time.monotonic()
    setup = _prepare_turn(ctx, started_at=started_at)
    _record_timing(timings, "prepare_ms", prepare_started)
    if isinstance(setup, OpenClawTurnResponse):
        timings["reply_ready_ms"] = _elapsed_ms(started_at)
        setup_metadata = setup.metadata or {}
        _log_turn_timing(
            ctx=ctx,
            timings=timings,
            started_at=started_at,
            status=setup.status,
            account_id=setup_metadata.get("account_id") or setup_metadata.get("ai4all_account_id"),
            session=setup_metadata.get("session_key") or ctx.identity.session_key,
            message_id=ctx.message_id or ctx.event_id,
        )
        return setup

    inbound_started = time.monotonic()
    inbound = _persist_and_screen_inbound(
        ctx,
        setup,
        started_at=started_at,
        timings=timings,
    )
    _record_timing(timings, "inbound_total_ms", inbound_started)
    if isinstance(inbound, OpenClawTurnResponse):
        timings["reply_ready_ms"] = _elapsed_ms(started_at)
        _log_turn_timing(
            ctx=ctx,
            timings=timings,
            started_at=started_at,
            status=inbound.status,
            account_id=setup.account_id,
            session=setup.openclaw_session_key,
            message_id=setup.message_id,
        )
        return inbound

    reply_started = time.monotonic()
    result = _resolve_turn_reply(
        ctx,
        setup,
        inbound,
        background_loop=background_loop,
        force_web_search_enabled=force_web_search_enabled,
        timings=timings,
    )
    _record_timing(timings, "reply_total_ms", reply_started)

    latency_ms = _elapsed_ms(started_at)
    timings["reply_ready_ms"] = latency_ms

    finalize_started = time.monotonic()
    response = _finalize_turn(
        ctx,
        setup,
        inbound,
        result,
        latency_ms=latency_ms,
        background_loop=background_loop,
        timings=timings,
    )
    _record_timing(timings, "finalize_total_ms", finalize_started)
    _log_turn_timing(
        ctx=ctx,
        timings=timings,
        started_at=started_at,
        status=response.status,
        account_id=setup.account_id,
        session=setup.openclaw_session_key,
        message_id=setup.message_id,
        error=result.generation_error,
    )
    return response


def build_channel_input_from_openclaw(
    payload: OpenClawTurnRequest,
    *,
    background_loop: Optional[asyncio.AbstractEventLoop] = None,
    force_web_search_enabled: Optional[bool] = None,
) -> Union[ChannelTurnInput, OpenClawTurnResponse]:
    """微信渠道 adapter：把 OpenClaw 入站 DTO 归一为渠道无关 ``ChannelTurnInput``。

    完成入口守卫（非私聊）、身份归一、按 binding 的账号解析与 unbound 收口、渠道能力选定。
    命中入口守卫返回 ``OpenClawTurnResponse``（入口即收口，不进核心）；否则返回规范化
    ``ChannelTurnInput``。**OpenClaw DTO 仅在本函数内出现**——核心链路从此不再触碰任何渠道 DTO。
    """
    id_diagnostics = _openclaw_id_diagnostics(payload)

    if payload.chat_type != "private":
        logger.info(
            "openclaw_turn ignored non_private channel=%s session=%s message_id=%s chat_type=%s",
            payload.channel,
            payload.session_key,
            payload.message_id or payload.event_id,
            payload.chat_type,
        )
        return OpenClawTurnResponse(status="ignored", no_reply=True)

    identity = resolve_openclaw_identity(
        channel=payload.channel,
        session_key=payload.session_key,
        channel_account_id=payload.channel_account_id or payload.account_id,
        sender_id=payload.sender_id,
        chat_id=payload.chat_id,
    )
    openclaw_session_key = identity.session_key
    # 渠道能力：单一开关来源，下游 onboarding/active-scope/工具/TDAI/投递 均由此分支。
    # 微信 cap 全 True，取值与历史等价；未知渠道回落微信 cap（get_channel_capability）。
    cap = get_channel_capability(identity.channel)
    resolved_account_id = resolve_account_id_for_inbound_channel_identity(
        channel=identity.channel,
        session_key=identity.session_key,
        channel_account_id=identity.channel_account_id,
    )
    if resolved_account_id is None:
        # 找不到 completed binding。收口：不再用 session_key 兜底创建账号，
        # 避免已解绑/未绑定的远端微信账号被当作新账号自动激活并继续回复。
        # 仅记 channel/account/session metadata，不记正文（解绑后隐私预期）。
        if getattr(settings, "openclaw_inbound_require_binding", True):
            logger.info(
                "openclaw_turn ignored unbound inbound channel=%s channel_account=%s "
                "session=%s message_id=%s",
                identity.channel,
                identity.channel_account_id,
                openclaw_session_key,
                payload.message_id or payload.event_id,
            )
            return OpenClawTurnResponse(
                status="ignored",
                no_reply=True,
                metadata={
                    "reason": "no_binding",
                    "channel": identity.channel,
                    "channel_account_id": identity.channel_account_id,
                    "session_key": openclaw_session_key,
                },
            )
        # 开关关闭（本地调试/测试）：保留 session_key 兜底，但优先复用该
        # session_key 已落过的账号（如 debug 建号直接写 sessions，没有走
        # binding），避免每次兜底都新建一个不同的影子账号（session_key
        # 本身也会变化，见 get_account_id_for_session_key 注释）。
        resolved_account_id = (
            get_account_id_for_session_key(session_key=openclaw_session_key)
            or openclaw_session_key
        )

    return ChannelTurnInput(
        account_id=resolved_account_id,
        cap=cap,
        identity=identity,
        message_id=payload.message_id or payload.event_id,
        event_id=payload.event_id,
        message_type=payload.message_type,
        text=payload.text,
        media=payload.media,
        raw=payload.raw,
        sender_name=payload.sender_name,
        background_loop=background_loop,
        force_web_search_enabled=force_web_search_enabled,
        inbound_diagnostics=id_diagnostics,
    )


def handle_openclaw_turn(
    payload: OpenClawTurnRequest,
    *,
    background_loop: Optional[asyncio.AbstractEventLoop] = None,
    force_web_search_enabled: Optional[bool] = None,
) -> OpenClawTurnResponse:
    """每条入站微信消息的主入口（薄封装）：经微信 adapter 归一为 ``ChannelTurnInput`` 后委派
    ``run_turn_for_account``。保留函数名与签名不变，既有 router/脚本/测试调用方零改动。adapter
    命中入口守卫（非私聊/unbound）时直接返回其 ``OpenClawTurnResponse``。"""
    result = build_channel_input_from_openclaw(
        payload,
        background_loop=background_loop,
        force_web_search_enabled=force_web_search_enabled,
    )
    if isinstance(result, OpenClawTurnResponse):
        return result
    return run_turn_for_account(result)
