"""app.db.billing — 由 app/db.py 按域拆分而来（机械搬运，逻辑不变）。"""
import json
import logging
import math
import re
from app.db._backend import Connection, IntegrityError, Row
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings
from app.db._core import (
    ACCOUNT_ACTIVE_SESSION_KEY,
    MODERATION_BLOCKED_ERROR,
    NEW_USER_GRANT_SHELLS,
    NEW_USER_GRANT_SHELL_MICROS,
    REFERRAL_QUALIFYING_MESSAGE_COUNT,
    REFERRAL_REWARD_SHELLS,
    REFERRAL_REWARD_SHELL_MICROS,
    REFERRAL_SOFT_REVIEW_REGISTRATION_LIMIT,
    REFERRAL_SOFT_REVIEW_REWARD_DELAY_DAYS,
    REFERRAL_SOFT_REVIEW_WINDOW_DAYS,
    SHELL_BILLABLE_TOKENS_PER_SHELL,
    SHELL_MICROS_PER_SHELL,
    _ACCOUNT_ID_GENERATION_RETRIES,
    _NON_CONTEXT_ASSISTANT_REPLY,
    _REFERRAL_CODE_GENERATION_RETRIES,
    _channel_account_id_aliases,
    _clean_default_account_display_name,
    _clean_text,
    _format_shell_amount,
    _new_account_id,
    _new_id,
    _savepoint,
    _new_referral_code,
    _normalize_phone,
    _normalize_referral_code,
    _settings,
    _tx,
    connect,
    logger,
)
__all__ = [
    'create_ai4all_account_for_user',
    'create_binding_intent',
    'create_or_get_platform_user_by_phone',
    'ensure_wallet',
    'get_account_owner_binding',
    'get_active_binding_intent_for_channel_account',
    'get_binding_intent',
    'get_completed_binding_intent_for_openclaw_login_session_key',
    'get_first_active_account_for_user',
    'get_latest_subscription_for_user',
    'get_or_create_account_active_session',
    'get_or_create_default_ai4all_account_for_user',
    'get_or_create_personal_referral_code_for_user',
    'get_or_create_session',
    'get_platform_user',
    'get_platform_user_by_phone',
    'get_platform_user_id_for_account',
    'get_wallet_balance_shell_micros',
    'get_wallet_summary',
    'grant_new_user_shells',
    'grant_shells',
    'increment_session_turn_count',
    'list_account_owner_bindings_for_account',
    'list_binding_intents_for_account',
    'list_referral_relationships',
    'list_wallet_ledger',
    'mark_referral_relationship_bound',
    'preview_referral_code',
    'process_referral_message_for_account',
    'record_chat_usage_charge',
    'record_image_understanding_charge',
    'register_platform_user_with_referral',
    'release_due_referral_rewards',
    'release_due_referral_rewards_for_user',
    'resolve_account_id_for_inbound_channel_identity',
    'retry_qualified_referral_rewards_for_user',
    'set_binding_intent_error',
    'update_binding_intent',
    'upsert_subscription_for_user',
    'validate_referral_code',
]
# ---------------------------------------------------------------------------
# Web onboarding
# ---------------------------------------------------------------------------

def create_or_get_platform_user_by_phone(
    *,
    phone: str,
    display_name: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_phone = _normalize_phone(phone)
    cleaned_display_name = _clean_text(display_name)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(phone) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, platform_users.display_name),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (_new_id("user"), normalized_phone, cleaned_display_name),
        )
        row = conn.execute(
            """
            SELECT id, phone, display_name, status, created_at, updated_at
            FROM platform_users
            WHERE phone = ?
            """,
            (normalized_phone,),
        ).fetchone()
    return dict(row)


def get_platform_user(*, platform_user_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, phone, display_name, status, created_at, updated_at
            FROM platform_users
            WHERE id = ?
            """,
            (platform_user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_platform_user_by_phone(*, phone: str) -> Optional[Dict[str, Any]]:
    """Return a platform user by normalized phone without creating a new user."""
    normalized_phone = _normalize_phone(phone)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, phone, display_name, status, created_at, updated_at
            FROM platform_users
            WHERE phone = ?
            """,
            (normalized_phone,),
        ).fetchone()
    return dict(row) if row else None


def _decode_referral_code_row(row: Row) -> Dict[str, Any]:
    item = dict(row)
    item["used_count"] = int(item.get("used_count") or 0)
    if item.get("max_uses") is not None:
        item["max_uses"] = int(item["max_uses"])
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _decode_referral_relationship_row(row: Row) -> Dict[str, Any]:
    item = dict(row)
    item["meaningful_message_count"] = int(item.get("meaningful_message_count") or 0)
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _decode_meaningful_message_review_row(row: Row) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["message_ids"] = json.loads(item.pop("message_ids_json") or "[]")
    except json.JSONDecodeError:
        item["message_ids"] = []
        item["message_ids_decode_error"] = True
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _referral_code_unavailable_reason(row: Optional[Row]) -> str:
    if row is None:
        return "invalid"
    if row["status"] != "active":
        return "disabled"
    if row["max_uses"] is not None and int(row["used_count"] or 0) >= int(row["max_uses"]):
        return "max_uses_exceeded"
    return "invalid"


def _get_usable_referral_code_in_conn(
    conn: Connection,
    *,
    code: str,
) -> Optional[Row]:
    return conn.execute(
        """
        SELECT *
        FROM referral_codes
        WHERE code = ?
          AND status = 'active'
          AND (expires_at IS NULL OR expires_at > datetime('now', '+8 hours'))
          AND (max_uses IS NULL OR used_count < max_uses)
        """,
        (code,),
    ).fetchone()


