"""
Safely grant shells to one AI4ALL account through wallet ledger entries.

Preview:

    .venv/bin/python scripts/grant_shells.py \
      --account aid_123456789 --amount 3000 --reason "运营手工赠送3000贝壳" --dry-run

Apply:

    .venv/bin/python scripts/grant_shells.py \
      --account aid_123456789 --amount 3000 --reason "运营手工赠送3000贝壳" --yes

The script never updates wallet balances directly. It writes one
entitlement_ledger credit via app.db.grant_shells() and records the
operation in admin_access_events.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Allow running from repo root or scripts/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from app.bootstrap.product_registry import (
        PRODUCTION_PRODUCT_REGISTRY,
        ProductRegistry,
    )
    from app.db import (
        SHELL_MICROS_PER_SHELL,
        connect,
        get_account,
        grant_shells as db_grant_shells,
        insert_admin_access_event,
    )
except ModuleNotFoundError as exc:
    missing = exc.name or str(exc)
    print(
        f"缺少 Python 依赖：{missing}\n"
        "请在仓库根目录使用项目虚拟环境运行：\n"
        "  .venv/bin/python scripts/grant_shells.py ...",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


SOURCE_TYPES = ("manual_grant", "compensation")


class GrantShellsError(ValueError):
    """Raised when a grant request fails validation or safety checks."""


@dataclass(frozen=True)
class GrantOperation:
    """Validated shell grant operation ready for preview or application."""

    account_id: str
    app_id: str
    platform_user_id: str
    amount_shell_micros: int
    reason: str
    admin_user_id: str
    source_type: str
    source_id: str
    idempotency_key: str
    wallet_id: Optional[str]
    current_balance_shell_micros: int


def format_shell_micros(amount_shell_micros: int) -> str:
    """Format shell micros using the same visible precision as wallet APIs."""

    sign = "-" if amount_shell_micros < 0 else ""
    amount = abs(int(amount_shell_micros))
    whole = amount // SHELL_MICROS_PER_SHELL
    fraction = amount % SHELL_MICROS_PER_SHELL
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def parse_shell_amount(value: str) -> int:
    """Parse a positive shell amount into micros without floating point drift."""

    text = str(value or "").strip()
    if not text:
        raise GrantShellsError("--amount is required")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise GrantShellsError(f"--amount must be a number, got: {value}") from exc
    if amount <= 0:
        raise GrantShellsError("--amount must be positive")

    micros = amount * Decimal(SHELL_MICROS_PER_SHELL)
    if micros != micros.to_integral_value():
        raise GrantShellsError("--amount supports at most 6 decimal places")
    return int(micros)


def default_operation_id(
    *,
    account_id: str,
    amount_shell_micros: int,
    reason: str,
    source_type: str,
    today: Optional[str] = None,
) -> str:
    """Build a deterministic default operation id for same-command replays."""

    date_text = today or datetime.now().date().isoformat()
    amount_text = format_shell_micros(amount_shell_micros).replace(".", "p")
    reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:10]
    return f"{source_type}-{account_id}-{amount_text}-shells-{date_text}-{reason_hash}"


def build_operation_ids(
    *,
    account_id: str,
    amount_shell_micros: int,
    reason: str,
    source_type: str,
    operation_id: Optional[str],
    source_id: Optional[str],
    idempotency_key: Optional[str],
) -> Dict[str, str]:
    """Resolve source_id and idempotency_key from explicit or default inputs."""

    cleaned_operation_id = (operation_id or "").strip()
    cleaned_source_id = (source_id or "").strip()
    cleaned_idempotency_key = (idempotency_key or "").strip()
    if cleaned_operation_id and cleaned_source_id:
        raise GrantShellsError("--operation-id and --source-id cannot both be set")

    resolved_source_id = (
        cleaned_source_id
        or cleaned_operation_id
        or default_operation_id(
            account_id=account_id,
            amount_shell_micros=amount_shell_micros,
            reason=reason,
            source_type=source_type,
        )
    )
    resolved_idempotency_key = (
        cleaned_idempotency_key
        or f"wallet-grant-{source_type}-{resolved_source_id}"
    )
    return {
        "source_id": resolved_source_id,
        "idempotency_key": resolved_idempotency_key,
    }


def _fetch_active_owner_bindings(
    account_id: str, app_id: str
) -> List[Dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, platform_user_id, account_id, app_id, binding_method, status,
                   verified_at, created_at, updated_at
            FROM account_owner_bindings
            WHERE account_id = ?
              AND app_id = ?
              AND status = 'active'
            ORDER BY created_at ASC, id ASC
            """,
            (account_id, app_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _fetch_platform_user(platform_user_id: str) -> Optional[Dict[str, Any]]:
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


def _fetch_wallet(account_id: str, app_id: str) -> Optional[Dict[str, Any]]:
    """按 account_id 取钱包行（仅用于孤儿号 owner 解析的兜底：无绑定号的钱包 account_id==自身）。

    D-14 钱包已上迁真人级，故权威钱包按 owner platform_user_id 解析（见
    _fetch_wallet_by_platform_user）；本函数只服务 resolve_platform_user_id 的 wallet_platform_user_id
    兜底入参，不作预览余额来源。
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, account_id, platform_user_id, app_id, balance_shell_micros,
                   status, created_at, updated_at
            FROM entitlement_wallets
            WHERE account_id = ? AND app_id = ?
            """,
            (account_id, app_id),
        ).fetchone()
    return dict(row) if row else None


