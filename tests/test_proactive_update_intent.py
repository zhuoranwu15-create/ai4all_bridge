"""P1-5：主动设置更新意图检测的单元测试。

`_infer_proactive_update_tool_choice` 从通用 LLM 入口移到 turn 域后，意图检测正/负例
在这里单测；通用入口只消费调用方传入的 first_round_tool_choice（见 test_llm_tools.py）。
"""
from app.turn_service import _infer_proactive_update_tool_choice

_FORCED = {"type": "function", "function": {"name": "update_proactive_message_settings"}}


def test_intent_positive_frequency():
    assert _infer_proactive_update_tool_choice("以后每天最多1条主动消息") == _FORCED


def test_intent_positive_mute():
    assert _infer_proactive_update_tool_choice("别再主动找我了") == _FORCED


def test_intent_negative_unrelated_more_send_phrase():
    # "多发点资料给同学" 与主动消息设置无关，不得强制工具。
    assert _infer_proactive_update_tool_choice("帮我多发点资料给同学") == "auto"


def test_intent_negative_plain_chat():
    assert _infer_proactive_update_tool_choice("今天天气真不错") == "auto"


def test_intent_empty():
    assert _infer_proactive_update_tool_choice("") == "auto"
    assert _infer_proactive_update_tool_choice(None) == "auto"