def get_or_create_personal_referral_code_for_user(
    *,
    platform_user_id: str,
) -> Dict[str, Any]:
    """Return the stable personal invite code for a platform user, creating it if needed."""
    with connect() as conn:
        user = conn.execute(
            "SELECT id FROM platform_users WHERE id = ?",
            (platform_user_id,),
        ).fetchone()
        if user is None:
            raise ValueError("platform_user not found")
        existing = conn.execute(
            """
            SELECT *
            FROM referral_codes
            WHERE platform_user_id = ?
              AND code_type = 'personal'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
        if existing is not None:
            return _decode_referral_code_row(existing)

        last_integrity_error = None
        for _ in range(_REFERRAL_CODE_GENERATION_RETRIES):
            try:
                # 保存点隔离唯一冲突：PG 下冲突会中止整笔事务，需回滚到保存点才能继续重试/查询
                with _savepoint(conn, "refcode_ins"):
                    conn.execute(
                        """
                        INSERT INTO referral_codes(
                            id, platform_user_id, code, code_type, status, updated_at
                        )
                        VALUES (?, ?, ?, 'personal', 'active', strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                        """,
                        (_new_id("refcode"), platform_user_id, _new_referral_code()),
                    )
                break
            except IntegrityError as err:
                existing = conn.execute(
                    """
                    SELECT *
                    FROM referral_codes
                    WHERE platform_user_id = ?
                      AND code_type = 'personal'
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (platform_user_id,),
                ).fetchone()
                if existing is not None:
                    return _decode_referral_code_row(existing)
                last_integrity_error = err
        else:
            raise RuntimeError("failed to generate a unique referral code") from last_integrity_error

        row = conn.execute(
            """
            SELECT *
            FROM referral_codes
            WHERE platform_user_id = ?
              AND code_type = 'personal'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("referral_code was not created")
    return _decode_referral_code_row(row)


def validate_referral_code(*, code: Optional[str]) -> Dict[str, Any]:
    """Validate an invite code for registration without consuming its use count."""
    normalized = _normalize_referral_code(code)
    if not normalized:
        return {"valid": False, "reason": "missing", "code": None}
    with connect() as conn:
        any_row = conn.execute(
            "SELECT * FROM referral_codes WHERE code = ?",
            (normalized,),
        ).fetchone()
        usable_row = _get_usable_referral_code_in_conn(conn, code=normalized)
        if usable_row is None:
            if any_row is not None and any_row["status"] != "active":
                reason = "disabled"
            elif any_row is not None and any_row["expires_at"]:
                expired = conn.execute(
                    "SELECT ? <= datetime('now', '+8 hours') AS expired",
                    (any_row["expires_at"],),
                ).fetchone()["expired"]
                reason = "expired" if expired else _referral_code_unavailable_reason(any_row)
            elif (
                any_row is not None
                and any_row["max_uses"] is not None
                and int(any_row["used_count"] or 0) >= int(any_row["max_uses"])
            ):
                reason = "max_uses_exceeded"
            else:
                reason = "invalid"
            return {"valid": False, "reason": reason, "code": normalized}
    code_row = _decode_referral_code_row(usable_row)
    return {
        "valid": True,
        "reason": None,
        "code": normalized,
        "referral_code": code_row,
    }


def preview_referral_code(*, code: Optional[str]) -> Dict[str, Any]:
    """Return a public, redacted preview of an invite code for the web onboarding page."""
    validation = validate_referral_code(code=code)
    if not validation.get("valid"):
        return validation
    code_row = validation["referral_code"]
    inviter_name = None
    if code_row.get("platform_user_id"):
        with connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(pu.display_name, ''), NULLIF(a.display_name, '')) AS display_name
                FROM platform_users pu
                LEFT JOIN account_owner_bindings b
                  ON b.platform_user_id = pu.id AND b.status = 'active'
                LEFT JOIN accounts a ON a.id = b.account_id
                WHERE pu.id = ?
                ORDER BY b.created_at ASC, b.id ASC
                LIMIT 1
                """,
                (code_row["platform_user_id"],),
            ).fetchone()
            inviter_name = row["display_name"] if row else None
    return {
        "valid": True,
        "code": code_row["code"],
        "code_type": code_row["code_type"],
        "inviter_display_name": inviter_name,
    }


def register_platform_user_with_referral(
    *,
    phone: str,
    display_name: Optional[str] = None,
    invite_code: Optional[str] = None,
    verified_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a platform user, creating a referral relationship for new users only.

    When verified_token is provided, OTP consumption, user creation, referral
    creation, and invite-code used_count consumption share one transaction.
    """
    normalized_phone = _normalize_phone(phone)
    cleaned_display_name = _clean_text(display_name)
    normalized_code = _normalize_referral_code(invite_code)
    with connect() as conn:
        existing = conn.execute(
            """
            SELECT id, phone, display_name, status, created_at, updated_at
            FROM platform_users
            WHERE phone = ?
            """,
            (normalized_phone,),
        ).fetchone()

        code_row = None
        if existing is None and normalized_code:
            code_row = _get_usable_referral_code_in_conn(conn, code=normalized_code)
            if code_row is None:
                raise ValueError("invalid_invite_code")

        if verified_token is not None:
            cursor = conn.execute(
                """
                UPDATE phone_verifications
                SET token_consumed_at = datetime('now', '+8 hours')
                WHERE verified_token = ?
                  AND phone = ?
                  AND token_consumed_at IS NULL
                  AND token_expires_at > datetime('now', '+8 hours')
                """,
                (verified_token, normalized_phone),
            )
            # rowcount 替代 SQLite 专有 changes()（两后端一致）
            if cursor.rowcount == 0:
                raise ValueError("invalid_otp_token")

        if existing is not None:
            conn.execute(
                """
                UPDATE platform_users
                SET display_name = COALESCE(?, display_name),
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                """,
                (cleaned_display_name, existing["id"]),
            )
            user_row = conn.execute(
                """
                SELECT id, phone, display_name, status, created_at, updated_at
                FROM platform_users
                WHERE id = ?
                """,
                (existing["id"],),
            ).fetchone()
            return {
                "platform_user": dict(user_row),
                "is_new_user": False,
                "referral_relationship": None,
            }

        platform_user_id = _new_id("user")
        conn.execute(
            """
            INSERT INTO platform_users(id, phone, display_name, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (platform_user_id, normalized_phone, cleaned_display_name),
        )

        relationship_row = None
        if code_row is not None:
            if code_row["platform_user_id"]:
                inviter_platform_user_id = code_row["platform_user_id"]
                if inviter_platform_user_id == platform_user_id:
                    raise ValueError("cannot_self_invite")
                relationship_id = _new_id("refrel")
                conn.execute(
                    """
                    INSERT INTO referral_relationships(
                        id, inviter_platform_user_id, invitee_platform_user_id,
                        referral_code_id, status, review_status, metadata_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'registered', 'pending', ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                    """,
                    (
                        relationship_id,
                        inviter_platform_user_id,
                        platform_user_id,
                        code_row["id"],
                        json.dumps(
                            {
                                "invite_code": normalized_code,
                                "registration_phone_last4": normalized_phone[-4:],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                _mark_referral_soft_review_if_needed_in_conn(
                    conn,
                    relationship_id=relationship_id,
                    inviter_platform_user_id=inviter_platform_user_id,
                )
            cursor = conn.execute(
                """
                UPDATE referral_codes
                SET used_count = used_count + 1,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at > datetime('now', '+8 hours'))
                  AND (max_uses IS NULL OR used_count < max_uses)
                """,
                (code_row["id"],),
            )
            # rowcount 替代 SQLite 专有 changes()（两后端一致）
            if cursor.rowcount == 0:
                raise ValueError("invalid_invite_code")
            if code_row["platform_user_id"]:
                relationship_row = conn.execute(
                    "SELECT * FROM referral_relationships WHERE id = ?",
                    (relationship_id,),
                ).fetchone()

        user_row = conn.execute(
            """
            SELECT id, phone, display_name, status, created_at, updated_at
            FROM platform_users
            WHERE id = ?
            """,
            (platform_user_id,),
        ).fetchone()
    return {
        "platform_user": dict(user_row),
        "is_new_user": True,
        "referral_relationship": (
            _decode_referral_relationship_row(relationship_row)
            if relationship_row is not None
            else None
        ),
    }


def upsert_subscription_for_user(
    *,
    platform_user_id: str,
    plan: str = "free",
    status: str = "active",
) -> Dict[str, Any]:
    cleaned_plan = _clean_text(plan) or "free"
    cleaned_status = _clean_text(status) or "active"
    with connect() as conn:
        user = conn.execute(
            "SELECT id FROM platform_users WHERE id = ?",
            (platform_user_id,),
        ).fetchone()
        if user is None:
            raise ValueError("platform_user not found")
        existing = conn.execute(
            """
            SELECT id FROM subscriptions
            WHERE platform_user_id = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
        if existing:
            subscription_id = existing["id"]
            conn.execute(
                """
                UPDATE subscriptions
                SET plan = ?, status = ?, updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                """,
                (cleaned_plan, cleaned_status, subscription_id),
            )
        else:
            subscription_id = _new_id("sub")
            conn.execute(
                """
                INSERT INTO subscriptions(id, platform_user_id, plan, status, updated_at)
                VALUES (?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                """,
                (subscription_id, platform_user_id, cleaned_plan, cleaned_status),
            )
        row = conn.execute(
            """
            SELECT id, platform_user_id, plan, status, created_at, updated_at
            FROM subscriptions
            WHERE id = ?
            """,
            (subscription_id,),
        ).fetchone()
    return dict(row)


def get_latest_subscription_for_user(
    *,
    platform_user_id: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, platform_user_id, plan, status, created_at, updated_at
            FROM subscriptions
            WHERE platform_user_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
    return dict(row) if row else None


def _decode_wallet_row(row: Row) -> Dict[str, Any]:
    item = dict(row)
    balance_shell_micros = int(item["balance_shell_micros"])
    item["balance_shell_micros"] = balance_shell_micros
    item["balance_shells"] = _format_shell_amount(balance_shell_micros)
    return item


def _decode_ledger_row(row: Row) -> Dict[str, Any]:
    item = dict(row)
    amount_shell_micros = int(item["amount_shell_micros"])
    balance_after_shell_micros = int(item["balance_after_shell_micros"])
    item["amount_shell_micros"] = amount_shell_micros
    item["amount_shells"] = _format_shell_amount(amount_shell_micros)
    item["balance_after_shell_micros"] = balance_after_shell_micros
    item["balance_after_shells"] = _format_shell_amount(balance_after_shell_micros)
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _decode_cost_event_row(row: Row) -> Dict[str, Any]:
    item = dict(row)
    item["billable_to_user"] = bool(item["billable_to_user"])
    item["computed_shell_micros"] = int(item["computed_shell_micros"] or 0)
    item["computed_shells"] = _format_shell_amount(item["computed_shell_micros"])
    item["model_price_multiplier"] = (
        int(item["model_price_multiplier_micros"] or 0) / 1_000_000
    )
    try:
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
        item["metadata_decode_error"] = True
    return item


def _ensure_wallet_in_conn(
    conn: Connection,
    *,
    account_id: str,
    platform_user_id: str,
) -> Row:
    account = conn.execute(
        "SELECT id FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if account is None:
        raise ValueError("account not found")
    user = conn.execute(
        "SELECT id FROM platform_users WHERE id = ?",
        (platform_user_id,),
    ).fetchone()
    if user is None:
        raise ValueError("platform_user not found")
    conn.execute(
        """
        INSERT INTO entitlement_wallets(
            id, account_id, platform_user_id, balance_shell_micros, status, updated_at
        )
        VALUES (?, ?, ?, 0, 'active', strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
        ON CONFLICT(account_id) DO NOTHING
        """,
        (_new_id("wallet"), account_id, platform_user_id),
    )
    row = conn.execute(
        """
        SELECT id, account_id, platform_user_id, balance_shell_micros, status,
               created_at, updated_at
        FROM entitlement_wallets
        WHERE account_id = ?
        """,
        (account_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("entitlement_wallet was not created")
    if row["platform_user_id"] != platform_user_id:
        logger.warning(
            "wallet platform_user_id mismatch account=%s wallet_owner=%s caller=%s — using existing wallet",
            account_id,
            row["platform_user_id"],
            platform_user_id,
        )
    return row


def ensure_wallet(
    *,
    account_id: str,
    platform_user_id: str,
) -> Dict[str, Any]:
    with connect() as conn:
        row = _ensure_wallet_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
    return _decode_wallet_row(row)


def _apply_wallet_ledger_in_conn(
    conn: Connection,
    *,
    account_id: str,
    platform_user_id: str,
    amount_shell_micros: int,
    entry_type: str,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Row:
    if amount_shell_micros == 0:
        raise ValueError("amount_shell_micros must not be zero")
    cleaned_entry_type = _clean_text(entry_type)
    cleaned_source_type = _clean_text(source_type)
    cleaned_idempotency_key = _clean_text(idempotency_key)
    if cleaned_entry_type not in {"credit", "debit"}:
        raise ValueError("entry_type must be credit or debit")
    if not cleaned_source_type:
        raise ValueError("source_type is required")
    if not cleaned_idempotency_key:
        raise ValueError("idempotency_key is required")

    existing = conn.execute(
        """
        SELECT *
        FROM entitlement_ledger
        WHERE idempotency_key = ?
        """,
        (cleaned_idempotency_key,),
    ).fetchone()
    if existing is not None:
        return existing

    wallet = _ensure_wallet_in_conn(
        conn,
        account_id=account_id,
        platform_user_id=platform_user_id,
    )
    ledger_id = _new_id("ledger")
    # Atomic increment: avoids TOCTOU race where two concurrent transactions
    # both read the same balance and overwrite each other's update.
    conn.execute(
        """
        UPDATE entitlement_wallets
        SET balance_shell_micros = balance_shell_micros + ?, updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        WHERE id = ?
        """,
        (int(amount_shell_micros), wallet["id"]),
    )
    balance_row = conn.execute(
        "SELECT balance_shell_micros FROM entitlement_wallets WHERE id = ?",
        (wallet["id"],),
    ).fetchone()
    balance_after = int(balance_row["balance_shell_micros"])
    conn.execute(
        """
        INSERT INTO entitlement_ledger(
            id, wallet_id, account_id, platform_user_id, entry_type,
            source_type, source_id, amount_shell_micros,
            balance_after_shell_micros, idempotency_key, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ledger_id,
            wallet["id"],
            account_id,
            platform_user_id,
            cleaned_entry_type,
            cleaned_source_type,
            _clean_text(source_id),
            int(amount_shell_micros),
            balance_after,
            cleaned_idempotency_key,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    row = conn.execute(
        "SELECT * FROM entitlement_ledger WHERE id = ?",
        (ledger_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("entitlement_ledger was not created")
    return row


def grant_shells(
    *,
    account_id: str,
    platform_user_id: str,
    amount_shell_micros: int,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if amount_shell_micros <= 0:
        raise ValueError("amount_shell_micros must be positive")
    with connect() as conn:
        row = _apply_wallet_ledger_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
            amount_shell_micros=int(amount_shell_micros),
            entry_type="credit",
            source_type=source_type,
            source_id=source_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
    return _decode_ledger_row(row)


def grant_new_user_shells(
    *,
    account_id: str,
    platform_user_id: str,
) -> Dict[str, Any]:
    return grant_shells(
        account_id=account_id,
        platform_user_id=platform_user_id,
        amount_shell_micros=NEW_USER_GRANT_SHELL_MICROS,
        source_type="new_user_grant",
        source_id=account_id,
        idempotency_key=f"new-user-grant-{account_id}",
        metadata={"grant_shells": NEW_USER_GRANT_SHELLS},
    )


def _load_referral_metadata(row: Row) -> Dict[str, Any]:
    try:
        return json.loads(row["metadata_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def _beijing_timestamp_in_conn(
    conn: Connection,
    *,
    modifier: Optional[str] = None,
) -> str:
    modifiers = ["'+8 hours'"]
    if modifier:
        modifiers.append("?")
    sql = (
        "SELECT strftime('%Y-%m-%d %H:%M:%S', "
        f"datetime('now', {', '.join(modifiers)}))"
    )
    params = (modifier,) if modifier else ()
    return str(conn.execute(sql, params).fetchone()[0])


def _mark_referral_soft_review_if_needed_in_conn(
    conn: Connection,
    *,
    relationship_id: str,
    inviter_platform_user_id: str,
) -> None:
    recent_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM referral_relationships
            WHERE inviter_platform_user_id = ?
              AND status IN ('registered', 'bound', 'qualified', 'rewarded')
              AND review_status != 'failed'
              AND created_at >= datetime('now', '+8 hours', ?)
            """,
            (
                inviter_platform_user_id,
                f"-{REFERRAL_SOFT_REVIEW_WINDOW_DAYS} days",
            ),
        ).fetchone()[0]
    )
    if recent_count <= REFERRAL_SOFT_REVIEW_REGISTRATION_LIMIT:
        return

    row = conn.execute(
        "SELECT * FROM referral_relationships WHERE id = ?",
        (relationship_id,),
    ).fetchone()
    if row is None:
        return
    metadata = _load_referral_metadata(row)
    metadata.update(
        {
            "soft_review_required": True,
            "soft_review_reason": "inviter_recent_registration_limit",
            "soft_review_window_days": REFERRAL_SOFT_REVIEW_WINDOW_DAYS,
            "soft_review_registration_limit": REFERRAL_SOFT_REVIEW_REGISTRATION_LIMIT,
            "inviter_recent_registration_count": recent_count,
        }
    )
    conn.execute(
        """
        UPDATE referral_relationships
        SET metadata_json = ?,
            updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        WHERE id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False), relationship_id),
    )


def _ensure_referral_soft_review_hold_in_conn(
    conn: Connection,
    *,
    relationship: Row,
    invitee_platform_user_id: str,
    account_id: str,
    candidate_ids: List[int],
    review_row: Optional[Row],
) -> Row:
    if relationship["review_status"] != "pending":
        return relationship

    metadata = _load_referral_metadata(relationship)
    now = _beijing_timestamp_in_conn(conn)
    metadata.setdefault("qualified_at", now)
    metadata.setdefault(
        "reward_release_after",
        _beijing_timestamp_in_conn(
            conn,
            modifier=f"+{REFERRAL_SOFT_REVIEW_REWARD_DELAY_DAYS} days",
        ),
    )
    metadata["reward_delay_days"] = REFERRAL_SOFT_REVIEW_REWARD_DELAY_DAYS
    metadata["reward_pending_reason"] = "soft_review_delayed_release"
    conn.execute(
        """
        UPDATE referral_relationships
        SET status = 'qualified',
            review_status = 'pending',
            metadata_json = ?,
            updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        WHERE id = ?
          AND reward_ledger_id IS NULL
        """,
        (json.dumps(metadata, ensure_ascii=False), relationship["id"]),
    )
    if review_row is None:
        review_id = _new_id("review")
        conn.execute(
            """
            INSERT OR IGNORE INTO meaningful_message_reviews(
                id, referral_relationship_id, invitee_platform_user_id,
                account_id, message_ids_json, reviewer_type, status,
                reason, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, 'ai', 'pending', ?, ?)
            """,
            (
                review_id,
                relationship["id"],
                invitee_platform_user_id,
                account_id,
                json.dumps(
                    candidate_ids[:REFERRAL_QUALIFYING_MESSAGE_COUNT],
                    ensure_ascii=False,
                ),
                "soft review delayed referral reward release",
                json.dumps(
                    {
                        "review_method": "phase1_heuristic",
                        "soft_review_required": True,
                        "reward_release_after": metadata["reward_release_after"],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    return conn.execute(
        "SELECT * FROM referral_relationships WHERE id = ?",
        (relationship["id"],),
    ).fetchone()


def _release_delayed_referral_reward_in_conn(
    conn: Connection,
    *,
    relationship_id: str,
) -> Optional[Row]:
    relationship = conn.execute(
        "SELECT * FROM referral_relationships WHERE id = ?",
        (relationship_id,),
    ).fetchone()
    if relationship is None or relationship["reward_ledger_id"]:
        return None
    if (
        relationship["status"] != "qualified"
        or relationship["review_status"] != "pending"
    ):
        return None

    metadata = _load_referral_metadata(relationship)
    if not metadata.get("soft_review_required"):
        return None
    release_after = _clean_text(metadata.get("reward_release_after"))
    if not release_after or release_after > _beijing_timestamp_in_conn(conn):
        return None

    now = _beijing_timestamp_in_conn(conn)
    metadata["soft_review_released_at"] = now
    metadata["soft_review_release_method"] = "delay_elapsed"
    conn.execute(
        """
        UPDATE referral_relationships
        SET review_status = 'passed',
            metadata_json = ?,
            updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        WHERE id = ?
          AND reward_ledger_id IS NULL
          AND review_status = 'pending'
        """,
        (json.dumps(metadata, ensure_ascii=False), relationship_id),
    )
    review_row = conn.execute(
        """
        SELECT *
        FROM meaningful_message_reviews
        WHERE referral_relationship_id = ?
          AND reviewer_type = 'ai'
        """,
        (relationship_id,),
    ).fetchone()
    review_metadata = {
        "review_method": "phase1_heuristic",
        "soft_review_required": True,
        "soft_review_released_at": now,
    }
    review_account_id = _clean_text(metadata.get("bound_account_id"))
    if not review_account_id:
        review_account_id = _first_rewardable_account_for_platform_user_in_conn(
            conn,
            platform_user_id=relationship["invitee_platform_user_id"],
        )
    if review_row is None and review_account_id:
        conn.execute(
            """
            INSERT INTO meaningful_message_reviews(
                id, referral_relationship_id, invitee_platform_user_id,
                account_id, message_ids_json, reviewer_type, status,
                reason, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, 'ai', 'passed', ?, ?)
            """,
            (
                _new_id("review"),
                relationship_id,
                relationship["invitee_platform_user_id"],
                review_account_id,
                json.dumps(
                    metadata.get("candidate_message_ids") or [],
                    ensure_ascii=False,
                ),
                "soft review delay elapsed",
                json.dumps(review_metadata, ensure_ascii=False),
            ),
        )
    elif review_row is not None:
        conn.execute(
            """
            UPDATE meaningful_message_reviews
            SET status = 'passed',
                reason = ?,
                metadata_json = ?
            WHERE id = ?
            """,
            (
                "soft review delay elapsed",
                json.dumps(review_metadata, ensure_ascii=False),
                review_row["id"],
            ),
        )

    return _apply_referral_reward_in_conn(
        conn,
        relationship_id=relationship_id,
    )


def _is_meaningful_referral_message(*, content: Optional[str], message_type: str) -> bool:
    """判断一条入站消息是否计入拉新有效消息（用于满 3 条触发邀请奖励）。

    规则：
    - 图片消息无条件计为有效（用户发图视为真实互动）。
    - 文本/语音消息需去空白后 >= 3 个字符、非寒暄黑名单、含中英文字符、
      非纯数字、非纯 URL、非单字符重复。
    - 其余消息类型一律不计。
    """
    # 图片视为有效互动，不受字数/占位符限制
    if message_type == "image":
        return True
    if message_type not in {"text", "voice"}:
        return False
    compact = re.sub(r"\s+", "", content or "")
    if not compact:
        return False
    lowered = compact.lower()
    if lowered in {
        "[voice message]",
        "[图片]",
        "你好",
        "您好",
        "在吗",
        "hello",
        "hi",
        "ok",
        "好的",
        "谢谢",
        "哈哈",
        "哈哈哈",
    }:
        return False
    if len(compact) < 3:
        return False
    if lowered.startswith(("http://", "https://")):
        return False
    if re.fullmatch(r"\d+", compact):
        return False
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", compact):
        return False
    if len(set(compact)) == 1:
        return False
    return True


def _mark_referral_relationship_bound_in_conn(
    conn: Connection,
    *,
    invitee_platform_user_id: str,
    account_id: str,
) -> Optional[Row]:
    row = conn.execute(
        """
        SELECT *
        FROM referral_relationships
        WHERE invitee_platform_user_id = ?
        """,
        (invitee_platform_user_id,),
    ).fetchone()
    if row is None:
        return None
    if row["status"] in {"pending_registration", "registered"}:
        metadata = _load_referral_metadata(row)
        metadata.setdefault("bound_account_id", account_id)
        conn.execute(
            """
            UPDATE referral_relationships
            SET status = 'bound',
                metadata_json = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND status IN ('pending_registration', 'registered')
            """,
            (json.dumps(metadata, ensure_ascii=False), row["id"]),
        )
        row = conn.execute(
            "SELECT * FROM referral_relationships WHERE id = ?",
            (row["id"],),
        ).fetchone()
    return row


def mark_referral_relationship_bound(
    *,
    invitee_platform_user_id: str,
    account_id: str,
) -> Optional[Dict[str, Any]]:
    """Mark a new user's referral relationship as bound after WeChat QR completion."""
    with connect() as conn:
        row = _mark_referral_relationship_bound_in_conn(
            conn,
            invitee_platform_user_id=invitee_platform_user_id,
            account_id=account_id,
        )
    return _decode_referral_relationship_row(row) if row else None


def _first_rewardable_account_for_platform_user_in_conn(
    conn: Connection,
    *,
    platform_user_id: str,
) -> Optional[str]:
    row = conn.execute(
        """
        SELECT b.account_id
        FROM account_owner_bindings b
        JOIN accounts a ON a.id = b.account_id
        WHERE b.platform_user_id = ?
          AND b.status = 'active'
          AND a.status = 'active'
        ORDER BY b.created_at ASC, b.id ASC
        LIMIT 1
        """,
        (platform_user_id,),
    ).fetchone()
    return str(row["account_id"]) if row else None


def _apply_referral_reward_in_conn(
    conn: Connection,
    *,
    relationship_id: str,
) -> Optional[Row]:
    relationship = conn.execute(
        """
        SELECT *
        FROM referral_relationships
        WHERE id = ?
        """,
        (relationship_id,),
    ).fetchone()
    if relationship is None or relationship["reward_ledger_id"]:
        return None

    inviter_account_id = _first_rewardable_account_for_platform_user_in_conn(
        conn,
        platform_user_id=relationship["inviter_platform_user_id"],
    )
    if inviter_account_id is None:
        metadata = _load_referral_metadata(relationship)
        metadata["reward_blocked_reason"] = "inviter_has_no_active_account"
        conn.execute(
            """
            UPDATE referral_relationships
            SET status = 'qualified',
                review_status = 'passed',
                metadata_json = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
              AND reward_ledger_id IS NULL
            """,
            (json.dumps(metadata, ensure_ascii=False), relationship_id),
        )
        return None

    ledger_row = _apply_wallet_ledger_in_conn(
        conn,
        account_id=inviter_account_id,
        platform_user_id=relationship["inviter_platform_user_id"],
        amount_shell_micros=REFERRAL_REWARD_SHELL_MICROS,
        entry_type="credit",
        source_type="referral_reward",
        source_id=relationship_id,
        idempotency_key=f"referral-reward-{relationship_id}",
        metadata={
            "referral_relationship_id": relationship_id,
            "invitee_platform_user_id": relationship["invitee_platform_user_id"],
            "grant_shells": REFERRAL_REWARD_SHELLS,
        },
    )
    conn.execute(
        """
        UPDATE referral_relationships
        SET status = 'rewarded',
            review_status = 'passed',
            reward_ledger_id = ?,
            rewarded_at = COALESCE(rewarded_at, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
            updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
        WHERE id = ?
          AND reward_ledger_id IS NULL
        """,
        (ledger_row["id"], relationship_id),
    )
    return ledger_row


def _retry_qualified_referral_rewards_for_inviter_in_conn(
    conn: Connection,
    *,
    inviter_platform_user_id: str,
) -> List[Row]:
    ledgers = _release_due_delayed_referral_rewards_for_inviter_in_conn(
        conn,
        inviter_platform_user_id=inviter_platform_user_id,
    )
    rows = conn.execute(
        """
        SELECT id
        FROM referral_relationships
        WHERE inviter_platform_user_id = ?
          AND status = 'qualified'
          AND review_status = 'passed'
          AND reward_ledger_id IS NULL
        ORDER BY updated_at ASC, id ASC
        """,
        (inviter_platform_user_id,),
    ).fetchall()
    for row in rows:
        ledger_row = _apply_referral_reward_in_conn(
            conn,
            relationship_id=row["id"],
        )
        if ledger_row is not None:
            ledgers.append(ledger_row)
    return ledgers


def _release_due_delayed_referral_rewards_for_inviter_in_conn(
    conn: Connection,
    *,
    inviter_platform_user_id: str,
    limit: int = 100,
) -> List[Row]:
    rows = conn.execute(
        """
        SELECT id
        FROM referral_relationships
        WHERE inviter_platform_user_id = ?
          AND status = 'qualified'
          AND review_status = 'pending'
          AND reward_ledger_id IS NULL
        ORDER BY updated_at ASC, id ASC
        LIMIT ?
        """,
        (inviter_platform_user_id, max(1, min(int(limit), 500))),
    ).fetchall()
    ledgers: List[Row] = []
    for row in rows:
        try:
            ledger_row = _release_delayed_referral_reward_in_conn(
                conn,
                relationship_id=row["id"],
            )
        except Exception:
            logger.exception(
                "delayed referral reward release failed relationship=%s",
                row["id"],
            )
            continue
        if ledger_row is not None:
            ledgers.append(ledger_row)
    return ledgers


def retry_qualified_referral_rewards_for_user(
    *,
    platform_user_id: str,
) -> List[Dict[str, Any]]:
    """Retry already-qualified referral rewards once the inviter has an active account."""
    with connect() as conn:
        rows = _retry_qualified_referral_rewards_for_inviter_in_conn(
            conn,
            inviter_platform_user_id=platform_user_id,
        )
    return [_decode_ledger_row(row) for row in rows]


def release_due_referral_rewards_for_user(
    *,
    platform_user_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Release delayed referral rewards whose soft-review hold has elapsed."""
    with connect() as conn:
        rows = _release_due_delayed_referral_rewards_for_inviter_in_conn(
            conn,
            inviter_platform_user_id=platform_user_id,
            limit=limit,
        )
    return [_decode_ledger_row(row) for row in rows]


def release_due_referral_rewards(limit: int = 200) -> List[Dict[str, Any]]:
    """Release all delayed referral rewards that are due, for admin or scheduler runs."""
    clean_limit = max(1, min(int(limit), 1000))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM referral_relationships
            WHERE status = 'qualified'
              AND review_status = 'pending'
              AND reward_ledger_id IS NULL
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (clean_limit,),
        ).fetchall()
        ledgers: List[Row] = []
        for row in rows:
            try:
                ledger_row = _release_delayed_referral_reward_in_conn(
                    conn,
                    relationship_id=row["id"],
                )
            except Exception:
                logger.exception(
                    "delayed referral reward release failed relationship=%s",
                    row["id"],
                )
                continue
            if ledger_row is not None:
                ledgers.append(ledger_row)
    return [_decode_ledger_row(row) for row in ledgers]


def process_referral_message_for_account(
    *,
    account_id: str,
    message_db_id: int,
) -> Optional[Dict[str, Any]]:
    """Count one bound invitee message and pay the inviter when the threshold is met."""
    with connect() as conn:
        owner = conn.execute(
            """
            SELECT platform_user_id
            FROM account_owner_bindings
            WHERE account_id = ?
              AND status = 'active'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if owner is None:
            return None
        invitee_platform_user_id = owner["platform_user_id"]

        relationship = _mark_referral_relationship_bound_in_conn(
            conn,
            invitee_platform_user_id=invitee_platform_user_id,
            account_id=account_id,
        )
        if relationship is None:
            return None

        review_row = conn.execute(
            """
            SELECT *
            FROM meaningful_message_reviews
            WHERE referral_relationship_id = ?
              AND reviewer_type = 'ai'
            """,
            (relationship["id"],),
        ).fetchone()
        ledger_row = None
        if relationship["reward_ledger_id"]:
            return {
                "relationship": _decode_referral_relationship_row(relationship),
                "review": _decode_meaningful_message_review_row(review_row) if review_row else None,
                "ledger": None,
            }

        message = conn.execute(
            """
            SELECT id, account_id, direction, role, message_type, content
            FROM messages
            WHERE id = ?
              AND account_id = ?
              AND direction = 'inbound'
              AND role = 'user'
            """,
            (message_db_id, account_id),
        ).fetchone()
        metadata = _load_referral_metadata(relationship)
        candidate_ids = list(metadata.get("candidate_message_ids") or [])
        candidate_id = int(message_db_id)
        if (
            message is not None
            and relationship["status"] in {"bound", "qualified"}
            and candidate_id not in candidate_ids
            and _is_meaningful_referral_message(
                content=message["content"],
                message_type=message["message_type"],
            )
        ):
            candidate_ids.append(candidate_id)
            metadata["candidate_message_ids"] = candidate_ids
            status = (
                "qualified"
                if len(candidate_ids) >= REFERRAL_QUALIFYING_MESSAGE_COUNT
                else "bound"
            )
            conn.execute(
                """
                UPDATE referral_relationships
                SET meaningful_message_count = ?,
                    status = ?,
                    metadata_json = ?,
                    updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                  AND reward_ledger_id IS NULL
                """,
                (
                    len(candidate_ids),
                    status,
                    json.dumps(metadata, ensure_ascii=False),
                    relationship["id"],
                ),
            )
            relationship = conn.execute(
                "SELECT * FROM referral_relationships WHERE id = ?",
                (relationship["id"],),
            ).fetchone()

        if int(relationship["meaningful_message_count"] or 0) >= REFERRAL_QUALIFYING_MESSAGE_COUNT:
            metadata = _load_referral_metadata(relationship)
            if metadata.get("soft_review_required"):
                if relationship["review_status"] == "passed":
                    ledger_row = _apply_referral_reward_in_conn(
                        conn,
                        relationship_id=relationship["id"],
                    )
                elif relationship["review_status"] == "pending":
                    relationship = _ensure_referral_soft_review_hold_in_conn(
                        conn,
                        relationship=relationship,
                        invitee_platform_user_id=invitee_platform_user_id,
                        account_id=account_id,
                        candidate_ids=candidate_ids,
                        review_row=review_row,
                    )
                    ledger_row = _release_delayed_referral_reward_in_conn(
                        conn,
                        relationship_id=relationship["id"],
                    )
                relationship = conn.execute(
                    "SELECT * FROM referral_relationships WHERE id = ?",
                    (relationship["id"],),
                ).fetchone()
                review_row = conn.execute(
                    """
                    SELECT *
                    FROM meaningful_message_reviews
                    WHERE referral_relationship_id = ?
                      AND reviewer_type = 'ai'
                    """,
                    (relationship["id"],),
                ).fetchone()
            else:
                if review_row is None:
                    review_id = _new_id("review")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO meaningful_message_reviews(
                            id, referral_relationship_id, invitee_platform_user_id,
                            account_id, message_ids_json, reviewer_type, status,
                            reason, metadata_json
                        )
                        VALUES (?, ?, ?, ?, ?, 'ai', 'passed', ?, ?)
                        """,
                        (
                            review_id,
                            relationship["id"],
                            invitee_platform_user_id,
                            account_id,
                            json.dumps(candidate_ids[:REFERRAL_QUALIFYING_MESSAGE_COUNT], ensure_ascii=False),
                            "phase1 heuristic accepted 3 meaningful inbound messages",
                            json.dumps(
                                {
                                    "review_method": "phase1_heuristic",
                                    "qualifying_message_count": REFERRAL_QUALIFYING_MESSAGE_COUNT,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    review_row = conn.execute(
                        """
                        SELECT *
                        FROM meaningful_message_reviews
                        WHERE referral_relationship_id = ?
                          AND reviewer_type = 'ai'
                        """,
                        (relationship["id"],),
                    ).fetchone()
                if review_row is not None and review_row["status"] == "passed":
                    conn.execute(
                        """
                        UPDATE referral_relationships
                        SET status = 'qualified',
                            review_status = 'passed',
                            updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                        WHERE id = ?
                          AND reward_ledger_id IS NULL
                        """,
                        (relationship["id"],),
                    )
                    ledger_row = _apply_referral_reward_in_conn(
                        conn,
                        relationship_id=relationship["id"],
                    )
                    relationship = conn.execute(
                        "SELECT * FROM referral_relationships WHERE id = ?",
                        (relationship["id"],),
                    ).fetchone()

        return {
            "relationship": _decode_referral_relationship_row(relationship),
            "review": _decode_meaningful_message_review_row(review_row) if review_row else None,
            "ledger": _decode_ledger_row(ledger_row) if ledger_row else None,
        }


def list_referral_relationships(
    *,
    limit: int = 50,
    inviter_platform_user_id: Optional[str] = None,
    invitee_platform_user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List referral relationships for admin diagnostics without exposing phone numbers."""
    clean_limit = max(1, min(int(limit), 200))
    clauses = []
    params: List[Any] = []
    if inviter_platform_user_id:
        clauses.append("inviter_platform_user_id = ?")
        params.append(inviter_platform_user_id)
    if invitee_platform_user_id:
        clauses.append("invitee_platform_user_id = ?")
        params.append(invitee_platform_user_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM referral_relationships
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (*params, clean_limit),
        ).fetchall()
    return [_decode_referral_relationship_row(row) for row in rows]


def _estimate_tokens_from_text(text: Optional[str]) -> int:
    cleaned = text or ""
    if not cleaned:
        return 0
    # Conservative mixed Chinese/English approximation until provider usage is wired.
    return max(1, math.ceil(len(cleaned) / 2))


def _estimate_tokens_from_messages(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += _estimate_tokens_from_text(content)
        elif isinstance(content, list):
            total += _estimate_tokens_from_text(json.dumps(content, ensure_ascii=False))
    return total


def _shell_micros_for_tokens(
    *,
    billable_tokens: int,
    model_price_multiplier_micros: int = 1_000_000,
) -> int:
    if billable_tokens <= 0:
        return 0
    return math.ceil(
        int(billable_tokens)
        * int(model_price_multiplier_micros)
        * SHELL_MICROS_PER_SHELL
        / (SHELL_BILLABLE_TOKENS_PER_SHELL * 1_000_000)
    )


def get_platform_user_id_for_account(*, account_id: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT platform_user_id
            FROM account_owner_bindings
            WHERE account_id = ?
              AND status = 'active'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
    return str(row["platform_user_id"]) if row else None


def record_chat_usage_charge(
    *,
    account_id: str,
    model: str,
    messages: List[Dict[str, Any]],
    reply: str,
    source_type: str,
    source_id: Optional[str],
    idempotency_key: str,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    model_price_multiplier_micros: int = 1_000_000,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    cleaned_idempotency_key = _clean_text(idempotency_key)
    if not cleaned_idempotency_key:
        raise ValueError("idempotency_key is required")

    estimated = input_tokens is None or output_tokens is None
    resolved_input_tokens = (
        int(input_tokens)
        if input_tokens is not None
        else _estimate_tokens_from_messages(messages)
    )
    resolved_output_tokens = (
        int(output_tokens)
        if output_tokens is not None
        else _estimate_tokens_from_text(reply)
    )
    billable_tokens = max(0, resolved_input_tokens) + max(0, resolved_output_tokens)
    computed_shell_micros = _shell_micros_for_tokens(
        billable_tokens=billable_tokens,
        model_price_multiplier_micros=model_price_multiplier_micros,
    )
    if computed_shell_micros <= 0:
        return None

    platform_user_id = get_platform_user_id_for_account(account_id=account_id)
    if platform_user_id is None:
        return None

    with connect() as conn:
        existing = conn.execute(
            """
            SELECT ce.*, l.id AS ledger_exists
            FROM cost_events ce
            LEFT JOIN entitlement_ledger l ON l.id = ce.entitlement_ledger_id
            WHERE ce.idempotency_key = ?
            """,
            (cleaned_idempotency_key,),
        ).fetchone()
        if existing is not None:
            event = _decode_cost_event_row(existing)
            ledger = None
            if event.get("entitlement_ledger_id"):
                ledger_row = conn.execute(
                    "SELECT * FROM entitlement_ledger WHERE id = ?",
                    (event["entitlement_ledger_id"],),
                ).fetchone()
                ledger = _decode_ledger_row(ledger_row) if ledger_row else None
            wallet_row = conn.execute(
                """
                SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                       created_at, updated_at
                FROM entitlement_wallets
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            return {
                "cost_event": event,
                "ledger": ledger,
                "wallet": _decode_wallet_row(wallet_row) if wallet_row else None,
            }

        wallet = _ensure_wallet_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
        event_id = _new_id("cost")
        ledger_idempotency_key = f"usage-charge-{cleaned_idempotency_key}"
        charge_metadata = {
            "estimated": estimated,
            "input_tokens": resolved_input_tokens,
            "output_tokens": resolved_output_tokens,
            "billable_tokens": billable_tokens,
            "model": model,
            **(metadata or {}),
        }
        ledger_row = _apply_wallet_ledger_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
            amount_shell_micros=-computed_shell_micros,
            entry_type="debit",
            source_type="usage_charge",
            source_id=source_id,
            idempotency_key=ledger_idempotency_key,
            metadata=charge_metadata,
        )
        conn.execute(
            """
            INSERT INTO cost_events(
                id, wallet_id, account_id, platform_user_id, cost_type,
                cost_owner, billable_to_user, model, input_tokens, output_tokens,
                billable_tokens, model_price_multiplier_micros, computed_shell_micros,
                entitlement_ledger_id, source_type, source_id, idempotency_key,
                metadata_json
            )
            VALUES (?, ?, ?, ?, 'llm_tokens', 'user', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                wallet["id"],
                account_id,
                platform_user_id,
                _clean_text(model),
                resolved_input_tokens,
                resolved_output_tokens,
                billable_tokens,
                int(model_price_multiplier_micros),
                computed_shell_micros,
                ledger_row["id"],
                _clean_text(source_type),
                _clean_text(source_id),
                cleaned_idempotency_key,
                json.dumps(charge_metadata, ensure_ascii=False),
            ),
        )
        event_row = conn.execute(
            "SELECT * FROM cost_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        wallet_row = conn.execute(
            """
            SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                   created_at, updated_at
            FROM entitlement_wallets
            WHERE id = ?
            """,
            (wallet["id"],),
        ).fetchone()

    if event_row is None:
        raise RuntimeError("cost_event was not created")
    return {
        "cost_event": _decode_cost_event_row(event_row),
        "ledger": _decode_ledger_row(ledger_row),
        "wallet": _decode_wallet_row(wallet_row),
    }


def record_image_understanding_charge(
    *,
    account_id: str,
    source_id: Optional[str],
    idempotency_key: str,
    model: Optional[str] = None,
    cost_shell_micros: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Record one fixed-price image-understanding cost event (independent of chat tokens).

    Mirrors record_chat_usage_charge but charges a flat shell amount with
    cost_type='image_understanding' and no token estimation. Idempotent on
    idempotency_key so retries never double-charge. Account-isolated.
    Returns None when the charge amount is non-positive or the account has no
    platform user (e.g. unbilled internal accounts).
    """
    cleaned_idempotency_key = _clean_text(idempotency_key)
    if not cleaned_idempotency_key:
        raise ValueError("idempotency_key is required")

    charge_micros = (
        int(cost_shell_micros)
        if cost_shell_micros is not None
        else int(_settings().image_understanding_cost_shell_micros)
    )
    if charge_micros <= 0:
        return None

    platform_user_id = get_platform_user_id_for_account(account_id=account_id)
    if platform_user_id is None:
        return None

    with connect() as conn:
        existing = conn.execute(
            """
            SELECT ce.*, l.id AS ledger_exists
            FROM cost_events ce
            LEFT JOIN entitlement_ledger l ON l.id = ce.entitlement_ledger_id
            WHERE ce.idempotency_key = ?
            """,
            (cleaned_idempotency_key,),
        ).fetchone()
        if existing is not None:
            event = _decode_cost_event_row(existing)
            ledger = None
            if event.get("entitlement_ledger_id"):
                ledger_row = conn.execute(
                    "SELECT * FROM entitlement_ledger WHERE id = ?",
                    (event["entitlement_ledger_id"],),
                ).fetchone()
                ledger = _decode_ledger_row(ledger_row) if ledger_row else None
            wallet_row = conn.execute(
                """
                SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                       created_at, updated_at
                FROM entitlement_wallets
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            return {
                "cost_event": event,
                "ledger": ledger,
                "wallet": _decode_wallet_row(wallet_row) if wallet_row else None,
            }

        wallet = _ensure_wallet_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
        event_id = _new_id("cost")
        ledger_idempotency_key = f"usage-charge-{cleaned_idempotency_key}"
        charge_metadata = {
            "cost_type": "image_understanding",
            "model": model,
            **(metadata or {}),
        }
        ledger_row = _apply_wallet_ledger_in_conn(
            conn,
            account_id=account_id,
            platform_user_id=platform_user_id,
            amount_shell_micros=-charge_micros,
            entry_type="debit",
            source_type="image_understanding_charge",
            source_id=source_id,
            idempotency_key=ledger_idempotency_key,
            metadata=charge_metadata,
        )
        conn.execute(
            """
            INSERT INTO cost_events(
                id, wallet_id, account_id, platform_user_id, cost_type,
                cost_owner, billable_to_user, model, input_tokens, output_tokens,
                billable_tokens, model_price_multiplier_micros, computed_shell_micros,
                entitlement_ledger_id, source_type, source_id, idempotency_key,
                metadata_json
            )
            VALUES (?, ?, ?, ?, 'image_understanding', 'user', 1, ?, 0, 0, 0, 1000000, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                wallet["id"],
                account_id,
                platform_user_id,
                _clean_text(model),
                charge_micros,
                ledger_row["id"],
                "image_understanding_charge",
                _clean_text(source_id),
                cleaned_idempotency_key,
                json.dumps(charge_metadata, ensure_ascii=False),
            ),
        )
        event_row = conn.execute(
            "SELECT * FROM cost_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        wallet_row = conn.execute(
            """
            SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                   created_at, updated_at
            FROM entitlement_wallets
            WHERE id = ?
            """,
            (wallet["id"],),
        ).fetchone()

    if event_row is None:
        raise RuntimeError("image_understanding cost_event was not created")
    return {
        "cost_event": _decode_cost_event_row(event_row),
        "ledger": _decode_ledger_row(ledger_row),
        "wallet": _decode_wallet_row(wallet_row),
    }


def get_wallet_summary(
    *,
    account_id: str,
    ensure_grant: bool = False,
    create_if_missing: bool = True,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        owner = conn.execute(
            """
            SELECT b.platform_user_id
            FROM account_owner_bindings b
            JOIN accounts a ON a.id = b.account_id
            WHERE b.account_id = ?
              AND b.status = 'active'
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        if owner is None:
            return None
        platform_user_id = owner["platform_user_id"]

    if ensure_grant:
        grant_new_user_shells(
            account_id=account_id,
            platform_user_id=platform_user_id,
        )
    elif create_if_missing:
        ensure_wallet(account_id=account_id, platform_user_id=platform_user_id)

    with connect() as conn:
        wallet_row = conn.execute(
            """
            SELECT id, account_id, platform_user_id, balance_shell_micros, status,
                   created_at, updated_at
            FROM entitlement_wallets
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
        if wallet_row is None:
            return None
        latest_ledger_row = conn.execute(
            """
            SELECT *
            FROM entitlement_ledger
            WHERE wallet_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (wallet_row["id"],),
        ).fetchone()
    wallet = _decode_wallet_row(wallet_row)
    latest_ledger = _decode_ledger_row(latest_ledger_row) if latest_ledger_row else None
    return {
        "wallet": wallet,
        "latest_ledger": latest_ledger,
        "display": {
            "balance": wallet["balance_shells"],
            "unit": "贝壳",
        },
    }


def get_wallet_balance_shell_micros(*, account_id: str) -> Optional[int]:
    """只读返回账号当前贝壳余额（micros）；无绑定 / 无钱包返回 None。

    供 agent_need_survival_status 的资源风险判断；不创建钱包、不发放新用户额度。
    """
    summary = get_wallet_summary(
        account_id=account_id,
        ensure_grant=False,
        create_if_missing=False,
    )
    if not summary or not summary.get("wallet"):
        return None
    return int(summary["wallet"]["balance_shell_micros"])


def list_wallet_ledger(
    *,
    account_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    clean_limit = max(1, min(int(limit), 200))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM entitlement_ledger
            WHERE account_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (account_id, clean_limit),
        ).fetchall()
    return [_decode_ledger_row(row) for row in rows]


def create_ai4all_account_for_user(
    *,
    platform_user_id: str,
    display_name: Optional[str],
    system_prompt: Optional[str] = None,
    plan: str = "free",
    require_display_name: bool = True,
    campaign_code: Optional[str] = None,
) -> Dict[str, Any]:
    from app.db.accounts import get_account, get_profile_for_account
    cleaned_display_name = _clean_text(display_name)
    if require_display_name and not cleaned_display_name:
        raise ValueError("display_name is required")

    cleaned_prompt = _clean_text(system_prompt)
    with connect() as conn:
        user = conn.execute(
            "SELECT id FROM platform_users WHERE id = ?",
            (platform_user_id,),
        ).fetchone()
        if user is None:
            raise ValueError("platform_user not found")
        existing_count = conn.execute(
            """
            SELECT COUNT(*) FROM account_owner_bindings
            WHERE platform_user_id = ? AND status = 'active'
            """,
            (platform_user_id,),
        ).fetchone()[0]
        if existing_count >= 10:
            raise ValueError("platform_user has reached the maximum number of agents (10)")
        last_integrity_error = None
        for _ in range(_ACCOUNT_ID_GENERATION_RETRIES):
            account_id = _new_account_id()
            try:
                # 保存点隔离 id 冲突：PG 下冲突会中止整笔事务，需回滚到保存点才能继续换 id 重试
                with _savepoint(conn, "account_ins"):
                    conn.execute(
                        """
                        INSERT INTO accounts(id, channel, display_name, updated_at)
                        VALUES (?, 'openclaw-weixin', ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                        """,
                        (account_id, cleaned_display_name),
                    )
                break
            except IntegrityError as err:
                if "accounts.id" not in str(err):
                    raise
                last_integrity_error = err
        else:
            raise RuntimeError("failed to generate a unique account_id") from last_integrity_error
        conn.execute(
            """
            INSERT INTO profiles(account_id, display_name, system_prompt, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (account_id, cleaned_display_name, cleaned_prompt),
        )
        cursor = conn.execute(
            """
            INSERT INTO account_owner_bindings(
                platform_user_id, account_id, binding_method, status, updated_at
            )
            VALUES (?, ?, 'web_onboarding', 'active', strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (platform_user_id, account_id),
        )
        owner_binding_id = int(cursor.lastrowid)

    subscription = upsert_subscription_for_user(
        platform_user_id=platform_user_id,
        plan=plan,
    )
    grant_new_user_shells(
        account_id=account_id,
        platform_user_id=platform_user_id,
    )
    retry_qualified_referral_rewards_for_user(platform_user_id=platform_user_id)

    # 营销活码归因：校验 → 写快照 → 计数 → 应用强制 SOUL 人设。与 onboarding 调试建号
    # 共用 apply_campaign_code_attribution（后者 increment_usage=False），确保调试忠实复现
    # 真实注册效果。校验失败/异常 fail-open，不阻断注册（campaign_codes_technical_design.md §3）。
    from app.db.campaign import apply_campaign_code_attribution
    apply_campaign_code_attribution(
        account_id=account_id,
        campaign_code=campaign_code,
        increment_usage=True,
    )

    return {
        "account": get_account(account_id=account_id),
        "profile": get_profile_for_account(account_id=account_id),
        "owner_binding": get_account_owner_binding(owner_binding_id=owner_binding_id),
        "subscription": subscription,
    }


def get_first_active_account_for_user(
    *,
    platform_user_id: str,
) -> Optional[Dict[str, Any]]:
    from app.db.accounts import get_account, get_profile_for_account
    with connect() as conn:
        row = conn.execute(
            """
            SELECT b.id, b.account_id
            FROM account_owner_bindings b
            JOIN accounts a ON a.id = b.account_id
            WHERE b.platform_user_id = ?
              AND b.status = 'active'
            ORDER BY b.created_at ASC, b.id ASC
            LIMIT 1
            """,
            (platform_user_id,),
        ).fetchone()
    if row is None:
        return None
    account = get_account(account_id=row["account_id"])
    if account is None:
        return None
    grant_new_user_shells(
        account_id=row["account_id"],
        platform_user_id=platform_user_id,
    )
    retry_qualified_referral_rewards_for_user(platform_user_id=platform_user_id)
    return {
        "account": account,
        "profile": get_profile_for_account(account_id=row["account_id"]),
        "owner_binding": get_account_owner_binding(owner_binding_id=int(row["id"])),
        "subscription": get_latest_subscription_for_user(
            platform_user_id=platform_user_id,
        ),
    }


def get_or_create_default_ai4all_account_for_user(
    *,
    platform_user_id: str,
    display_name: Optional[str] = None,
    plan: str = "free",
    campaign_code: Optional[str] = None,
) -> Dict[str, Any]:
    existing = get_first_active_account_for_user(platform_user_id=platform_user_id)
    if existing is not None:
        return existing
    return create_ai4all_account_for_user(
        platform_user_id=platform_user_id,
        display_name=_clean_default_account_display_name(display_name),
        system_prompt=None,
        plan=plan,
        campaign_code=campaign_code,
        require_display_name=False,
    )


def get_account_owner_binding(
    *,
    owner_binding_id: int,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, binding_method, status,
                verified_at, created_at, updated_at
            FROM account_owner_bindings
            WHERE id = ?
            """,
            (owner_binding_id,),
        ).fetchone()
    return dict(row) if row else None


def list_account_owner_bindings_for_account(
    *,
    account_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, binding_method, status,
                verified_at, created_at, updated_at
            FROM account_owner_bindings
            WHERE account_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def create_binding_intent(
    *,
    platform_user_id: str,
    account_id: str,
    channel: str = "openclaw-weixin",
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    from app.db.proactive import pick_node, resolve_node_for_account, set_account_assigned_node
    cleaned_channel = _clean_text(channel) or "openclaw-weixin"
    binding_intent_id = _new_id("bind")
    openclaw_login_session_key = binding_intent_id
    manual_login_command = (
        "openclaw channels login "
        f"--channel {cleaned_channel} "
        f"--account {openclaw_login_session_key} "
        "--verbose"
    )

    # 多机接入:定目标节点 = 显式 > 账号既有归属 > pick_node(最闲 online) > default_node_id。
    # standalone 下三者皆空 → None,登录走本机直调(_is_local_node 命中),行为不变。
    resolved_node_id = (
        _clean_text(node_id)
        or resolve_node_for_account(account_id)
        or pick_node()
        or (_clean_text(getattr(settings, "default_node_id", "")) or None)
    )

    with connect() as conn:
        owner = conn.execute(
            """
            SELECT id FROM account_owner_bindings
            WHERE platform_user_id = ?
              AND account_id = ?
              AND status = 'active'
            """,
            (platform_user_id, account_id),
        ).fetchone()
        if owner is None:
            raise ValueError("account is not owned by platform_user")
        conn.execute(
            """
            INSERT INTO binding_intents(
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, manual_login_command, node_id, expires_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'created', ?, ?, datetime('now', '+8 hours', '+30 minutes'), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                binding_intent_id,
                platform_user_id,
                account_id,
                openclaw_login_session_key,
                cleaned_channel,
                manual_login_command,
                resolved_node_id,
            ),
        )
    # 登录会话将钉死在该节点 → 同步写账号归属(出站路由/入站反查依据)。
    if resolved_node_id:
        set_account_assigned_node(account_id=account_id, node_id=resolved_node_id)
    item = get_binding_intent(binding_intent_id=binding_intent_id)
    if item is None:
        raise RuntimeError("binding_intent was not created")
    return item


def update_binding_intent(
    *,
    binding_intent_id: str,
    status: Optional[str] = None,
    openclaw_login_session_key: Optional[str] = None,
    channel_account_id: Optional[str] = None,
    qr_data_url: Optional[str] = None,
    raw_result: Optional[Dict[str, Any]] = None,
    completed: bool = False,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_binding_intent(binding_intent_id=binding_intent_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE binding_intents
            SET status = ?,
                openclaw_login_session_key = ?,
                channel_account_id = COALESCE(?, channel_account_id),
                qr_data_url = ?,
                raw_result_json = ?,
                error = ?,
                completed_at = CASE WHEN ? THEN strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')) ELSE completed_at END,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                status if status is not None else current["status"],
                openclaw_login_session_key
                if openclaw_login_session_key is not None
                else current["openclaw_login_session_key"],
                channel_account_id,
                qr_data_url if qr_data_url is not None else current.get("qr_data_url"),
                json.dumps(raw_result, ensure_ascii=False)
                if raw_result is not None
                else json.dumps(current.get("raw_result") or {}, ensure_ascii=False),
                error,
                1 if completed else 0,
                binding_intent_id,
            ),
        )
    return get_binding_intent(binding_intent_id=binding_intent_id)


def set_binding_intent_error(
    *,
    binding_intent_id: str,
    status: str,
    error: str,
    raw_result: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    current = get_binding_intent(binding_intent_id=binding_intent_id)
    if current is None:
        return None
    with connect() as conn:
        conn.execute(
            """
            UPDATE binding_intents
            SET status = ?,
                raw_result_json = ?,
                error = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (
                status,
                json.dumps(
                    raw_result if raw_result is not None else current.get("raw_result") or {},
                    ensure_ascii=False,
                ),
                error,
                binding_intent_id,
            ),
        )
    return get_binding_intent(binding_intent_id=binding_intent_id)


def get_binding_intent(
    *,
    binding_intent_id: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                node_id, raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE id = ?
            """,
            (binding_intent_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
    except json.JSONDecodeError:
        item["raw_result"] = {}
        item["raw_result_decode_error"] = True
    # Auto-expire stale qr_created intents
    if item.get("status") == "qr_created" and item.get("expires_at"):
        with connect() as conn:
            conn.execute(
                """
                UPDATE binding_intents
                SET status = 'expired', updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ? AND status = 'qr_created'
                  AND expires_at < datetime('now', '+8 hours')
                """,
                (binding_intent_id,),
            )
            refreshed = conn.execute(
                """
                SELECT
                    id, platform_user_id, account_id, openclaw_login_session_key,
                    channel, status, channel_account_id, qr_data_url, manual_login_command,
                    node_id, raw_result_json, expires_at, completed_at, error, created_at, updated_at
                FROM binding_intents WHERE id = ?
                """,
                (binding_intent_id,),
            ).fetchone()
        if refreshed is not None:
            item = dict(refreshed)
            try:
                item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
            except json.JSONDecodeError:
                item["raw_result"] = {}
    return item


def list_binding_intents_for_account(
    *,
    account_id: str,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                node_id, raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE account_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
        except json.JSONDecodeError:
            item["raw_result"] = {}
            item["raw_result_decode_error"] = True
        result.append(item)
    return result


def get_active_binding_intent_for_channel_account(
    *,
    channel: str,
    channel_account_id: str,
) -> Optional[Dict[str, Any]]:
    account_aliases = _channel_account_id_aliases(channel_account_id)
    if not account_aliases:
        return None
    placeholders = ", ".join("?" for _ in account_aliases)
    with connect() as conn:
        # Primary lookup: dedicated column (reliable, indexed)
        row = conn.execute(
            f"""
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                node_id, raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE channel = ?
              AND status = 'completed'
              AND channel_account_id IN ({placeholders})
            ORDER BY completed_at DESC, updated_at DESC
            LIMIT 1
            """,
            (channel, *account_aliases),
        ).fetchone()
        if row is None:
            # Fallback: json_extract for rows written before the column was added
            row = conn.execute(
                f"""
                SELECT
                    id, platform_user_id, account_id, openclaw_login_session_key,
                    channel, status, channel_account_id, qr_data_url, manual_login_command,
                    node_id, raw_result_json, expires_at, completed_at, error, created_at, updated_at
                FROM binding_intents
                WHERE channel = ?
                  AND status = 'completed'
                  AND json_extract(raw_result_json, '$.channel_account_id') IN ({placeholders})
                ORDER BY completed_at DESC, updated_at DESC
                LIMIT 1
                """,
                (channel, *account_aliases),
            ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
    except json.JSONDecodeError:
        item["raw_result"] = {}
        item["raw_result_decode_error"] = True
    return item


def get_completed_binding_intent_for_openclaw_login_session_key(
    *,
    channel: str,
    openclaw_login_session_key: str,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, platform_user_id, account_id, openclaw_login_session_key,
                channel, status, channel_account_id, qr_data_url, manual_login_command,
                node_id, raw_result_json, expires_at, completed_at, error, created_at, updated_at
            FROM binding_intents
            WHERE channel = ?
              AND status = 'completed'
              AND openclaw_login_session_key = ?
            ORDER BY completed_at DESC, updated_at DESC
            LIMIT 1
            """,
            (channel, openclaw_login_session_key),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["raw_result"] = json.loads(item.pop("raw_result_json") or "{}")
    except json.JSONDecodeError:
        item["raw_result"] = {}
        item["raw_result_decode_error"] = True
    return item


def resolve_account_id_for_inbound_channel_identity(
    *,
    channel: str,
    session_key: str,
    channel_account_id: Optional[str],
) -> Optional[str]:
    """解析入站身份对应的 AI4ALL account_id。

    仅当存在 completed binding intent 时返回真实 account_id；找不到任何绑定时返回
    None（不再用 session_key 兜底）。调用方据此决定收口或回退，避免已解绑/未绑定的
    远端账号被当作新账号自动激活。
    """
    if channel_account_id:
        intent = get_active_binding_intent_for_channel_account(
            channel=channel,
            channel_account_id=channel_account_id,
        )
        if intent:
            return str(intent["account_id"])
        intent = get_completed_binding_intent_for_openclaw_login_session_key(
            channel=channel,
            openclaw_login_session_key=channel_account_id,
        )
        if intent:
            return str(intent["account_id"])
    intent = get_completed_binding_intent_for_openclaw_login_session_key(
        channel=channel,
        openclaw_login_session_key=session_key,
    )
    if intent:
        return str(intent["account_id"])
    return None


def get_or_create_session(
    *,
    account_id: str,
    channel: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    session_key: str,
    business_day: Optional[str] = None,
    carryover_summary: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    update_account_channel: bool = True,
) -> Dict[str, Any]:
    """``update_account_channel=False``（§3）：已存在账号的 ``accounts.channel`` 不被本次
    改写——供 Web 首触已绑微信的账号时保留原渠道字段。默认 True，微信/存量调用方行为不变
    （新建账号仍按 INSERT 写入 ``channel``；仅 ON CONFLICT 更新分支受此开关影响）。"""
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    # 内部固定常量拼接（非用户输入），无注入风险：控制 ON CONFLICT 是否覆写 channel。
    _channel_update_clause = "channel = excluded.channel,\n                " if update_account_channel else ""
    with connect() as conn:
        conn.execute(
            f"""
            INSERT INTO accounts(id, channel, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(id) DO UPDATE SET
                {_channel_update_clause}updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (account_id, channel),
        )
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()

        conn.execute(
            """
            INSERT INTO sessions(
                account_id, session_key, sender_id, chat_id, sender_name,
                business_day, carryover_summary, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id, session_key) DO UPDATE SET
                sender_id = COALESCE(excluded.sender_id, sessions.sender_id),
                chat_id = COALESCE(excluded.chat_id, sessions.chat_id),
                sender_name = COALESCE(excluded.sender_name, sessions.sender_name),
                business_day = COALESCE(sessions.business_day, excluded.business_day),
                carryover_summary = COALESCE(sessions.carryover_summary, excluded.carryover_summary),
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (
                account_id,
                session_key,
                sender_id,
                chat_id,
                sender_name,
                business_day,
                carryover_summary,
                metadata_json,
            ),
        )
        session = conn.execute(
            "SELECT * FROM sessions WHERE account_id = ? AND session_key = ?",
            (account_id, session_key),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO profiles(account_id, updated_at)
            VALUES (?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id) DO UPDATE SET
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (account_id,),
        )
        profile = conn.execute(
            "SELECT * FROM profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()

        return {
            "account": dict(account),
            "session": dict(session),
            "profile": dict(profile),
        }


def get_or_create_account_active_session(
    *,
    account_id: str,
    channel: str,
    sender_id: str,
    sender_name: Optional[str],
    chat_id: Optional[str],
    business_day: Optional[str] = None,
    active_session_key: str = ACCOUNT_ACTIVE_SESSION_KEY,
    update_account_channel: bool = True,
) -> Dict[str, Any]:
    """Return the account-level active session used for main conversation context.

    OpenClaw's session_key is a channel routing/debug field. P0 keeps the
    existing sessions schema and rotates the stable compatibility key when
    the account's active session crosses a lifecycle boundary.

    ``active_session_key`` selects the conversation_scope (§7.1): WeChat uses
    ``__account_active__`` (default, behavior unchanged), Web uses
    ``__web_active__``. The archived key is derived from it, so each scope's
    closed sessions carry their own prefix and stay isolated.

    ``update_account_channel=False``（§3）：已存在账号的 ``accounts.channel`` 不被本次
    改写（供 Web 首触已绑微信账号时保留原渠道）。默认 True，微信路径行为不变。
    """

    # 内部固定常量拼接（非用户输入），无注入风险：控制 ON CONFLICT 是否覆写 channel。
    _channel_update_clause = "channel = excluded.channel,\n                " if update_account_channel else ""
    with connect() as conn:
        conn.execute(
            f"""
            INSERT INTO accounts(id, channel, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(id) DO UPDATE SET
                {_channel_update_clause}updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (account_id, channel),
        )
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()

        conn.execute(
            """
            INSERT INTO profiles(account_id, updated_at)
            VALUES (?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(account_id) DO UPDATE SET
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (account_id,),
        )

        session = conn.execute(
            """
            SELECT * FROM sessions
            WHERE account_id = ? AND session_key = ?
            """,
            (account_id, active_session_key),
        ).fetchone()

        carryover_summary = None
        created_reason = "account_created"
        if session is not None:
            rotation_reason = _active_session_rotation_reason(
                dict(session),
                business_day=business_day,
            )
            if rotation_reason:
                carryover_summary = _build_session_carryover_summary(
                    conn,
                    session_id=int(session["id"]),
                )
                archived_session_key = f"{active_session_key}:{session['id']}"
                conn.execute(
                    """
                    UPDATE sessions
                    SET session_key = ?,
                        status = 'closed',
                        ended_at = COALESCE(ended_at, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
                        close_reason = COALESCE(close_reason, ?),
                        carryover_summary = COALESCE(NULLIF(carryover_summary, ''), ?),
                        updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                    WHERE id = ?
                    """,
                    (
                        archived_session_key,
                        rotation_reason,
                        carryover_summary,
                        int(session["id"]),
                    ),
                )
                session = None
                created_reason = rotation_reason
            else:
                conn.execute(
                    """
                    UPDATE sessions
                    SET sender_id = COALESCE(?, sender_id),
                        chat_id = COALESCE(?, chat_id),
                        sender_name = COALESCE(?, sender_name),
                        business_day = COALESCE(business_day, ?),
                        updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                    WHERE id = ?
                    """,
                    (
                        sender_id,
                        chat_id,
                        sender_name,
                        business_day,
                        int(session["id"]),
                    ),
                )

        if session is None:
            conn.execute(
                """
                INSERT INTO sessions(
                    account_id, session_key, sender_id, chat_id, sender_name,
                    business_day, carryover_summary, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
                """,
                (
                    account_id,
                    active_session_key,
                    sender_id,
                    chat_id,
                    sender_name,
                    business_day,
                    carryover_summary,
                    json.dumps(
                        {"created_reason": created_reason},
                        ensure_ascii=False,
                    ),
                ),
            )

        session = conn.execute(
            """
            SELECT * FROM sessions
            WHERE account_id = ? AND session_key = ?
            """,
            (account_id, active_session_key),
        ).fetchone()
        profile = conn.execute(
            "SELECT * FROM profiles WHERE account_id = ?",
            (account_id,),
        ).fetchone()

        return {
            "account": dict(account),
            "session": dict(session),
            "profile": dict(profile),
        }


def _active_session_rotation_reason(
    session: Dict[str, Any],
    *,
    business_day: Optional[str],
) -> Optional[str]:
    if session.get("status") != "active":
        return "replaced"
    session_business_day = _clean_text(session.get("business_day"))
    if business_day and session_business_day and session_business_day != business_day:
        return "daily_dreaming"
    return None


def _build_session_carryover_summary(
    conn: Connection,
    *,
    session_id: int,
    limit: int = 8,
) -> Optional[str]:
    rows = conn.execute(
        """
        SELECT role, content FROM messages
        WHERE session_id = ?
          AND content IS NOT NULL
          AND content != ''
          AND NOT (
            role = 'assistant'
            AND error IS NOT NULL
            AND error != ''
          )
          AND NOT (
            role = 'assistant'
            AND content = ?
          )
          AND (error IS NULL OR error != ?)
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, _NON_CONTEXT_ASSISTANT_REPLY, MODERATION_BLOCKED_ERROR, limit),
    ).fetchall()
    if not rows:
        return None

    lines = ["Recent carryover from previous session:"]
    for row in reversed(rows):
        role = "User" if row["role"] == "user" else "AI"
        content = _clean_text(row["content"]) or ""
        if len(content) > 400:
            content = content[:400] + "...[truncated]"
        lines.append(f"{role}: {content}")
    summary = "\n".join(lines)
    if len(summary) > 2400:
        summary = summary[:2400] + "...[truncated]"
    return summary


def increment_session_turn_count(
    *,
    session_id: int,
    count: int = 1,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    count = max(1, int(count))
    with _tx(conn) as tx:
        tx.execute(
            """
            UPDATE sessions
            SET turn_count = COALESCE(turn_count, 0) + ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE id = ?
            """,
            (count, session_id),
        )
        row = tx.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