def _fetch_wallet_by_platform_user(
    platform_user_id: str, app_id: str
) -> Optional[Dict[str, Any]]:
    """按 owner 与 app_id 取权威 active 钱包（MP-02 产品级；镜像 get_wallet_summary）。

    同真人多个 account 共享一钱包、其 account_id 恒指向首号——故第 2 个号必须按 platform_user_id
    解析，否则按 account_id 查漏空、dry-run 预览余额错记 0。
    """
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, account_id, platform_user_id, app_id, balance_shell_micros,
                   status, created_at, updated_at
            FROM entitlement_wallets
            WHERE platform_user_id = ? AND app_id = ? AND status = 'active'
            """,
            (platform_user_id, app_id),
        ).fetchone()
    return dict(row) if row else None


def _fetch_ledger_by_idempotency_key(
    idempotency_key: str, app_id: str
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM entitlement_ledger
            WHERE app_id = ? AND idempotency_key = ?
            """,
            (app_id, idempotency_key),
        ).fetchone()
    return dict(row) if row else None


def _fetch_audit_event(*, action: str, ledger_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM admin_access_events
            WHERE action = ?
              AND resource_type = 'entitlement_ledger'
              AND resource_id = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (action, ledger_id),
        ).fetchone()
    return dict(row) if row else None


def resolve_platform_user_id(
    *,
    account_id: str,
    app_id: str,
    requested_platform_user_id: Optional[str] = None,
    wallet_platform_user_id: Optional[str] = None,
) -> str:
    """Resolve the active owner platform_user_id for an account."""

    active_bindings = _fetch_active_owner_bindings(account_id, app_id)
    requested = (requested_platform_user_id or "").strip()
    if requested:
        user = _fetch_platform_user(requested)
        if user is None:
            raise GrantShellsError(f"platform_user not found: {requested}")
        if active_bindings and requested not in {
            binding["platform_user_id"] for binding in active_bindings
        }:
            raise GrantShellsError(
                "requested platform_user_id is not an active owner of this account"
            )
        if not active_bindings and requested != (wallet_platform_user_id or ""):
            raise GrantShellsError(
                "account has no active owner binding and requested platform_user_id "
                "does not match an existing wallet owner"
            )
        return requested

    if not active_bindings:
        raise GrantShellsError(
            "account has no active owner binding; pass --platform-user-id only after manual verification"
        )
    owner_ids = list(dict.fromkeys(row["platform_user_id"] for row in active_bindings))
    if len(owner_ids) > 1:
        raise GrantShellsError(
            "account has multiple active owners; pass --platform-user-id explicitly"
        )
    return owner_ids[0]


