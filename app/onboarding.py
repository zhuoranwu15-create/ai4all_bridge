"""First-chat onboarding state machine, prompt context builder, and info extractor."""
import asyncio
import json
import logging
from datetime import datetime
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

# Persona preset keys that users can choose in step 3
PERSONA_PRESETS = {
    "1": "blank",
    "2": "chaochao",
    "3": "xixi",
    "4": "ju",
    "blank": "blank",
    "留白": "blank",
    "空着": "blank",
    "朝朝": "chaochao",
    "夕夕": "xixi",
    "橘": "ju",
}

PERSONA_PRESET_NAMES_ZH = {
    "blank": "留白（默认）",
    "chaochao": "朝朝",
    "xixi": "夕夕",
    "ju": "橘",
}

PERSONA_OPTION_LINES_WITH_PRESET_NAMES = [
    "1. 先空着留白，在我们相处中慢慢养成",
    "2. 朝朝 —— 爱自由、有好奇心，说话直但不失风趣洒脱",
    "3. 夕夕 —— 平和有生活味，喜欢用经历和故事开解人",
    "4. 橘 —— 慵懒傲娇，格难以捉摸的小猫仙",
]

PERSONA_OPTION_LINES_WITHOUT_PRESET_NAMES = [
    "1. 先空着留白，在我们相处中慢慢养成",
    "2. 爱自由、有好奇心，说话直但不失风趣洒脱",
    "3. 平和有生活味，喜欢用经历和故事开解人",
    "4. 慵懒傲娇，格难以捉摸的小猫仙",
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

    can_ask_user_name = user_name_ask_count < USER_NAME_ASK_LIMIT and not user_name
    can_ask_persona = persona_ask_count < PERSONA_ASK_LIMIT and not persona

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
        # User is now REPLYING to the "what should I call you?" question.
        # Acknowledge their name, then immediately ask what they want to call the AI.
        lines.append("用户刚刚回复了你关于称呼的问题。自然接收他们的回应（有无均可），然后紧接着询问用户想怎么称呼你（AI）。")
        lines.append('参考话术：好的！那你想给我起什么名字呢？或者，你希望怎么称呼我？')

    elif state == ONBOARDING_STEP2_SENT:
        # User is now REPLYING to the "what should I call the AI?" question.
        # Acknowledge the AI name, then immediately ask about persona.
        if user_name:
            lines.append(f'用户（{user_name}）刚刚回复了你关于 AI 称呼的问题。自然确认他们给的名字（如有），然后询问用户希望 AI 是什么样的性格。')
        else:
            lines.append("用户刚刚回复了你关于 AI 称呼的问题。自然接收他们的回应，然后询问用户希望 AI 是什么样的性格。")
        if not can_ask_persona:
            lines.append("（人设询问已达上限或已获取，本轮不再追问。）")
        else:
            lines.append("请在回复末尾自然列出以下性格选项：")
            if ai_name:
                lines.append(f'用户已经把 AI 称呼设为"{ai_name}"，选项里不要再展示朝朝、夕夕、橘等预设名字，避免让用户误以为 AI 改名。')
                lines.extend(PERSONA_OPTION_LINES_WITHOUT_PRESET_NAMES)
            else:
                lines.extend(PERSONA_OPTION_LINES_WITH_PRESET_NAMES)
            lines.append("（也可以让用户自己描述想要的风格。）")

    elif state == ONBOARDING_STEP3_SENT:
        # User is now REPLYING to the persona question. Acknowledge and warm wrap-up.
        if ai_name:
            lines.append(f'用户刚刚回复了你关于性格选择的问题。接收他们的选择，以"{ai_name}"的身份自然温暖地完成 onboarding，不需要再问任何 onboarding 问题。')
        else:
            lines.append("用户刚刚回复了你关于性格选择的问题。接收他们的选择，自然温暖地完成 onboarding，不需要再问任何 onboarding 问题。")

    lines += [
        "",
        "重要规则：",
        "- 先自然回应用户内容，再在回复末尾轻量引出当前 onboarding 问题（未超出询问上限时）。",
        "- 不要暴露 onboarding 状态机或技术细节。",
        "- 如用户明确说随便、不设置、跳过，接受并推进。",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-based info extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """你是一个信息提取助手。从用户的消息中提取以下信息（JSON 格式）：

{
  "user_name": "用户希望被称呼的名字或昵称（字符串，未提供为 null）",
  "ai_name": "用户给 AI 起的名字（字符串，未提供为 null）",
  "persona": "用户选择的 AI 性格（只能是：blank/chaochao/xixi/ju/custom，未提供为 null）",
  "persona_custom": "若 persona=custom，用户描述的自定义性格（字符串，否则为 null）",
  "skip": "用户是否明确表示跳过或随便（布尔值）"
}

persona 取值规则：
- 用户说"1"、"留白"、"空着"→ "blank"
- 用户说"2"、"朝朝" → "chaochao"
- 用户说"3"、"夕夕" → "xixi"
- 用户说"4"、"橘" → "ju"
- 用户自由描述了性格 → "custom"
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
        "persona": None,
        "persona_custom": None,
        "skip": False,
    }
    if not user_text or not user_text.strip():
        return default

    # Only extract what's relevant for the current step
    extract_user_name = current_state in {ONBOARDING_PENDING, ONBOARDING_STEP1_SENT}
    extract_ai_name = current_state == ONBOARDING_STEP2_SENT
    extract_persona = current_state == ONBOARDING_STEP3_SENT

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
        if extract_persona and extracted.get("persona"):
            result["persona"] = extracted["persona"]
            result["persona_custom"] = extracted.get("persona_custom")
        result["skip"] = bool(extracted.get("skip"))
        if result["skip"] and extract_ai_name:
            result["ai_name"] = None
        return result
    except Exception as err:
        logger.warning("onboarding info extraction failed state=%s error=%s", current_state, err)
        return default


# ---------------------------------------------------------------------------
# Context file write helpers (calls user_profiles functions)
# ---------------------------------------------------------------------------

# Default AI names for named presets, used when user didn't give a custom name
_PRESET_DEFAULT_NAMES = {"chaochao": "朝朝", "xixi": "夕夕", "ju": "橘"}


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
    if user_name and current_state in {ONBOARDING_PENDING, ONBOARDING_STEP1_SENT}:
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
    if persona and current_state == ONBOARDING_STEP3_SENT:
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

    return written


def _resolve_persona_preset(raw: str) -> str:
    """Map raw persona value to canonical preset key."""
    mapping = {
        "blank": "blank",
        "chaochao": "chaochao",
        "xixi": "xixi",
        "ju": "ju",
        "custom": "blank",  # custom description goes into blank template, enriched separately
    }
    return mapping.get(raw, "blank")


# ---------------------------------------------------------------------------
# State advancement logic
# ---------------------------------------------------------------------------

def next_onboarding_state(
    *,
    current_state: str,
    extracted: dict,
    user_name_ask_count: int,
    persona_ask_count: int,
) -> str:
    """Determine next onboarding state after a turn.

    The caller is responsible for checking if the transition is appropriate.
    Returns the next state string.
    """
    skip = extracted.get("skip", False)

    if current_state == ONBOARDING_PENDING:
        return ONBOARDING_STEP1_SENT

    if current_state == ONBOARDING_STEP1_SENT:
        # Got user's reply to step 1 (asked user name)
        # Advance to step2 regardless of whether we extracted a name
        return ONBOARDING_STEP2_SENT

    if current_state == ONBOARDING_STEP2_SENT:
        # Got user's reply to step 2 (asked AI name)
        # Always advance to step3
        return ONBOARDING_STEP3_SENT

    if current_state == ONBOARDING_STEP3_SENT:
        # Got user's reply to step 3 (asked persona)
        # Complete onboarding
        return ONBOARDING_COMPLETE

    return current_state


def check_onboarding_timeout(*, last_updated_at: Optional[str]) -> bool:
    """Return True if the onboarding has been idle past the timeout threshold."""
    if not last_updated_at:
        return False
    try:
        updated = datetime.fromisoformat(last_updated_at)
        idle_minutes = (datetime.now() - updated).total_seconds() / 60
        return idle_minutes >= ONBOARDING_TIMEOUT_MINUTES
    except Exception:
        return False
