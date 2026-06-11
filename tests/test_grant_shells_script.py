import pytest


def _create_wallet_account(*, phone: str = "13800006001") -> dict:
    from app.db import create_ai4all_account_for_user, create_or_get_platform_user_by_phone

    user = create_or_get_platform_user_by_phone(phone=phone, display_name="Shell User")
    result = create_ai4all_account_for_user(
        platform_user_id=user["id"],
        display_name="Shell Bot",
    )
    return {
        "platform_user": user,
        "account": result["account"],
    }


def _ledger_rows(account_id: str) -> list[dict]:
    from app.db import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM entitlement_ledger
            WHERE account_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (account_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _audit_rows(account_id: str) -> list[dict]:
    from app.db import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM admin_access_events
            WHERE account_id = ?
            ORDER BY id ASC
            """,
            (account_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _wallet_balance(account_id: str) -> int:
    from app.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT balance_shell_micros FROM entitlement_wallets WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return int(row["balance_shell_micros"])


def test_parse_shell_amount_uses_micros():
    from scripts.grant_shells import GrantShellsError, parse_shell_amount

    assert parse_shell_amount("3000") == 3_000_000_000
    assert parse_shell_amount("1.25") == 1_250_000
    with pytest.raises(GrantShellsError):
        parse_shell_amount("0")
    with pytest.raises(GrantShellsError):
        parse_shell_amount("1.0000001")


def test_dry_run_does_not_write_ledger_or_audit(fresh_db):
    from scripts.grant_shells import run_grant

    created = _create_wallet_account()
    account_id = created["account"]["id"]

    result = run_grant(
        account_id=account_id,
        amount="3000",
        reason="运营手工赠送3000贝壳",
        operation_id="ops-test-dry-run",
        dry_run=True,
    )

    assert result["status"] == "would_apply"
    assert result["balance_before_shells"] == "1000"
    assert result["balance_after_shells"] == "4000"
    assert len(_ledger_rows(account_id)) == 1
    assert _audit_rows(account_id) == []
    assert _wallet_balance(account_id) == 1_000_000_000


def test_apply_grant_writes_wallet_ledger_and_audit(fresh_db):
    from scripts.grant_shells import run_grant

    created = _create_wallet_account(phone="13800006002")
    account_id = created["account"]["id"]

    result = run_grant(
        account_id=account_id,
        amount="3000",
        reason="运营手工赠送3000贝壳",
        operation_id="ops-test-apply",
        admin_user_id="admin",
        dry_run=False,
    )

    assert result["status"] == "applied"
    assert result["amount_shells"] == "3000"
    assert result["balance_before_shells"] == "1000"
    assert result["balance_after_shells"] == "4000"
    assert _wallet_balance(account_id) == 4_000_000_000

    rows = _ledger_rows(account_id)
    assert len(rows) == 2
    grant = next(row for row in rows if row["source_id"] == "ops-test-apply")
    assert grant["id"] == result["ledger_id"]
    assert grant["entry_type"] == "credit"
    assert grant["source_type"] == "manual_grant"
    assert grant["source_id"] == "ops-test-apply"
    assert grant["amount_shell_micros"] == 3_000_000_000
    assert grant["balance_after_shell_micros"] == 4_000_000_000

    audits = _audit_rows(account_id)
    assert len(audits) == 1
    assert audits[0]["admin_user_id"] == "admin"
    assert audits[0]["action"] == "wallet.manual_grant"
    assert audits[0]["resource_type"] == "entitlement_ledger"
    assert audits[0]["resource_id"] == result["ledger_id"]
    assert audits[0]["plaintext"] == 0
    assert audits[0]["reason"] == "运营手工赠送3000贝壳"


def test_replay_is_idempotent_and_does_not_duplicate_audit(fresh_db):
    from scripts.grant_shells import run_grant

    created = _create_wallet_account(phone="13800006003")
    account_id = created["account"]["id"]

    first = run_grant(
        account_id=account_id,
        amount="3000",
        reason="运营手工赠送3000贝壳",
        operation_id="ops-test-replay",
        dry_run=False,
    )
    second = run_grant(
        account_id=account_id,
        amount="3000",
        reason="运营手工赠送3000贝壳",
        operation_id="ops-test-replay",
        dry_run=False,
    )

    assert first["status"] == "applied"
    assert second["status"] == "already_applied"
    assert second["ledger_id"] == first["ledger_id"]
    assert second["audit_event"]["created"] is False
    assert _wallet_balance(account_id) == 4_000_000_000
    assert len(_ledger_rows(account_id)) == 2
    assert len(_audit_rows(account_id)) == 1


def test_replay_repairs_missing_audit_without_changing_balance(fresh_db):
    from app.db import connect
    from scripts.grant_shells import run_grant

    created = _create_wallet_account(phone="13800006004")
    account_id = created["account"]["id"]

    first = run_grant(
        account_id=account_id,
        amount="3000",
        reason="运营手工赠送3000贝壳",
        operation_id="ops-test-audit-repair",
        dry_run=False,
    )
    with connect() as conn:
        conn.execute("DELETE FROM admin_access_events WHERE resource_id = ?", (first["ledger_id"],))

    second = run_grant(
        account_id=account_id,
        amount="3000",
        reason="运营手工赠送3000贝壳",
        operation_id="ops-test-audit-repair",
        dry_run=False,
    )

    assert second["status"] == "already_applied"
    assert second["audit_event"]["created"] is True
    assert _wallet_balance(account_id) == 4_000_000_000
    assert len(_ledger_rows(account_id)) == 2
    assert len(_audit_rows(account_id)) == 1


def test_wallet_owner_mismatch_aborts(fresh_db):
    from app.db import connect, create_or_get_platform_user_by_phone
    from scripts.grant_shells import GrantShellsError, run_grant

    created = _create_wallet_account(phone="13800006005")
    account_id = created["account"]["id"]
    other = create_or_get_platform_user_by_phone(phone="13800006006")
    with connect() as conn:
        conn.execute(
            """
            UPDATE entitlement_wallets
            SET platform_user_id = ?
            WHERE account_id = ?
            """,
            (other["id"], account_id),
        )

    with pytest.raises(GrantShellsError):
        run_grant(
            account_id=account_id,
            amount="3000",
            reason="运营手工赠送3000贝壳",
            operation_id="ops-test-owner-mismatch",
            dry_run=True,
        )