def prepare_operation(
    *,
    account_id: str,
    amount_shell_micros: int,
    reason: str,
    admin_user_id: str,
    source_type: str = "manual_grant",
    platform_user_id: Optional[str] = None,
    operation_id: Optional[str] = None,
    source_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    allow_inactive_account: bool = False,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> GrantOperation:
    """Validate account ownership and return a grant operation."""

    cleaned_account_id = str(account_id or "").strip()
    cleaned_reason = str(reason or "").strip()
    cleaned_admin_user_id = str(admin_user_id or "").strip() or "admin"
    cleaned_source_type = str(source_type or "").strip()
    if not cleaned_account_id:
        raise GrantShellsError("--account is required")
    if amount_shell_micros <= 0:
        raise GrantShellsError("amount_shell_micros must be positive")
    if not cleaned_reason:
        raise GrantShellsError("--reason is required")
    if cleaned_source_type not in SOURCE_TYPES:
        raise GrantShellsError(f"--source-type must be one of: {', '.join(SOURCE_TYPES)}")

    account = get_account(account_id=cleaned_account_id)
    if account is None:
        raise GrantShellsError(f"account not found: {cleaned_account_id}")
    try:
        resolved_app_id = registry.require_enabled(
            str(account.get("app_id") or "")
        ).app_id
    except ValueError as exc:
        raise GrantShellsError(str(exc)) from exc
    if account.get("status") != "active" and not allow_inactive_account:
        raise GrantShellsError(
            f"account status is {account.get('status')}; pass --allow-inactive-account to override"
        )

    # 先按 account_id 取钱包，仅为给 resolve_platform_user_id 提供孤儿号兜底（无绑定号钱包
    # account_id==自身）。owner 解析后再按 platform_user_id 取**权威** active 钱包作预览余额来源
    # ——D-14 钱包已上迁真人级，同真人第 2 个号按 account_id 查会漏空、dry-run 预览余额错记 0。
    account_wallet = _fetch_wallet(cleaned_account_id, resolved_app_id)
    resolved_platform_user_id = resolve_platform_user_id(
        account_id=cleaned_account_id,
        app_id=resolved_app_id,
        requested_platform_user_id=platform_user_id,
        wallet_platform_user_id=account_wallet["platform_user_id"] if account_wallet else None,
    )
    wallet = (
        _fetch_wallet_by_platform_user(resolved_platform_user_id, resolved_app_id)
        or account_wallet
    )
    if wallet and wallet["platform_user_id"] != resolved_platform_user_id:
        raise GrantShellsError(
            "wallet platform_user_id does not match resolved account owner; aborting"
        )
    if wallet and wallet["app_id"] != resolved_app_id:
        raise GrantShellsError("wallet app_id does not match account app; aborting")

    ids = build_operation_ids(
        account_id=cleaned_account_id,
        amount_shell_micros=amount_shell_micros,
        reason=cleaned_reason,
        source_type=cleaned_source_type,
        operation_id=operation_id,
        source_id=source_id,
        idempotency_key=idempotency_key,
    )
    return GrantOperation(
        account_id=cleaned_account_id,
        app_id=resolved_app_id,
        platform_user_id=resolved_platform_user_id,
        amount_shell_micros=amount_shell_micros,
        reason=cleaned_reason,
        admin_user_id=cleaned_admin_user_id,
        source_type=cleaned_source_type,
        source_id=ids["source_id"],
        idempotency_key=ids["idempotency_key"],
        wallet_id=wallet["id"] if wallet else None,
        current_balance_shell_micros=int(wallet["balance_shell_micros"]) if wallet else 0,
    )


def _validate_existing_ledger(operation: GrantOperation, ledger: Dict[str, Any]) -> None:
    if ledger["app_id"] != operation.app_id:
        raise GrantShellsError("idempotency_key already belongs to another app")
    if ledger["account_id"] != operation.account_id:
        raise GrantShellsError("idempotency_key already belongs to another account")
    if ledger["platform_user_id"] != operation.platform_user_id:
        raise GrantShellsError("idempotency_key already belongs to another platform_user")
    if ledger["entry_type"] != "credit":
        raise GrantShellsError("idempotency_key already belongs to a non-credit ledger")
    if ledger["source_type"] != operation.source_type:
        raise GrantShellsError("idempotency_key already belongs to a different source_type")
    if ledger["source_id"] != operation.source_id:
        raise GrantShellsError("idempotency_key already belongs to a different source_id")
    if int(ledger["amount_shell_micros"]) != operation.amount_shell_micros:
        raise GrantShellsError("idempotency_key already belongs to a different amount")


def _audit_metadata(operation: GrantOperation, ledger: Dict[str, Any]) -> Dict[str, Any]:
    amount = int(ledger["amount_shell_micros"])
    balance_after = int(ledger["balance_after_shell_micros"])
    balance_before = balance_after - amount
    return {
        "script": "scripts/grant_shells.py",
        "account_id": operation.account_id,
        "app_id": operation.app_id,
        "platform_user_id": operation.platform_user_id,
        "source_type": operation.source_type,
        "source_id": operation.source_id,
        "idempotency_key": operation.idempotency_key,
        "amount_shells": format_shell_micros(amount),
        "amount_shell_micros": amount,
        "balance_before_shells": format_shell_micros(balance_before),
        "balance_before_shell_micros": balance_before,
        "balance_after_shells": format_shell_micros(balance_after),
        "balance_after_shell_micros": balance_after,
    }


def ensure_audit_event(operation: GrantOperation, ledger: Dict[str, Any]) -> Dict[str, Any]:
    """Create the admin audit event for a ledger row if it is missing."""

    action = f"wallet.{operation.source_type}"
    existing = _fetch_audit_event(action=action, ledger_id=ledger["id"])
    if existing is not None:
        return {
            "id": existing["id"],
            "created": False,
            "action": existing["action"],
        }
    event_id = insert_admin_access_event(
        admin_user_id=operation.admin_user_id,
        action=action,
        resource_type="entitlement_ledger",
        resource_id=ledger["id"],
        account_id=operation.account_id,
        plaintext=False,
        reason=operation.reason,
        request_path="scripts/grant_shells.py",
        metadata=_audit_metadata(operation, ledger),
    )
    return {
        "id": event_id,
        "created": True,
        "action": action,
    }


def grant_operation(
    operation: GrantOperation,
    *,
    dry_run: bool,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
) -> Dict[str, Any]:
    """Preview or apply a validated grant operation."""

    try:
        registry.require_enabled(operation.app_id)
    except ValueError as exc:
        raise GrantShellsError(str(exc)) from exc
    existing = _fetch_ledger_by_idempotency_key(
        operation.idempotency_key, operation.app_id
    )
    if existing is not None:
        _validate_existing_ledger(operation, existing)
        audit = None if dry_run else ensure_audit_event(operation, existing)
        return {
            "status": "already_applied",
            "dry_run": dry_run,
            "account_id": operation.account_id,
            "app_id": operation.app_id,
            "platform_user_id": operation.platform_user_id,
            "source_type": operation.source_type,
            "source_id": operation.source_id,
            "idempotency_key": operation.idempotency_key,
            "amount_shells": format_shell_micros(operation.amount_shell_micros),
            "amount_shell_micros": operation.amount_shell_micros,
            "balance_before_shells": format_shell_micros(
                int(existing["balance_after_shell_micros"])
                - int(existing["amount_shell_micros"])
            ),
            "balance_after_shells": format_shell_micros(
                int(existing["balance_after_shell_micros"])
            ),
            "ledger_id": existing["id"],
            "audit_event": audit,
        }

    balance_after = operation.current_balance_shell_micros + operation.amount_shell_micros
    if dry_run:
        return {
            "status": "would_apply",
            "dry_run": True,
            "account_id": operation.account_id,
            "app_id": operation.app_id,
            "platform_user_id": operation.platform_user_id,
            "source_type": operation.source_type,
            "source_id": operation.source_id,
            "idempotency_key": operation.idempotency_key,
            "amount_shells": format_shell_micros(operation.amount_shell_micros),
            "amount_shell_micros": operation.amount_shell_micros,
            "balance_before_shells": format_shell_micros(
                operation.current_balance_shell_micros
            ),
            "balance_after_shells": format_shell_micros(balance_after),
            "ledger_id": None,
            "audit_event": None,
        }

    ledger = db_grant_shells(
        account_id=operation.account_id,
        platform_user_id=operation.platform_user_id,
        amount_shell_micros=operation.amount_shell_micros,
        source_type=operation.source_type,
        source_id=operation.source_id,
        idempotency_key=operation.idempotency_key,
        metadata={
            "reason": operation.reason,
            "app_id": operation.app_id,
            "amount_shells": format_shell_micros(operation.amount_shell_micros),
            "amount_shell_micros": operation.amount_shell_micros,
            "operator": operation.admin_user_id,
            "script": "scripts/grant_shells.py",
        },
        registry=registry,
    )
    audit = ensure_audit_event(operation, ledger)
    return {
        "status": "applied",
        "dry_run": False,
        "account_id": operation.account_id,
        "app_id": operation.app_id,
        "platform_user_id": operation.platform_user_id,
        "source_type": operation.source_type,
        "source_id": operation.source_id,
        "idempotency_key": operation.idempotency_key,
        "amount_shells": ledger["amount_shells"],
        "amount_shell_micros": ledger["amount_shell_micros"],
        "balance_before_shells": format_shell_micros(
            int(ledger["balance_after_shell_micros"]) - int(ledger["amount_shell_micros"])
        ),
        "balance_after_shells": ledger["balance_after_shells"],
        "ledger_id": ledger["id"],
        "audit_event": audit,
    }


def run_grant(
    *,
    account_id: str,
    amount: str,
    reason: str,
    admin_user_id: str = "admin",
    source_type: str = "manual_grant",
    platform_user_id: Optional[str] = None,
    operation_id: Optional[str] = None,
    source_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    allow_inactive_account: bool = False,
    registry: ProductRegistry = PRODUCTION_PRODUCT_REGISTRY,
    dry_run: bool,
) -> Dict[str, Any]:
    """Validate CLI-style inputs and preview or apply the grant."""

    amount_shell_micros = parse_shell_amount(amount)
    operation = prepare_operation(
        account_id=account_id,
        amount_shell_micros=amount_shell_micros,
        reason=reason,
        admin_user_id=admin_user_id,
        source_type=source_type,
        platform_user_id=platform_user_id,
        operation_id=operation_id,
        source_id=source_id,
        idempotency_key=idempotency_key,
        allow_inactive_account=allow_inactive_account,
        registry=registry,
    )
    return grant_operation(operation, dry_run=dry_run, registry=registry)


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""

    parser = argparse.ArgumentParser(description="Grant shells through entitlement ledger.")
    parser.add_argument("--account", required=True, help="AI4ALL account id, e.g. aid_123456789")
    parser.add_argument("--amount", required=True, help="Shell amount, e.g. 3000 or 1.5")
    parser.add_argument("--reason", required=True, help="Operational reason recorded in ledger/audit")
    parser.add_argument("--admin-user", default="admin", help="admin_access_events.admin_user_id")
    parser.add_argument(
        "--source-type",
        default="manual_grant",
        choices=SOURCE_TYPES,
        help="Ledger source_type. Use compensation for customer-service compensation.",
    )
    parser.add_argument("--platform-user-id", help="Override active owner resolution after verification")
    parser.add_argument("--operation-id", help="Human operation id; used as source_id by default")
    parser.add_argument("--source-id", help="Ledger source_id. Cannot be combined with --operation-id")
    parser.add_argument("--idempotency-key", help="Explicit ledger idempotency_key")
    parser.add_argument(
        "--allow-inactive-account",
        action="store_true",
        help="Allow grants to non-active accounts after manual verification",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview without writing ledger/audit")
    mode.add_argument("--yes", action="store_true", help="Apply the grant")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def print_human_result(result: Dict[str, Any]) -> None:
    """Print a compact human-readable operation result."""

    print(f"status: {result['status']}")
    print(f"account_id: {result['account_id']}")
    print(f"platform_user_id: {result['platform_user_id']}")
    print(f"source_type: {result['source_type']}")
    print(f"source_id: {result['source_id']}")
    print(f"idempotency_key: {result['idempotency_key']}")
    print(f"amount_shells: {result['amount_shells']}")
    print(f"balance: {result['balance_before_shells']} -> {result['balance_after_shells']}")
    print(f"ledger_id: {result['ledger_id'] or '-'}")
    audit = result.get("audit_event")
    if audit:
        print(
            "audit_event: "
            f"{audit['id']} ({audit['action']}, created={str(audit['created']).lower()})"
        )
    else:
        print("audit_event: -")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_grant(
            account_id=args.account,
            amount=args.amount,
            reason=args.reason,
            admin_user_id=args.admin_user,
            source_type=args.source_type,
            platform_user_id=args.platform_user_id,
            operation_id=args.operation_id,
            source_id=args.source_id,
            idempotency_key=args.idempotency_key,
            allow_inactive_account=args.allow_inactive_account,
            dry_run=args.dry_run,
        )
    except GrantShellsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_human_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
