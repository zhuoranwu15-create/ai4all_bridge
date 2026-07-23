"""Agent Runtime Output Parser。

将 LLM 原始字符串解析为结构化 dict。
不抛异常：任何解析失败都返回 fallback，保证接口稳定。
"""
import json
import logging
import re

logger = logging.getLogger("ai4all.agent_runtime.output_parser")

# 匹配 ```json ... ``` 代码块
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def parse(raw: str) -> dict:
    """解析 LLM 输出为 dict。

    策略（按序尝试）：
    1. 直接 json.loads
    2. 提取 ```json ``` 代码块后 json.loads
    3. 返回 fallback dict（不抛异常）

    Returns:
        解析成功：原始 dict
        解析失败：{"parse_error": True, "raw": raw, "reply": ""}
    """
    raw = (raw or "").strip()

    # 策略 1：直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 策略 2：提取代码块
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 策略 3：fallback — 把 LLM 原始文本直接作为 reply，避免前端因 reply="" 触发第二次 LLM 调用
    logger.warning("output_parser: failed to parse LLM output, returning fallback. raw=%r", raw[:200])
    return {
        "parse_error": True,
        "raw": raw,
        "reply": raw,   # 使用原始文本而非空字符串，防止前端回退到 legacy 链路
        "actions": [{"type": "none"}],
        "recommendations": [],
    }
