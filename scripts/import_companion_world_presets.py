#!/usr/bin/env python3
"""导入/预检 Companion World 首发模板目录；默认可用 ``--dry-run`` 只读规划。

manifest 不包含代码内置人设，必须显式提供连续的 rank 1..N（N ≥
``MIN_INITIAL_CANDIDATES``，与运行时 ``_validate_initial_catalog`` 同一口径，
故加一位预设是纯数据操作、不需要改代码）。已存在 template_id 的已发布
字段必须逐项相同；内容变更须换新 template_id，脚本会在同一事务退休旧目录并插入新版本。

**运营元数据是可原地更新的例外**（m0049 / NAME-001、CAND-001）：``name_pool``、
``name_pool_version``、``long_summary`` 不参与人设内容的不可变判定，可以对已发布模板
补配和调整——生产四模板早已上线、id 不能换，否则名池永远配不上去。``persona_key``
介于两者之间：允许从空补上，但一旦非空就不许改值（改了会破坏跨模板版本的身份连续性）。
"""
import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.products.zhaoxi.domain.companion_world.naming import (  # noqa: E402
    NamePoolError,
    normalize_name_pool,
)
from app.products.zhaoxi.domain.companion_world.service import (  # noqa: E402
    MIN_INITIAL_CANDIDATES,
)

_TEMPLATE_ID_RE = re.compile(r"^tmpl_[A-Za-z0-9_-]{1,120}$")


@dataclass(frozen=True)
class PresetRecord:
    """一条已校验、可稳定重放的运营模板记录。"""

    template_id: str
    rank: int
    name: str
    avatar_ref: str
    summary: str
    tags: Tuple[str, str, str]
    persona_seed_json: str
    persona_version: str
    # 以下为可选运营元数据；未提供时保持既有值不变（不会把已配好的名池清空）。
    persona_key: Optional[str] = None
    long_summary: Optional[str] = None
    name_pool: Tuple[str, ...] = ()
    name_pool_version: Optional[str] = None


@dataclass
class ImportReport:
    """导入规划/结果；不包含 persona 正文。"""

    dry_run: bool
    create_ids: List[str]
    keep_ids: List[str]
    retire_ids: List[str]
    errors: List[str]
    catalog_ready: bool = False
    # 已发布模板上被原地更新的运营元数据（名池/长介绍/首次补 persona_key）。
    update_ids: List[str] = field(default_factory=list)


def _persona_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as err:
            raise ValueError("persona_seed_json must be valid JSON") from err
    else:
        parsed = value
    if not isinstance(parsed, dict):
        raise ValueError("persona_seed_json must be an object")
    soul = parsed.get("SOUL.md", parsed.get("soul"))
    identity = parsed.get("IDENTITY.md", parsed.get("identity"))
    if not all(isinstance(item, str) and item.strip() for item in (soul, identity)):
        raise ValueError("persona_seed_json requires non-empty SOUL.md and IDENTITY.md")
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def _name_pool(template_id: str, item: Dict[str, Any]) -> Tuple[str, ...]:
    """读取并校验可选名池；校验规则单点落在领域层 ``normalize_name_pool``。"""
    raw = item.get("name_pool")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"template {template_id}: name_pool must be a list")
    try:
        return normalize_name_pool(raw)
    except NamePoolError as err:
        raise ValueError(f"template {template_id}: {err}") from err


