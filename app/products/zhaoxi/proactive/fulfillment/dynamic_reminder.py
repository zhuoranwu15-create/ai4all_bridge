"""动态提醒履约器：合成轮次 + 可配工具集，产出一条带来源的简报正文。

设计要点（见动态提醒设计文档 §5）：
- 复用普通对话的 tool loop（app.agent_runtime.llm.service.generate_reply_with_tools），因此天然可用完整工具注册表；
  工具集由履约 policy 决定，v1 只带 web_search 且首轮强制。
- 「强制搜索」用 first_round_tool_choice 表达，不靠砍工具集；将来放开工具集时该约束仍独立。
- 校验「至少一次搜索成功」用 ctx.web_search_success_count（web_search handler 成功时自增）；
  搜索全失败则判履约失败、不发编造内容。
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.config import settings
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.time_utils import beijing_naive_now

logger = logging.getLogger("ai4all.proactive.fulfillment")


@dataclass
class FulfillmentResult:
    """一次履约的产出。text 为空/ok=False 时调用方不得发送。"""

    ok: bool
    text: str = ""
    search_ok: bool = False
    error: Optional[str] = None
    search_trace: Dict[str, Any] = field(default_factory=dict)


# 工具名 -> 该工具的 schema 获取函数（履约可用工具白名单的来源）。
# v1 只登记 web_search；后续放开只需在此登记更多工具并在配置里列出。
def _tool_schema_registry() -> Dict[str, Any]:
    from app.tools.definitions import get_web_search_tools

    return {"web_search": get_web_search_tools}


def _csv(value: Any) -> List[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def is_dynamic_reminder_allowed(account_id: str) -> bool:
    """账号是否可创建/履约动态提醒。

    只看单一总开关 dynamic_reminder_enabled：开=对全部账号放开（全量），关=整体禁用。
    不再按账号灰度（account_id 保留在签名里，供将来需要时重新引入收窄逻辑）。
    """
    _ = account_id  # 当前不按账号收窄；保留参数以兼容调用方与未来扩展
    return bool(getattr(settings, "dynamic_reminder_enabled", False))


def _fulfillment_tools() -> List[Dict[str, Any]]:
    """按配置装配履约工具集（未登记的工具名忽略并告警）。"""
    registry = _tool_schema_registry()
    names = _csv(getattr(settings, "dynamic_reminder_fulfillment_tools", "web_search")) or ["web_search"]
    tools: List[Dict[str, Any]] = []
    for name in names:
        getter = registry.get(name)
        if getter is None:
            logger.warning("dynamic reminder fulfillment tool not registered: %s", name)
            continue
        tools.extend(getter())
    return tools


def _forced_first_tool_choice(tools: List[Dict[str, Any]]) -> Any:
    """首轮工具选择：默认 "auto"。

    注意：当前 PRO 档是 thinking 模型（deepseek-v4-pro），其 API 只接受 tool_choice="auto"，
    传指定函数或 "required" 都会 400。因此默认不在 API 层强制；"必须先搜索" 由履约提示词 +
    search_ok 后置校验保证（搜不到不发编造）。仅当 dynamic_reminder_force_first_tool 被显式
    配成某工具、且该工具在本次工具集内时，才下发指定函数 tool_choice（供未来非 thinking provider）。
    """
    forced = _csv(getattr(settings, "dynamic_reminder_force_first_tool", ""))
    if not forced:
        return "auto"
    name = forced[0]
    available = {t.get("function", {}).get("name") for t in tools}
    if name not in available:
        return "auto"
    return {"type": "function", "function": {"name": name}}


def _suggested_date_after(reminder: Dict[str, Any], now: datetime) -> str:
    """搜索起始日期：优先上次成功履约时间，首次回退到最近一周。"""
    last = (reminder.get("content_meta") or {}).get("last_success_run_at")
    if last:
        try:
            return str(last).split(" ")[0]
        except Exception:  # noqa: BLE001 — 脏数据不阻断，回退默认
            pass
    return (now - timedelta(days=7)).strftime("%Y-%m-%d")


def _build_prompt(reminder: Dict[str, Any], now: datetime) -> str:
    meta = reminder.get("content_meta") or {}
    topic = str(meta.get("topic") or reminder.get("text") or "").strip()
    instructions = str(meta.get("instructions") or "").strip()
    try:
        max_items = int(meta.get("max_items") or 5)
    except (TypeError, ValueError):
        max_items = 5
    max_items = max(3, min(max_items, 5))
    date_after = _suggested_date_after(reminder, now)
    now_str = now.strftime("%Y-%m-%d %H:%M")

    lines = [
        "你在为用户生成一条【例行简报】（用户此前明确订阅的定时内容推送），"
        "不是普通聊天回复。请严格遵守以下要求：",
        f"当前时间：{now_str}（北京时间）。",
        f"简报主题：{topic or '（未指定，按用户历史意图理解）'}。",
    ]
    if instructions:
        lines.append(f"用户补充要求：{instructions}")
    lines += [
        "",
        "硬性规则：",
        "1. 必须先调用 web_search 检索最新信息，严禁仅凭模型已有知识生成内容。",
        f"2. 搜索优先检索 {date_after} 之后发布的信息（date_after={date_after}），获取最新进展。",
        "3. 只能引用搜索结果中真实出现的标题、来源与时间，不得编造或臆测。",
        f"4. 汇总为 {max_items} 条以内的要点，每条一句话概述，并附来源（标题或链接）。",
        "5. 全部内容必须能放进一条微信消息（简洁，避免长段落）。",
        "6. 如果搜索失败或没有可用结果，直接说明本期无法生成，不要用旧知识凑内容。",
        "",
        "现在开始：先检索，再据检索结果生成本期简报正文（直接给用户看的最终文本）。",
    ]
    return "\n".join(lines)


def _build_synthetic_ctx(reminder: Dict[str, Any], now: datetime):
    """构造无用户输入的合成 TurnContext：account 隔离 + 放行 web_search。"""
    from app.db import get_account
    from app.products.zhaoxi.application.memory.session_lifecycle import business_day_for
    from app.agent_runtime.context.models import TurnContext

    account_id = reminder["account_id"]
    account = get_account(account_id=account_id) or {"id": account_id}
    business_day = business_day_for(
        now,
        start_hour=int(getattr(settings, "conversation_session_business_day_start_hour", 4)),
    )
    return TurnContext(
        account_id=account_id,
        app_id=ZHAOXI_APP_ID,
        account=account,
        session={},  # 无会话行：tool invocation 记录 session_id=None
        identity=None,
        binding={},
        message_id=f"dynrem-{reminder.get('id')}-{now.strftime('%Y%m%d%H%M%S')}",
        text="",
        today=now.strftime("%Y-%m-%d"),
        business_day=business_day,
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="completed",
        onboarding_active=False,
        web_search_enabled=True,  # 履约必须能搜索（独立于账号普通 web_search 开关）
    )


def fulfill_dynamic_reminder(
    reminder: Dict[str, Any], *, now: Optional[datetime] = None
) -> FulfillmentResult:
    """跑一次履约合成轮次，返回可发送正文（或失败原因）。never raises。"""
    current = now or beijing_naive_now()
    tools = _fulfillment_tools()
    if not tools:
        return FulfillmentResult(ok=False, error="no_fulfillment_tools_configured")

    try:
        from app.agent_runtime.llm.service import TIER_PRO, generate_reply_with_tools

        ctx = _build_synthetic_ctx(reminder, current)
        prompt = _build_prompt(reminder, current)
        forced_choice = _forced_first_tool_choice(tools)
        reply, err = generate_reply_with_tools(
            user_text="请根据上述要求检索并生成本期简报。",
            history=[],
            system_prompt=prompt,
            tools=tools,
            ctx=ctx,
            first_round_tool_choice=forced_choice,
            tier=TIER_PRO,
        )
    except Exception as exc:  # noqa: BLE001 — 履约失败降级为结果，不拖垮调度
        logger.exception("dynamic reminder fulfillment crashed reminder=%s", reminder.get("id"))
        return FulfillmentResult(ok=False, error=f"fulfillment_exception: {exc}")

    search_ok = int(getattr(ctx, "web_search_success_count", 0) or 0) > 0
    trace = {"web_search_success_count": int(getattr(ctx, "web_search_success_count", 0) or 0)}

    if err:
        return FulfillmentResult(ok=False, search_ok=search_ok, error=str(err), search_trace=trace)
    text = (reply or "").strip()
    if not text:
        return FulfillmentResult(ok=False, search_ok=search_ok, error="empty_generation", search_trace=trace)
    if not search_ok:
        # 搜索全失败：即便模型给了文本也不发（可能是纯知识编造/过期内容）。
        return FulfillmentResult(ok=False, search_ok=False, error="search_failed", search_trace=trace)
    return FulfillmentResult(ok=True, text=text, search_ok=True, search_trace=trace)
