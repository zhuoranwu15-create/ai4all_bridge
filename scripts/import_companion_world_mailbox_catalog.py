#!/usr/bin/env python3
"""校验并导入签名 mailbox catalog manifest；不内置任何运营角色内容。"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Mapping, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_CATALOG_ID_RE = re.compile(r"^lcat_[A-Za-z0-9_-]{1,120}$")
_CHARACTER_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class MailboxCatalogManifestRecord:
    """一条已验签、已校验的不可变 catalog 记录。"""

    catalog_id: str
    character_key: str
    character_template_id: str
    template_version: str
    letter_body: str
    policy_version: str
    priority: int
    available_from: str | None
    available_until: str | None


@dataclass
class MailboxCatalogImportReport:
    """导入报告；不输出 letter body、签名或 HMAC secret。"""

    dry_run: bool
    create_ids: List[str]
    keep_ids: List[str]
    retire_ids: List[str]
    errors: List[str]
    catalog_ready: bool = False


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        {"version": payload.get("version"), "entries": payload.get("entries")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_manifest_payload(payload: Mapping[str, Any], *, secret: str) -> str:
    """返回 manifest canonical payload 的 HMAC-SHA256 hex；仅供受控工具/测试。"""
    clean_secret = str(secret or "")
    if not clean_secret:
        raise ValueError("mailbox manifest HMAC secret is required")
    return hmac.new(
        clean_secret.encode("utf-8"),
        _canonical_payload(payload),
        hashlib.sha256,
    ).hexdigest()


def _optional_time(value: object, field: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError as err:
        raise ValueError(f"{field} must be YYYY-MM-DD HH:MM:SS") from err
    return text


def validate_signed_manifest(
    payload: Any, *, secret: str
) -> Tuple[MailboxCatalogManifestRecord, ...]:
    """验签并校验 manifest；任一未知/缺失内容都 fail-closed。"""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "entries", "signature"}
        or payload.get("version") != 1
    ):
        raise ValueError("mailbox manifest version must be 1")
    entries = payload.get("entries")
    signature = str(payload.get("signature") or "").strip().lower()
    if not isinstance(entries, list) or not entries:
        raise ValueError("mailbox manifest entries must be a non-empty list")
    expected = sign_manifest_payload(payload, secret=secret)
    if not signature or not hmac.compare_digest(signature, expected):
        raise ValueError("mailbox manifest signature mismatch")
    records: list[MailboxCatalogManifestRecord] = []
    allowed_keys = {
        "catalog_id",
        "character_key",
        "character_template_id",
        "template_version",
        "letter_body",
        "policy_version",
        "priority",
        "available_from",
        "available_until",
    }
    for item in entries:
        if not isinstance(item, dict) or not set(item).issubset(allowed_keys):
            raise ValueError("mailbox manifest entry has unknown fields")
        catalog_id = str(item.get("catalog_id") or "").strip()
        character_key = str(item.get("character_key") or "").strip()
        template_id = str(item.get("character_template_id") or "").strip()
        template_version = str(item.get("template_version") or "").strip()
        letter_body = str(item.get("letter_body") or "").strip()
        policy_version = str(item.get("policy_version") or "").strip()
        if not _CATALOG_ID_RE.fullmatch(catalog_id):
            raise ValueError(f"invalid catalog_id: {catalog_id!r}")
        if not _CHARACTER_KEY_RE.fullmatch(character_key):
            raise ValueError(f"invalid character_key: {character_key!r}")
        if not template_id or not template_version or not policy_version:
            raise ValueError(f"catalog {catalog_id} has incomplete version metadata")
        if not letter_body or len(letter_body) > 2000:
            raise ValueError(f"catalog {catalog_id} has invalid letter body")
        priority = int(item.get("priority") or 0)
        if not -1000 <= priority <= 1000:
            raise ValueError(f"catalog {catalog_id} priority is out of range")
        available_from = _optional_time(item.get("available_from"), "available_from")
        available_until = _optional_time(item.get("available_until"), "available_until")
        if available_from and available_until and available_from >= available_until:
            raise ValueError(f"catalog {catalog_id} availability window is invalid")
        records.append(
            MailboxCatalogManifestRecord(
                catalog_id=catalog_id,
                character_key=character_key,
                character_template_id=template_id,
                template_version=template_version,
                letter_body=letter_body,
                policy_version=policy_version,
                priority=priority,
                available_from=available_from,
                available_until=available_until,
            )
        )
    if len({record.catalog_id for record in records}) != len(records):
        raise ValueError("mailbox manifest catalog_id values must be unique")
    if len({record.character_key for record in records}) != len(records):
        raise ValueError("mailbox manifest character_key values must be unique")
    if len({record.character_template_id for record in records}) != len(records):
        raise ValueError("mailbox manifest template ids must be unique")
    return tuple(records)


def load_signed_manifest(
    path: str, *, secret: str
) -> Tuple[MailboxCatalogManifestRecord, ...]:
    """读取 UTF-8 JSON 并执行验签/校验。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_signed_manifest(payload, secret=secret)


def _immutable_matches(row: Mapping[str, Any], record: MailboxCatalogManifestRecord) -> bool:
    return all(
        (
            str(row["character_key"]) == record.character_key,
            str(row["character_template_id"]) == record.character_template_id,
            str(row["template_version"]) == record.template_version,
            str(row["letter_body"]) == record.letter_body,
            str(row["policy_version"]) == record.policy_version,
            int(row["priority"]) == record.priority,
            row.get("available_from") == record.available_from,
            row.get("available_until") == record.available_until,
        )
    )