def validate_manifest(payload: Any) -> Tuple[PresetRecord, ...]:
    """校验 manifest 含连续 rank 1..N（N ≥ 下限），返回按 rank 排序的不可变记录。"""
    items = payload.get("templates") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or len(items) < MIN_INITIAL_CANDIDATES:
        raise ValueError(
            f"manifest must contain at least {MIN_INITIAL_CANDIDATES} templates"
        )
    records: List[PresetRecord] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each template must be an object")
        template_id = str(item.get("template_id") or "").strip()
        if not _TEMPLATE_ID_RE.fullmatch(template_id):
            raise ValueError(f"invalid template_id: {template_id!r}")
        rank = int(item.get("initial_candidate_rank") or 0)
        name = str(item.get("name") or "").strip()
        avatar_ref = str(item.get("avatar_ref") or "").strip()
        summary = str(item.get("summary") or "").strip()
        persona_version = str(item.get("persona_version") or "").strip()
        tags_raw = item.get("tags")
        if not isinstance(tags_raw, list) or len(tags_raw) != 3:
            raise ValueError(f"template {template_id} requires exactly three tags")
        tags = tuple(str(tag).strip() for tag in tags_raw)
        if not name or not avatar_ref or not summary or not persona_version or not all(tags):
            raise ValueError(f"template {template_id} has incomplete metadata")
        name_pool = _name_pool(template_id, item)
        name_pool_version = str(item.get("name_pool_version") or "").strip() or None
        # 名池与版本必须成对：只给名池会让「换名池必须换版本」失去着力点，
        # 只给版本则是空引用。两者都不给 = 该模板暂不参与实例命名（naming_status=unavailable）。
        if bool(name_pool) != bool(name_pool_version):
            raise ValueError(
                f"template {template_id}: name_pool and name_pool_version must be set together"
            )
        records.append(
            PresetRecord(
                template_id=template_id,
                rank=rank,
                name=name,
                avatar_ref=avatar_ref,
                summary=summary,
                tags=(tags[0], tags[1], tags[2]),
                persona_seed_json=_persona_json(item.get("persona_seed_json")),
                persona_version=persona_version,
                persona_key=str(item.get("persona_key") or "").strip() or None,
                long_summary=str(item.get("long_summary") or "").strip() or None,
                name_pool=name_pool,
                name_pool_version=name_pool_version,
            )
        )
    records.sort(key=lambda record: record.rank)
    expected = list(range(1, len(records) + 1))
    if [record.rank for record in records] != expected:
        raise ValueError(
            "initial_candidate_rank must be a contiguous 1.."
            f"{len(records)} sequence"
        )
    if len({record.template_id for record in records}) != len(records):
        raise ValueError("template_id values must be unique")
    return tuple(records)


