"""LLM 工具错误的稳定码与产品语言消息。"""
from __future__ import annotations

from typing import Any, Dict


def tool_error(ctx: Any, code: str, *, fallback: str, **params: Any) -> Dict[str, str]:
    """构造工具错误；支持产品注入本地化器，旧产品回落到既有文案。"""

    message = str(fallback)
    resolver = getattr(ctx, "localized_message", None)
    if callable(resolver):
        message = str(resolver(code, fallback=message, **params))
    return {"error_code": str(code), "error": message}


__all__ = ["tool_error"]
