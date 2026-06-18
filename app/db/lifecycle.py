"""app.db.lifecycle — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
import json
import logging
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings
from app.db._core import (
    _tx,
    connect,
)
__all__ = [
    'reenable_proactive_after_rebind',
    'unbind_account_channel',
    'unbind_and_wipe_account',
    'wipe_account_data',
]
# ---------------------------------------------------------------------------
# Account unbind / wipe
# ---------------------------------------------------------------------------

def unbind_account_channel(
    *, account_id: str, conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """Path A: disconnect WeChat channel, cancel reminders & proactive.

    Removes channel routing rows so incoming messages can no longer reach this
    account.  All conversation history and context files are preserved.
    Returns counts of affected rows for audit logging.

    传入 conn 时复用调用方事务（供 unbind_and_wipe_account 单事务编排）。
    """
    with _tx(conn) as conn:
        cb = conn.execute(
            "DELETE FROM channel_bindings WHERE account_id = ?",
            (account_id,),
        ).rowcount
        bi = conn.execute(
            """
            UPDATE binding_intents
            SET status = 'revoked', updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE account_id = ? AND status = 'completed'
            """,
            (account_id,),
        ).rowcount
        rem = conn.execute(
            """
            UPDATE reminders
            SET status = 'cancelled',
                cancelled_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE account_id = ? AND status = 'pending'
            """,
            (account_id,),
        ).rowcount
        pc = conn.execute(
            """
            DELETE FROM proactive_commitments
            WHERE account_id = ? AND status IN ('pending', 'scheduled')
            """,
            (account_id,),
        ).rowcount
        ci = conn.execute(
            """
            UPDATE content_invitations
            SET status = 'cancelled',
                policy_reason = 'account_unbound',
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE account_id = ?
              AND status IN ('candidate', 'sending', 'invited', 'accepted')
            """,
            (account_id,),
        ).rowcount
        conn.execute(
            """
            UPDATE proactive_account_state
            SET enabled = 0, updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE account_id = ?
            """,
            (account_id,),
        )
    return {
        "channel_bindings_deleted": cb,
        "binding_intents_revoked": bi,
        "reminders_cancelled": rem,
        "proactive_commitments_deleted": pc,
        "content_invitations_cancelled": ci,
    }


def wipe_account_data(
    *, account_id: str, conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """Path B: hard-delete all account data after unbind_account_channel().

    Removes sessions, messages, dreaming data, profile row, and the
    account_owner_binding.  Sets account status to 'deactivated'.
    Does NOT touch the filesystem — caller must remove user_profiles dir.

    传入 conn 时复用调用方事务（供 unbind_and_wipe_account 单事务编排）。
    """
    from app.db.moderation import _delete_content_moderation_tasks_where
    with _tx(conn) as conn:
        memory_events = conn.execute(
            "DELETE FROM memory_events WHERE account_id = ?",
            (account_id,),
        ).rowcount
        dmi = conn.execute(
            """
            DELETE FROM dreaming_memory_items
            WHERE dreaming_run_id IN (SELECT id FROM dreaming_runs WHERE account_id = ?)
               OR account_id = ?
            """,
            (account_id, account_id),
        ).rowcount
        dr = conn.execute(
            "DELETE FROM dreaming_runs WHERE account_id = ?",
            (account_id,),
        ).rowcount
        reminders = conn.execute(
            "DELETE FROM reminders WHERE account_id = ?",
            (account_id,),
        ).rowcount
        proactive_commitments = conn.execute(
            "DELETE FROM proactive_commitments WHERE account_id = ?",
            (account_id,),
        ).rowcount
        content_invitations = conn.execute(
            "DELETE FROM content_invitations WHERE account_id = ?",
            (account_id,),
        ).rowcount
        content_invitation_preferences = conn.execute(
            "DELETE FROM content_invitation_preferences WHERE account_id = ?",
            (account_id,),
        ).rowcount
        moderation_deleted = _delete_content_moderation_tasks_where(
            conn,
            "account_id = ?",
            (account_id,),
        )
        moderation_risk_state = conn.execute(
            "DELETE FROM moderation_account_risk_state WHERE account_id = ?",
            (account_id,),
        ).rowcount
        account_user_meta_daily = conn.execute(
            "DELETE FROM account_user_meta_daily WHERE account_id = ?",
            (account_id,),
        ).rowcount
        account_user_meta = conn.execute(
            "DELETE FROM account_user_meta WHERE account_id = ?",
            (account_id,),
        ).rowcount
        outbound = conn.execute(
            "DELETE FROM outbound_messages WHERE account_id = ?",
            (account_id,),
        ).rowcount
        cost_events = conn.execute(
            "DELETE FROM cost_events WHERE account_id = ?",
            (account_id,),
        ).rowcount
        ledger = conn.execute(
            "DELETE FROM entitlement_ledger WHERE account_id = ?",
            (account_id,),
        ).rowcount
        wallets = conn.execute(
            "DELETE FROM entitlement_wallets WHERE account_id = ?",
            (account_id,),
        ).rowcount
        daily_usage = conn.execute(
            "DELETE FROM daily_usage WHERE account_id = ?",
            (account_id,),
        ).rowcount
        debug_traces = conn.execute(
            "DELETE FROM debug_traces WHERE account_id = ?",
            (account_id,),
        ).rowcount
        proactive_account_state = conn.execute(
            "DELETE FROM proactive_account_state WHERE account_id = ?",
            (account_id,),
        ).rowcount
        # tool_invocations 是 sessions 的 NO ACTION 子表，必须先删，否则 DELETE sessions
        # 触发 FOREIGN KEY constraint failed。search_provider_runs 又是 tool_invocations
        # 的子表，需更早删（content_invitations 同样引用 tool_invocations，已在上方删除）。
        search_provider_runs = conn.execute(
            "DELETE FROM search_provider_runs WHERE account_id = ?",
            (account_id,),
        ).rowcount
        analytics_events = conn.execute(
            "DELETE FROM analytics_events WHERE account_id = ?",
            (account_id,),
        ).rowcount
        tool_invocations = conn.execute(
            "DELETE FROM tool_invocations WHERE account_id = ?",
            (account_id,),
        ).rowcount
        msgs = conn.execute(
            "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE account_id = ?)",
            (account_id,),
        ).rowcount
        sess = conn.execute(
            "DELETE FROM sessions WHERE account_id = ?",
            (account_id,),
        ).rowcount
        binding_intents = conn.execute(
            "DELETE FROM binding_intents WHERE account_id = ?",
            (account_id,),
        ).rowcount
        profiles = conn.execute(
            "DELETE FROM profiles WHERE account_id = ?",
            (account_id,),
        ).rowcount
        account_owner_bindings = conn.execute(
            "DELETE FROM account_owner_bindings WHERE account_id = ?",
            (account_id,),
        ).rowcount
        conn.execute(
            """
            UPDATE accounts
            SET status = 'deactivated', updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (account_id,),
        )
    return {
        "messages_deleted": msgs,
        "sessions_deleted": sess,
        "dreaming_memory_items_deleted": dmi,
        "dreaming_runs_deleted": dr,
        "memory_events_deleted": memory_events,
        "reminders_deleted": reminders,
        "proactive_commitments_deleted": proactive_commitments,
        "content_invitations_deleted": content_invitations,
        "content_invitation_preferences_deleted": content_invitation_preferences,
        "moderation_tasks_deleted": moderation_deleted["moderation_tasks_deleted"],
        "moderation_results_deleted": moderation_deleted["moderation_results_deleted"],
        "moderation_actions_deleted": moderation_deleted["moderation_actions_deleted"],
        "moderation_exports_deleted": moderation_deleted["moderation_exports_deleted"],
        "moderation_risk_state_deleted": moderation_risk_state,
        "account_user_meta_deleted": account_user_meta,
        "account_user_meta_daily_deleted": account_user_meta_daily,
        "cost_events_deleted": cost_events,
        "entitlement_ledger_deleted": ledger,
        "entitlement_wallets_deleted": wallets,
        "outbound_messages_deleted": outbound,
        "daily_usage_deleted": daily_usage,
        "debug_traces_deleted": debug_traces,
        "tool_invocations_deleted": tool_invocations,
        "search_provider_runs_deleted": search_provider_runs,
        "analytics_events_deleted": analytics_events,
        "binding_intents_deleted": binding_intents,
        "profiles_deleted": profiles,
        "proactive_account_state_deleted": proactive_account_state,
        "account_owner_bindings_deleted": account_owner_bindings,
    }


def unbind_and_wipe_account(*, account_id: str) -> Dict[str, Any]:
    """单事务完成 unbind + wipe（清空记忆解绑）。

    两步在同一连接/事务里执行：要么全部提交，要么整体回滚，杜绝「channel 已
    解绑但记忆未清、账号未 deactivate」的半成品状态（旧实现两次独立提交，wipe
    失败会留下半成品并 500）。文件系统清理（profile 目录）需调用方在提交后单独
    处理，因为它不在 DB 事务范围内。
    """
    with connect() as conn:
        stats = unbind_account_channel(account_id=account_id, conn=conn)
        stats.update(wipe_account_data(account_id=account_id, conn=conn))
    return stats


def reenable_proactive_after_rebind(*, account_id: str) -> None:
    """Re-enable proactive state when user successfully re-binds WeChat."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_account_state
            SET enabled = 1, updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE account_id = ?
            """,
            (account_id,),
        )