def load_manifest(path: str) -> Tuple[PresetRecord, ...]:
    """从 UTF-8 JSON 文件读取并校验 manifest。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_manifest(payload)


def _canonical_json(raw: Any, fallback: Any) -> Any:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return fallback


def _immutable_matches(row: dict, record: PresetRecord) -> bool:
    return (
        row["source_type"] == "operations"
        and row.get("owner_platform_user_id") is None
        and row["name"] == record.name
        and row.get("avatar_ref") == record.avatar_ref
        and row.get("summary") == record.summary
        and _canonical_json(row.get("tags_json"), []) == list(record.tags)
        and _canonical_json(row.get("persona_seed_json"), {})
        == json.loads(record.persona_seed_json)
        and row["persona_version"] == record.persona_version
        and row.get("initial_candidate_rank") is not None
        and int(row["initial_candidate_rank"]) == record.rank
    )


def _operational_updates(row: dict, record: PresetRecord) -> Dict[str, Any]:
    """算出已发布模板上需要原地更新的运营元数据；无差异返回空 dict。

    manifest 未提供的字段一律不动——重放一份不含名池的老 manifest 不应把已配好的名池抹掉。
    """
    updates: Dict[str, Any] = {}
    if record.persona_key and row.get("persona_key") != record.persona_key:
        updates["persona_key"] = record.persona_key
    if record.long_summary and row.get("long_summary") != record.long_summary:
        updates["long_summary"] = record.long_summary
    if record.name_pool:
        desired_pool = json.dumps(list(record.name_pool), ensure_ascii=False)
        if _canonical_json(row.get("name_pool_json"), None) != list(record.name_pool):
            updates["name_pool_json"] = desired_pool
        if row.get("name_pool_version") != record.name_pool_version:
            updates["name_pool_version"] = record.name_pool_version
    return updates


def inspect_import(conn, records: Sequence[PresetRecord], *, dry_run: bool) -> ImportReport:
    """只读计算 create/keep/update/retire 与 immutable 冲突。"""
    desired = {record.template_id: record for record in records}
    # 占位符按 manifest 条数生成：写死四个会在候选池扩容时静默漏读已存在模板，
    # 把「幂等重放」变成「重复创建 → 唯一索引冲突」。
    placeholders = ",".join("?" for _ in desired)
    rows = conn.execute(
        f"SELECT * FROM character_templates WHERE id IN ({placeholders})",
        tuple(desired),
    ).fetchall()
    existing = {str(row["id"]): dict(row) for row in rows}
    create_ids: List[str] = []
    keep_ids: List[str] = []
    update_ids: List[str] = []
    errors: List[str] = []
    for template_id, record in desired.items():
        row = existing.get(template_id)
        if row is None:
            create_ids.append(template_id)
        elif row["status"] != "active":
            errors.append(f"{template_id}: retired template_id cannot be reactivated")
        elif not _immutable_matches(row, record):
            errors.append(f"{template_id}: published template is immutable; use a new template_id")
        elif (
            record.persona_key
            and row.get("persona_key")
            and row["persona_key"] != record.persona_key
        ):
            errors.append(f"{template_id}: persona_key is immutable once assigned")
        else:
            keep_ids.append(template_id)
            if _operational_updates(row, record):
                update_ids.append(template_id)
    active_rows = conn.execute(
        """
        SELECT id FROM character_templates
        WHERE status='active' AND initial_candidate_rank IS NOT NULL
        ORDER BY initial_candidate_rank
        """
    ).fetchall()
    retire_ids = [str(row["id"]) for row in active_rows if str(row["id"]) not in desired]
    return ImportReport(
        dry_run=dry_run,
        create_ids=create_ids,
        keep_ids=keep_ids,
        retire_ids=retire_ids,
        errors=errors,
        update_ids=update_ids,
    )


def _catalog_ready(conn) -> bool:
    rows = conn.execute(
        """
        SELECT initial_candidate_rank, name, avatar_ref, summary, tags_json,
               persona_seed_json, persona_version
        FROM character_templates
        WHERE status='active' AND initial_candidate_rank IS NOT NULL
        ORDER BY initial_candidate_rank
        """
    ).fetchall()
    try:
        validate_manifest(
            [
                {
                    "template_id": f"tmpl_check_{index}",
                    "initial_candidate_rank": row["initial_candidate_rank"],
                    "name": row["name"],
                    "avatar_ref": row["avatar_ref"],
                    "summary": row["summary"],
                    "tags": _canonical_json(row["tags_json"], []),
                    "persona_seed_json": row["persona_seed_json"],
                    "persona_version": row["persona_version"],
                }
                for index, row in enumerate(rows)
            ]
        )
    except (ValueError, TypeError):
        return False
    return True


def import_presets(records: Sequence[PresetRecord], *, dry_run: bool) -> ImportReport:
    """在单事务内应用 manifest；dry_run 时完全只读。"""
    from app.db import connect, create_character_template

    with connect() as conn:
        report = inspect_import(conn, records, dry_run=dry_run)
        if report.errors or dry_run:
            report.catalog_ready = _catalog_ready(conn)
            return report
        if report.retire_ids:
            placeholders = ",".join("?" for _ in report.retire_ids)
            conn.execute(
                f"""
                UPDATE character_templates
                SET status='retired',
                    updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id IN ({placeholders})
                """,
                tuple(report.retire_ids),
            )
        by_id = {record.template_id: record for record in records}
        for template_id in report.create_ids:
            record = by_id[template_id]
            create_character_template(
                template_id=record.template_id,
                source_type="operations",
                name=record.name,
                avatar_ref=record.avatar_ref,
                summary=record.summary,
                tags_json=json.dumps(list(record.tags), ensure_ascii=False),
                persona_seed_json=record.persona_seed_json,
                persona_version=record.persona_version,
                initial_candidate_rank=record.rank,
                persona_key=record.persona_key,
                long_summary=record.long_summary,
                name_pool_json=(
                    json.dumps(list(record.name_pool), ensure_ascii=False)
                    if record.name_pool
                    else None
                ),
                name_pool_version=record.name_pool_version,
                conn=conn,
            )
        for template_id in report.update_ids:
            row = dict(
                conn.execute(
                    "SELECT * FROM character_templates WHERE id = ?", (template_id,)
                ).fetchone()
            )
            updates = _operational_updates(row, by_id[template_id])
            assignments = ", ".join(f"{column} = ?" for column in updates)
            conn.execute(
                f"""
                UPDATE character_templates
                SET {assignments},
                    updated_at=strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
                WHERE id = ?
                """,
                (*updates.values(), template_id),
            )
        report.catalog_ready = _catalog_ready(conn)
        if not report.catalog_ready:
            raise RuntimeError("catalog precheck failed after import")
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="导入/预检 Companion World 初始候选模板目录")
    parser.add_argument("manifest", help="受控 JSON manifest 路径")
    parser.add_argument("--dry-run", action="store_true", help="只读规划，不写数据库")
    parser.add_argument("--database-url", default=None, help="可选覆盖 DATABASE_URL")
    args = parser.parse_args()
    if args.database_url is not None:
        from app.config import settings

        settings.database_url = args.database_url
    report = import_presets(load_manifest(args.manifest), dry_run=args.dry_run)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 1 if report.errors or (not args.dry_run and not report.catalog_ready) else 0


if __name__ == "__main__":
    raise SystemExit(main())
