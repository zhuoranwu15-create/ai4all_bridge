"""First-chat onboarding state machine, prompt context builder, and info extractor."""
import asyncio
import json
import logging
from datetime import datetime

from app.time_utils import beijing_now
from typing import Optional

from app.config import settings

logger = logging.getLogger("ai4all.onboarding")

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

ONBOARDING_PENDING = "pending"
ONBOARDING_STEP1_SENT = "step1_sent"
ONBOARDING_STEP2_SENT = "step2_sent"
ONBOARDING_STEP3_SENT = "step3_sent"
ONBOARDING_COMPLETE = "complete"
ONBOARDING_TIMED_OUT = "timed_out"

ACTIVE_ONBOARDING_STATES = {
    ONBOARDING_PENDING,
    ONBOARDING_STEP1_SENT,
    ONBOARDING_STEP2_SENT,
    ONBOARDING_STEP3_SENT,
}

# Onboarding welcome text sent to the user as the bot's opening message.
#
# Trigger path A (ideal): 5-second timer after binding completes (main.py).
#   Fails in practice because WeChat sender_id is not returned by web.login.wait —
#   OpenClaw only provides the bot account ID at QR-scan time, not the user's wxid.
#
# Trigger path B (actual): first inbound message arrives in turn_service.py.
#   At that point sender_id is finally known. We send the welcome immediately and
#   return no_reply=True so the user's first message content is absorbed rather than
#   answered. From the user's perspective the bot appears to have greeted them first.
ONBOARDING_WELCOME_TEXT = "你好，很高兴能成为微信好友，你希望我怎么称呼你？"

# Persona preset keys that users can choose in the combined AI-name/persona step.
PERSONA_PRESETS = {
    "1": "blank",
    "2": "xiaotaiyang",
    "3": "xiaoyueya",
    "4": "ju",
    "blank": "blank",
    "留白": "blank",
    "空着": "blank",
    "小太阳": "xiaotaiyang",
    "小月牙": "xiaoyueya",
    "橘": "ju",
    "5": "custom",
    "custom": "custom",
    "自己设定": "custom",
    "自定义": "custom",
}

PERSONA_PRESET_NAMES_ZH = {
    "blank": "留白（默认）",
    "xiaotaiyang": "小太阳",
    "xiaoyueya": "小月牙",
    "ju": "橘",
}

PERSONA_OPTION_LINES_WITH_PRESET_NAMES = [
    "1. 先留白，后续相处里慢慢养成",
    "2. 小太阳 —— 明亮主动，能量往外扑，护短又会看脸色",
    "3. 小月牙 —— 安静发微光，平和地表达自己的感悟",
    "4. 橘 —— 慵懒傲娇，性格难以捉摸的小猫仙",
    "5. 你自己设定：直接告诉我想怎么叫我、希望我是什么样",
]

ONBOARDING_TIMEOUT_MINUTES = 15

# Question ask limits
USER_NAME_ASK_LIMIT = 2
PERSONA_ASK_LIMIT = 1


def is_onboarding_active(state: str) -> bool:
    return state in ACTIVE_ONBOARDING_STATES


def is_onboarding_done(state: str) -> bool:
    return state in {ONBOARDING_COMPLETE, ONBOARDING_TIMED_OUT}


# ---------------------------------------------------------------------------
# Prompt context builder
# ---------------------------------------------------------------------------

