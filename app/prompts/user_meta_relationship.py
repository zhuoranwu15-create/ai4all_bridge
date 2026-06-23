"""账号关系状态天级 LLM 评估的 prompt 与解析。

镜像 app/prompts/user_meta_companion_type.py。只评估三个字段：
relationship_stage(仅 acquainted→deep_bond 的升级判断)、agent_need_trust_status、
agent_need_growth_status；不评估 agent_need_survival_status（确定性维护）。
枚举合法值复用 app/db/user_meta.py 的 *_VALUES，避免口径漂移。
"""
import json
import re
from typing import Any, Dict, List, Optional

from app.db.user_meta import (
    GROWTH_STATUS_VALUES,
    RELATIONSHIP_STAGE_VALUES,
    TRUST_STATUS_VALUES,
)

# 各状态值的中文释义（仅用于 prompt 展示与当前值回显）。
_STAGE_LABELS = {
    "icebreaking": "破冰：刚接触，彼此陌生",
    "acquainted": "相识：有一定了解，互动稳定",
    "deep_bond": "挚友/热恋：高度信任、情感紧密",
}
_TRUST_LABELS = {
    "building": "建立中：信任尚在形成",
    "stable": "稳定：信任牢固",
    "damaged": "受损：出现不信任或被冒犯",
}
_GROWTH_LABELS = {
    "not_started": "未开始：尚无共同成长迹象",
    "emerging": "有苗头：开始出现成长性互动",
    "stable": "稳定发生：持续的共同成长",
}

RELATIONSHIP_EVAL_PROMPT_TEMPLATE = """你是一个关系状态分析器，根据用户最近向 AI 发送的消息和当前关系状态，判断三个字段的最新取值。

## 字段定义
relationship_stage（关系阶段）：
- icebreaking：破冰
- acquainted：相识
- deep_bond：挚友/热恋
agent_need_trust_status（信任与尊重）：
- building：建立中
- stable：稳定
- damaged：受损
agent_need_growth_status（共同成长）：
- not_started：未开始
- emerging：有苗头
- stable：稳定发生

## 当前状态
- 关系阶段：{stage}
- 信任与尊重：{trust}
- 共同成长：{growth}
- （生存/活跃状态由系统计算，无需你判断）

## 要求
- 只依据用户侧消息判断，不参考 AI 的回复
- relationship_stage：仅当当前为「相识(acquainted)」且关系已明显达到挚友/热恋深度时，才输出 deep_bond；否则原样输出当前阶段（不要从 icebreaking 越级，也不要降级）
- agent_need_trust_status / agent_need_growth_status：按上面定义给出最新取值
- 信号不足或无法判断的字段，原样返回当前值
- 只输出 JSON，不要任何额外文字

## 用户最近消息（共 {n} 条）
{messages}

输出严格 JSON：
{{"relationship_stage": "...", "agent_need_trust_status": "...", "agent_need_growth_status": "..."}}
"""


def _clean_message_content(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 200:
        return text[:200] + "..."
    return text


def _label(value: Optional[str], labels: Dict[str, str]) -> str:
    text = str(value or "").strip()
    return labels.get(text, text or "未知")


def build_relationship_eval_prompt(
    *, messages: List[Dict[str, Any]], current_state: Dict[str, Any]
) -> str:
    """接收入站消息列表与当前四状态，返回完整关系评估 prompt。"""
    lines = []
    index = 0
    for message in messages:
        content = _clean_message_content(message.get("content"))
        if not content:
            continue
        index += 1
        lines.append(f"{index}. {content}")
    return RELATIONSHIP_EVAL_PROMPT_TEMPLATE.format(
        stage=_label(current_state.get("relationship_stage"), _STAGE_LABELS),
        trust=_label(current_state.get("agent_need_trust_status"), _TRUST_LABELS),
        growth=_label(current_state.get("agent_need_growth_status"), _GROWTH_LABELS),
        n=index,
        messages="\n".join(lines) if lines else "（无）",
    )


def _extract_json_object(raw: str) -> Dict[str, Any]:
    """从 LLM 文本里抽出 JSON 对象；解析失败抛 ValueError。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM output is not JSON")
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM output must be a JSON object")
    return payload


def _normalize_enum(value: Any, allowed: set) -> Optional[str]:
    text = str(value or "").strip()
    return text if text in allowed else None


def parse_relationship_payload(raw: str) -> Dict[str, Optional[str]]:
    """解析 LLM 输出为三个关系字段；非法/缺失字段归一为 None（表示无意见）。

    仅当整体 JSON 无法解析时抛 ValueError；单字段非法不抛，交由 merge 保留当前值。
    """
    payload = _extract_json_object(raw)
    return {
        "relationship_stage": _normalize_enum(
            payload.get("relationship_stage"), RELATIONSHIP_STAGE_VALUES
        ),
        "agent_need_trust_status": _normalize_enum(
            payload.get("agent_need_trust_status"), TRUST_STATUS_VALUES
        ),
        "agent_need_growth_status": _normalize_enum(
            payload.get("agent_need_growth_status"), GROWTH_STATUS_VALUES
        ),
    }
