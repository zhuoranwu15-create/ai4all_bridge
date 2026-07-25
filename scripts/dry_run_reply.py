"""只读 dry-run:用某账号真实上下文 + 一条测试文本,实际让 LLM 产出一条回复。

安全保证:只读 DB(account/session/profile/历史),不写 messages、不扣费、
不调用 OpenClaw 网关(不会发到用户微信)。唯一外部副作用是一次 LLM API 调用。

    PYTHONPATH=. .venv/bin/python scripts/dry_run_reply.py aid_806382741 "在吗"
"""

import sys

from app.config import settings
from app.db import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    get_account,
    get_account_onboarding_state,
    get_profile_for_account,
    list_sessions_for_account,
)
from app.agent_runtime.llm.service import generate_completion, get_active_llm_model
from app.onboarding import is_onboarding_active
from app.time_utils import beijing_now
from app.turn_service import build_turn_llm_input


def _active_session(account_id: str):
    sessions = list_sessions_for_account(account_id=account_id, limit=20)
    if not sessions:
        return None
    active = [
        s for s in sessions
        if s.get("session_key") == ACCOUNT_ACTIVE_SESSION_KEY and s.get("status") == "active"
    ]
    return active[0] if active else sessions[0]


def main() -> int:
    account_id = sys.argv[1] if len(sys.argv) > 1 else "aid_806382741"
    user_text = sys.argv[2] if len(sys.argv) > 2 else "在吗"

    account = get_account(account_id=account_id)
    if account is None:
        print(f"账号不存在: {account_id}")
        return 2
    session = _active_session(account_id)
    if session is None:
        print(f"账号无 session: {account_id}")
        return 2

    profile = get_profile_for_account(account_id=account_id) or {}
    now = beijing_now()
    today = now.date().isoformat()
    onboarding_state = get_account_onboarding_state(account_id=account_id)

    llm_input = build_turn_llm_input(
        account_id=account_id,
        account=account,
        session=session,
        profile=profile,
        text=user_text,
        today=today,
        current_time=now.strftime("%H:%M"),
        onboarding_state=onboarding_state,
        onboarding_active=is_onboarding_active(onboarding_state),
        web_search_enabled=bool(getattr(settings, "web_search_enabled", False)),
        include_tool_instructions=True,
        debug_dry_run=True,  # 把 user_text 拼进 messages,且无持久化
    )
    messages = llm_input["messages"]

    print(f"account_id   = {account_id}")
    print(f"session_id   = {session['id']}")
    print(f"历史条数      = {llm_input['metadata'].get('history_count')}")
    print(f"llm_model    = {get_active_llm_model()}")
    print(f"测试输入      = {user_text!r}")
    print("=" * 50)
    reply = generate_completion(messages)
    print("LLM 回复:")
    print(reply if reply else "(空 — 未配置 LLM_API_KEY 或模型返回空)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
