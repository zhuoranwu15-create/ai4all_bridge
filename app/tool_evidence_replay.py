"""工具证据跨 turn 回灌 (Batch C)

从 tool_invocations 表 read-side 重建最近 K 轮的工具调用 wire，
splice 进 history messages list，不改写 messages 表，不影响 dreaming/memory 消费方。
"""
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def inject_tool_evidence_replay(
    history: List[Dict[str, Any]],
    history_rows: List[Dict[str, Any]],
    account_id: str,
    *,
    enabled: bool = True,
    max_turns: int = 2,
    max_result_chars: int = 1500,
) -> List[Dict[str, Any]]:
    """在 history 里找最近 max_turns 个有 tool 调用的 user 消息，
    在对应位置 splice 进 assistant.tool_calls + role:tool wire 消息。

    Returns 扩充后的新列表（不修改原 history）。
    """
    if not enabled or not history or not history_rows:
        return history

    # 找最近 max_turns 个有 message_id 的 user 行（逆序取、去重）
    target_message_ids: List[str] = []
    for row in reversed(history_rows):
        if row.get("role") == "user" and row.get("message_id"):
            mid = row["message_id"]
            if mid not in target_message_ids:
                target_message_ids.append(mid)
            if len(target_message_ids) >= max_turns:
                break

    if not target_message_ids:
        return history

    # 查 DB
    try:
        from app.db.analytics import list_recent_tool_invocations_for_replay
        invocations = list_recent_tool_invocations_for_replay(
            account_id, message_ids=target_message_ids
        )
    except Exception:
        logger.warning("tool_evidence_replay: DB query failed", exc_info=True)
        return history

    if not invocations:
        return history

    # 按 message_id 分组，保持调用顺序（query 已 ORDER BY id ASC）
    by_mid: Dict[str, List[Dict[str, Any]]] = {}
    for inv in invocations:
        mid = inv.get("message_id") or ""
        if mid:
            by_mid.setdefault(mid, []).append(inv)

    if not by_mid:
        return history

    # 重建 history，在 user 消息后 splice tool wire
    result: List[Dict[str, Any]] = []
    for msg, row in zip(history, history_rows):
        result.append(msg)
        mid = row.get("message_id") or ""
        if msg.get("role") == "user" and mid in by_mid:
            for inv in by_mid[mid]:
                tool_call_id = inv.get("tool_call_id") or f"replay_{inv.get('id', 0)}"
                tool_name = inv.get("tool_name") or "unknown"
                args_json = json.dumps(inv.get("args") or {}, ensure_ascii=False)
                result_raw = json.dumps(inv.get("result") or {}, ensure_ascii=False)
                result_content = result_raw[:max_result_chars]
                if len(result_raw) > max_result_chars:
                    result_content += "…[截断]"

                result.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": args_json,
                        },
                    }],
                })
                result.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_content,
                })

    return result