def build_onboarding_prompt_context(
    *,
    state: str,
    user_name: Optional[str],
    ai_name: Optional[str],
    persona: Optional[str],
    user_name_ask_count: int,
    persona_ask_count: int,
    needs_confirmation: bool = False,
) -> str:
    """Build the onboarding guidance block to inject into the system prompt.

    Returns empty string when onboarding is not active.
    """
    if not is_onboarding_active(state):
        return ""

    collected_parts = []
    if user_name:
        collected_parts.append(f"用户称呼：{user_name}")
    if ai_name:
        collected_parts.append(f"AI 称呼：{ai_name}")
    if persona:
        collected_parts.append(f"人设选择：{PERSONA_PRESET_NAMES_ZH.get(persona, persona)}")

    collected_str = "、".join(collected_parts) if collected_parts else "（尚未收集）"

    lines = [
        "【首次聊天 Onboarding】",
        f"当前账号处于首次聊天 onboarding 阶段，当前步骤：{state}",
        f"已收集信息：{collected_str}",
        "",
    ]

    if state == ONBOARDING_PENDING:
        lines.append("本轮目标：自然回应用户内容（如有），然后引出欢迎语并询问用户希望如何称呼自己。")
        lines.append('参考话术（仅供参考，请按你的性格自然表达）：你好，很高兴能成为微信好友，你希望我怎么称呼你？')

    elif state == ONBOARDING_STEP1_SENT:
        # User is replying to "what should I call you?" — next ask the combined AI setup question.
        if user_name:
            lines.append(f'用户刚才回复了你问的称呼问题，他们想被你称为"{user_name}"。请自然确认这个称呼，然后合并询问用户想怎么称呼你（AI），以及希望你是什么样的陪伴。')
        else:
            lines.append('用户刚刚回复了你的问题（你问的是"你希望我怎么称呼你？"），但尚未明确提取到用户称呼。请不要硬猜用户称呼，直接进入下一步：合并询问用户想怎么称呼你（AI），以及希望你是什么样的陪伴。')
        lines.append("请在回复末尾自然列出以下候选。候选里的名字只是建议，用户可以沿用，也可以改成自己喜欢的名字：")
        lines.extend(PERSONA_OPTION_LINES_WITH_PRESET_NAMES)
        lines.append("用户不用按固定格式回复，可以回复编号、候选名、改名后的候选，或直接自己描述。")

    elif state == ONBOARDING_STEP2_SENT:
        # User is now replying to the combined AI-name/persona question.
        if needs_confirmation:
            # Extraction found something but it's ambiguous — do one light confirmation turn.
            known = []
            if ai_name:
                known.append(f'AI 称呼：{ai_name}')
            if persona:
                known.append(f'人设：{PERSONA_PRESET_NAMES_ZH.get(persona, persona)}')
            known_str = "、".join(known) if known else "（未明确）"
            lines.append(
                f"用户刚刚回复了你关于 AI 称呼和人设的问题，但含义有一点歧义。"
                f"当前提取到的理解是：{known_str}。"
                "请用一句自然的话向用户轻量确认你的理解，例如「我理解成你想叫我小满，性格大概是朝朝那种，对吗？」。"
                "确认语气要轻，不要让用户感到在填表单。"
                "不要重新展示候选列表。"
            )
        elif ai_name or persona:
            known = []
            if ai_name:
                known.append(f'AI 称呼：{ai_name}')
            if persona:
                known.append(f'人设：{PERSONA_PRESET_NAMES_ZH.get(persona, persona)}')
            lines.append(f"用户刚刚回复了你关于 AI 称呼和人设的问题。已提取到：{'、'.join(known)}。请**完全以你刚被设定的角色性格和语气**回复确认，这是你第一次用这个角色开口说话，让用户感受到角色的样子。然后自然进入正常聊天，不再追问 onboarding 问题。")
        else:
            lines.append("用户刚刚回复了你关于 AI 称呼和人设的问题，但没有明确设定或选择了留白/跳过。请以你当前的角色语气自然接住，不再追问 onboarding 问题，可以表达之后慢慢相处中养成。")

    elif state == ONBOARDING_STEP3_SENT:
        # Legacy state from the old three-step flow. Acknowledge and complete.
        if ai_name:
            lines.append(f'用户刚刚回复了你关于性格选择的问题。接收他们的选择，以"{ai_name}"的身份自然温暖地完成 onboarding，不需要再问任何 onboarding 问题。')
        else:
            lines.append("用户刚刚回复了你关于性格选择的问题。接收他们的选择，自然温暖地完成 onboarding，不需要再问任何 onboarding 问题。")

    lines += [
        "",
        "重要规则：",
        "- 先自然回应用户内容，再在回复末尾轻量引出当前 onboarding 问题（未超出询问上限时）。",
        "- onboarding 阶段不会调用网络搜索、提醒等工具；如果用户要求这类能力，说明先完成当前称呼/设定后就可以帮忙，不要说自己没有这些能力。",
        "- 不要暴露 onboarding 状态机或技术细节。",
        "- 如用户明确说随便、不设置、跳过或先留白，接受并推进。",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-based info extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """你是一个信息提取助手。从用户的消息中提取以下信息（JSON 格式）：

{
  "user_name": "用户希望被称呼的名字或昵称（字符串，未提供为 null）",
  "ai_name": "用户给 AI 起的名字（字符串，未提供为 null）",
  "ai_name_source": "AI 名字来源：preset/modified_preset/custom/none",
  "persona": "用户选择的 AI 性格（只能是：blank/xiaotaiyang/xiaoyueya/ju/custom，未提供为 null）",
  "persona_custom": "若 persona=custom，用户描述的自定义性格（字符串，否则为 null）",
  "skip": "用户是否明确表示跳过、随便或先留白（布尔值）",
  "needs_confirmation": "用户明显在设定但关键含义有歧义，需要确认（布尔值）"
}

名字提取规则：
- 只有用户明确表达"叫我 X / 你叫 X / 以后叫你 X / 我想叫你 X"时才填写名字。
- 不要把普通聊天内容、问题、句子里的泛称或你猜测出来的昵称当名字。
- "随便"、"都行"、"不设"、"跳过"、"无所谓"、"先空着"、"先留白" 等 → skip=true，未明确字段为 null。

persona 取值规则：
- 用户说"1"、"留白"、"空着"→ "blank"
- 用户说"2"、"小太阳" → "xiaotaiyang"
- 用户说"3"、"小月牙" → "xiaoyueya"
- 用户说"4"、"橘" → "ju"
- 用户说"5"、"自己设定"或自由描述了性格 → "custom"
- 用户选择预设但改了名字，例如"选 2，但叫你小满"：ai_name="小满"，ai_name_source="modified_preset"，persona="xiaotaiyang"
- 用户选择预设且未改名：ai_name 填候选名，ai_name_source="preset"，persona 填对应预设
- 用户只给 AI 名字，没给人设：只填 ai_name，persona=null
- 用户只给人设，没给 AI 名字：只填 persona，ai_name=null，除非选择了带名字的预设
- 未明确选择 → null

只返回 JSON，不要解释。"""


async def extract_onboarding_info_async(
    *,
    user_text: str,
    current_state: str,
) -> dict:
    """Call LLM to extract onboarding info from user reply.

    Returns dict with keys: user_name, ai_name, persona, persona_custom, skip.
    All values default to None/False on failure.
    """
    default = {
        "user_name": None,
        "ai_name": None,
        "ai_name_source": "none",
        "persona": None,
        "persona_custom": None,
        "skip": False,
        "needs_confirmation": False,
    }
    if not user_text or not user_text.strip():
        return default

    # Only extract what's relevant for the current step
    extract_user_name = current_state in {ONBOARDING_PENDING, ONBOARDING_STEP1_SENT, ONBOARDING_STEP2_SENT}
    extract_ai_name = current_state == ONBOARDING_STEP2_SENT
    extract_persona = current_state in {ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT}

    if not (extract_user_name or extract_ai_name or extract_persona):
        return default

    try:
        from app.llm import generate_completion  # noqa: PLC0415  (lazy import, avoid circular)
        messages = [
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": f"当前步骤：{current_state}\n用户消息：{user_text}"},
        ]
        raw = await asyncio.to_thread(generate_completion, messages)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        extracted = json.loads(raw)
        result = {**default}
        if extract_user_name and extracted.get("user_name"):
            result["user_name"] = str(extracted["user_name"]).strip() or None
        if extract_ai_name and extracted.get("ai_name"):
            result["ai_name"] = str(extracted["ai_name"]).strip() or None
            result["ai_name_source"] = str(extracted.get("ai_name_source") or "custom").strip() or "custom"
        if extract_persona and extracted.get("persona"):
            result["persona"] = _resolve_persona_preset(str(extracted["persona"]))
            result["persona_custom"] = extracted.get("persona_custom")
        result["skip"] = bool(extracted.get("skip"))
        result["needs_confirmation"] = bool(extracted.get("needs_confirmation"))
        if result["skip"] and extract_ai_name and result["persona"] != "blank":
            result["ai_name"] = None
        return result
    except Exception as err:
        logger.warning("onboarding info extraction failed state=%s error=%s", current_state, err)
        return default


# ---------------------------------------------------------------------------
# Context file write helpers (calls user_profiles functions)
# ---------------------------------------------------------------------------

# Default AI names for named presets, used when user didn't give a custom name
_PRESET_DEFAULT_NAMES = {"xiaotaiyang": "小太阳", "xiaoyueya": "小月牙", "ju": "橘"}


def apply_extracted_onboarding_info(
    *,
    account_id: str,
    extracted: dict,
    current_state: str,
) -> dict:
    """Write confirmed onboarding info to context files.

    Returns dict summarising what was written.
    """
    from app.user_profiles import (  # noqa: PLC0415
        apply_soul_preset,
        context_file_path,
        write_ai_name_to_identity,
        write_user_name,
    )

    written = {}

    user_name = extracted.get("user_name")
    if user_name and current_state in {ONBOARDING_PENDING, ONBOARDING_STEP1_SENT, ONBOARDING_STEP2_SENT}:
        try:
            write_user_name(account_id=account_id, name=user_name)
            written["user_name"] = user_name
        except Exception as err:
            logger.error("write user_name failed account=%s error=%s", account_id, err)

    ai_name = extracted.get("ai_name")
    if ai_name and current_state == ONBOARDING_STEP2_SENT:
        try:
            write_ai_name_to_identity(account_id=account_id, name=ai_name)
            written["ai_name"] = ai_name
        except Exception as err:
            logger.error("write ai_name failed account=%s error=%s", account_id, err)

    persona = extracted.get("persona")
    if persona and current_state in {ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT}:
        preset = _resolve_persona_preset(persona)
        custom_desc = extracted.get("persona_custom") if persona == "custom" else None

        # If user chose a named preset but never gave the AI a name, use the preset name
        if preset in _PRESET_DEFAULT_NAMES and "ai_name" not in written:
            identity_path = context_file_path(account_id, "IDENTITY.md")
            has_name = (
                identity_path.exists() and "AI 名字" in identity_path.read_text(encoding="utf-8")
            )
            if not has_name:
                try:
                    write_ai_name_to_identity(account_id=account_id, name=_PRESET_DEFAULT_NAMES[preset])
                    written["ai_name"] = _PRESET_DEFAULT_NAMES[preset]
                except Exception as err:
                    logger.error("write preset ai_name failed account=%s error=%s", account_id, err)

        try:
            apply_soul_preset(
                account_id=account_id,
                preset_name=preset,
                custom_description=custom_desc,
            )
            written["persona"] = preset
        except Exception as err:
            logger.error("apply soul preset failed account=%s preset=%s error=%s", account_id, preset, err)

    # 旁路打点：人设选择事件。仅存枚举/来源/布尔，绝不写昵称或自定义人设原文。
    persona_selected = extracted.get("persona")
    if persona_selected and current_state in {ONBOARDING_STEP2_SENT, ONBOARDING_STEP3_SENT}:
        try:
            from app.db import record_analytics_event  # noqa: PLC0415

            record_analytics_event(
                account_id=account_id,
                event_name="persona_selected",
                from_state=current_state,
                source="turn_service",
                properties={
                    "persona": written.get("persona"),
                    "is_custom": persona_selected == "custom",
                    "skipped": bool(extracted.get("skip")),
                    "has_ai_name": "ai_name" in written,
                    "has_user_name": "user_name" in written,
                },
            )
        except Exception as err:
            logger.error("persona_selected event emit failed account=%s error=%s", account_id, err)

    return written


def _resolve_persona_preset(raw: str) -> str:
    """Map raw persona value to canonical preset key."""
    mapping = {
        "blank": "blank",
        "xiaotaiyang": "xiaotaiyang",
        "xiaoyueya": "xiaoyueya",
        "ju": "ju",
        "custom": "custom",
    }
    resolved = mapping.get(raw)
    if resolved is None:
        logger.warning("unknown persona preset %r — falling back to blank", raw)
        return "blank"
    return resolved


# ---------------------------------------------------------------------------
# State advancement logic
# ---------------------------------------------------------------------------

def next_onboarding_state(
    *,
    current_state: str,
    extracted: dict,
    user_name_ask_count: int,
    persona_ask_count: int,
    confirmation_ask_count: int = 0,
) -> str:
    """Determine next onboarding state after a turn.

    The caller is responsible for checking if the transition is appropriate.
    Returns the next state string.
    """
    if current_state == ONBOARDING_PENDING:
        return ONBOARDING_STEP1_SENT

    if current_state == ONBOARDING_STEP1_SENT:
        # Got user's reply to step 1 (asked user name)
        # Advance to step2 regardless of whether we extracted a name
        return ONBOARDING_STEP2_SENT

    if current_state == ONBOARDING_STEP2_SENT:
        # Hold in step2 for at most one confirmation turn.
        # confirmation_ask_count >= 1 means we already sent one confirmation; force complete.
        if extracted.get("needs_confirmation") and confirmation_ask_count < 1:
            return ONBOARDING_STEP2_SENT
        return ONBOARDING_COMPLETE

    if current_state == ONBOARDING_STEP3_SENT:
        # Legacy flow: got user's reply to the old persona-only question.
        return ONBOARDING_COMPLETE

    return current_state


def check_onboarding_timeout(*, last_updated_at: Optional[str]) -> bool:
    """Return True if the onboarding has been idle past the timeout threshold."""
    if not last_updated_at:
        return False
    try:
        updated = datetime.fromisoformat(last_updated_at)
        idle_minutes = (beijing_now().replace(tzinfo=None) - updated).total_seconds() / 60
        return idle_minutes >= ONBOARDING_TIMEOUT_MINUTES
    except Exception:
        return False