def inspect_import(
    conn, records: Sequence[MailboxCatalogManifestRecord], *, dry_run: bool
) -> MailboxCatalogImportReport:
    """只读计算 create/keep/retire，并检查模板与已发布内容不可变。"""
    desired = {record.catalog_id: record for record in records}
    create_ids: list[str] = []
    keep_ids: list[str] = []
    retire_ids: list[str] = []
    errors: list[str] = []
    for record in records:
        template = conn.execute(
            "SELECT * FROM character_templates WHERE id = ?",
            (record.character_template_id,),
        ).fetchone()
        if (
            template is None
            or template["status"] != "active"
            or template["source_type"] not in {"official", "operations"}
            or str(template["persona_version"]) != record.template_version
        ):
            errors.append(f"{record.catalog_id}: template unavailable or version mismatch")
            continue
        existing = conn.execute(
            "SELECT * FROM character_letter_catalog WHERE id = ?",
            (record.catalog_id,),
        ).fetchone()
        if existing is None:
            create_ids.append(record.catalog_id)
        elif existing["status"] != "active":
            errors.append(f"{record.catalog_id}: retired catalog id cannot be reactivated")
        elif not _immutable_matches(dict(existing), record):
            errors.append(f"{record.catalog_id}: published catalog is immutable; use a new id")
        else:
            keep_ids.append(record.catalog_id)
        conflicts = conn.execute(
            """
            SELECT id FROM character_letter_catalog
            WHERE id <> ? AND (
                (character_key = ? AND template_version = ?)
                OR character_template_id = ?
            )
            """,
            (
                record.catalog_id,
                record.character_key,
                record.template_version,
                record.character_template_id,
            ),
        ).fetchall()
        if conflicts:
            errors.append(f"{record.catalog_id}: character/version or template already published")
        active_old = conn.execute(
            "SELECT id FROM character_letter_catalog "
            "WHERE character_key = ? AND status = 'active' AND id <> ?",
            (record.character_key, record.catalog_id),
        ).fetchall()
        retire_ids.extend(str(row["id"]) for row in active_old)
    return MailboxCatalogImportReport(
        dry_run=dry_run,
        create_ids=sorted(set(create_ids)),
        keep_ids=sorted(set(keep_ids)),
        retire_ids=sorted(set(retire_ids)),
        errors=errors,
    )


def import_mailbox_catalog(
    records: Sequence[MailboxCatalogManifestRecord],
    *,
    dry_run: bool,
    actor: str,
) -> MailboxCatalogImportReport:
    """在单事务内 retire/create；dry-run 完全只读。"""
    from app.db import (
        connect,
        create_character_letter_catalog_entry,
        retire_character_letter_catalog_entry,
    )
    from app.products.mingchan.application.mailbox import build_mailbox_policy
    from app.time_utils import beijing_now_str

    with connect() as conn:
        report = inspect_import(conn, records, dry_run=dry_run)
        expected_policy = build_mailbox_policy().version
        report.errors.extend(
            f"{record.catalog_id}: policy_version must be {expected_policy}"
            for record in records
            if record.policy_version != expected_policy
        )
        if report.errors or dry_run:
            report.catalog_ready = not report.errors and not report.create_ids
            return report
        now = beijing_now_str()
        for catalog_id in report.retire_ids:
            retire_character_letter_catalog_entry(
                catalog_id=catalog_id,
                retired_by=actor,
                retired_at=now,
                conn=conn,
            )
        by_id = {record.catalog_id: record for record in records}
        for catalog_id in report.create_ids:
            record = by_id[catalog_id]
            create_character_letter_catalog_entry(
                catalog_id=record.catalog_id,
                character_key=record.character_key,
                character_template_id=record.character_template_id,
                template_version=record.template_version,
                letter_body=record.letter_body,
                policy_version=record.policy_version,
                priority=record.priority,
                created_by=actor,
                available_from=record.available_from,
                available_until=record.available_until,
                conn=conn,
            )
        active_ids = {
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM character_letter_catalog WHERE status = 'active'"
            ).fetchall()
        }
        report.catalog_ready = set(by_id).issubset(active_ids)
        if not report.catalog_ready:
            raise RuntimeError("mailbox catalog precheck failed after import")
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="导入签名 Companion World mailbox catalog")
    parser.add_argument("manifest", help="签名 UTF-8 JSON manifest 路径")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只读规划，不写数据库")
    mode.add_argument("--apply", action="store_true", help="执行 retire/create 单事务")
    parser.add_argument("--actor", default="mailbox-manifest-import", help="审计 actor id")
    parser.add_argument("--database-url", default=None, help="可选覆盖 DATABASE_URL")
    args = parser.parse_args()
    from app.config import settings

    if args.database_url is not None:
        settings.database_url = args.database_url
    secret = settings.mingchan_mailbox_manifest_hmac_secret
    records = load_signed_manifest(args.manifest, secret=secret)
    report = import_mailbox_catalog(records, dry_run=args.dry_run, actor=args.actor)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 1 if report.errors or (args.apply and not report.catalog_ready) else 0


if __name__ == "__main__":
    raise SystemExit(main())
