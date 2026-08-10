"""连续 rank 运营目录导入：dry-run、幂等、不可变、换版退休与当前五人 manifest。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import app.db as db
from scripts.import_companion_world_presets import import_presets, load_manifest, validate_manifest

SHIPPED_MANIFEST = Path(__file__).resolve().parents[1] / "data/companion_world/presets_v1.json"


def _manifest(version: str = "v1") -> list[dict]:
    return [
        {
            "template_id": f"tmpl_mingchan_{version}_{rank}",
            "initial_candidate_rank": rank,
            "name": f"运营角色{rank}",
            "avatar_ref": f"asset://{version}/{rank}",
            "summary": f"{version} 简介 {rank}",
            "tags": ["温柔", "好奇", f"类型{rank}"],
            "persona_seed_json": {
                "SOUL.md": f"# SOUL\n\n{version}-{rank}",
                "IDENTITY.md": f"# IDENTITY\n\n{version}-{rank}",
            },
            "persona_version": version,
        }
        for rank in range(1, 5)
    ]


def _insert_zhaoxi_catalog(conn, *, ids: list[str] | None = None) -> None:
    """插入一套朝夕历史目录，模拟生产库中与鸣蝉并存的产品资产。"""
    template_ids = ids or [f"tmpl_ops_v1_{rank}" for rank in range(1, 6)]
    for rank, template_id in enumerate(template_ids, start=1):
        conn.execute(
            """
            INSERT INTO character_templates(
                id, app_id, source_type, name, avatar_ref, summary, tags_json,
                persona_seed_json, persona_version, status, initial_candidate_rank
            ) VALUES (?, 'zhaoxi', 'operations', ?, ?, ?, ?, ?, 'v1', 'active', ?)
            """,
            (
                template_id,
                f"朝夕角色{rank}",
                f"asset://zhaoxi/{rank}",
                f"朝夕简介 {rank}",
                json.dumps(["朝夕", "历史", f"角色{rank}"], ensure_ascii=False),
                json.dumps(
                    {
                        "SOUL.md": f"# SOUL\n\nzhaoxi-{rank}",
                        "IDENTITY.md": f"# IDENTITY\n\nzhaoxi-{rank}",
                    },
                    ensure_ascii=False,
                ),
                rank,
            ),
        )


def test_shipped_manifest_contains_five_ranked_personas_with_sichen():
    records = load_manifest(str(SHIPPED_MANIFEST))
    assert [item.template_id for item in records] == [
        f"tmpl_mingchan_v1_{rank}" for rank in range(1, 6)
    ]
    assert [item.rank for item in records] == [1, 2, 3, 4, 5]
    assert [item.persona_key for item in records] == [
        "linxiaoman",
        "luxingye",
        "shenchuan",
        "atang",
        "sichen",
    ]
    sichen = records[-1]
    assert sichen.name == "司辰"
    assert sichen.name_pool == ("司辰", "辰叔", "老辰", "司叔", "辰生")
    assert "用户主动提排盘" in sichen.persona_seed_json


def test_preset_import_dry_run_apply_and_replay(fresh_db):
    records = validate_manifest(_manifest())
    dry = import_presets(records, dry_run=True)
    assert dry.create_ids == [f"tmpl_mingchan_v1_{rank}" for rank in range(1, 5)]
    assert dry.catalog_ready is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM character_templates").fetchone()["c"] == 0

    applied = import_presets(records, dry_run=False)
    replay = import_presets(records, dry_run=False)
    assert applied.catalog_ready is True
    assert len(applied.create_ids) == 4
    assert replay.create_ids == [] and len(replay.keep_ids) == 4
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM character_templates "
            "WHERE app_id='mingchan' AND status='active'"
        ).fetchone()["c"] == 4


def test_import_isolated_from_existing_zhaoxi_catalog(fresh_db):
    """朝夕目录不能让鸣蝉 ready，也不能被鸣蝉导入退休、更新或复制。"""
    with db.connect() as conn:
        _insert_zhaoxi_catalog(conn)
        before = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM character_templates WHERE app_id='zhaoxi' ORDER BY id"
            ).fetchall()
        ]

    records = load_manifest(str(SHIPPED_MANIFEST))
    dry = import_presets(records, dry_run=True)
    assert dry.errors == []
    assert dry.catalog_ready is False
    assert dry.retire_ids == []
    assert dry.create_ids == [f"tmpl_mingchan_v1_{rank}" for rank in range(1, 6)]

    applied = import_presets(records, dry_run=False)
    assert applied.errors == []
    assert applied.catalog_ready is True
    assert applied.retire_ids == []
    with db.connect() as conn:
        mingchan = conn.execute(
            """
            SELECT id, initial_candidate_rank, status
            FROM character_templates
            WHERE app_id='mingchan'
            ORDER BY initial_candidate_rank
            """
        ).fetchall()
        after = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM character_templates WHERE app_id='zhaoxi' ORDER BY id"
            ).fetchall()
        ]
    actual_catalog = [
        (row["id"], row["initial_candidate_rank"], row["status"])
        for row in mingchan
    ]
    assert actual_catalog == [
        (f"tmpl_mingchan_v1_{rank}", rank, "active")
        for rank in range(1, 6)
    ]
    assert after == before


def test_import_reports_cross_product_template_id_conflict(fresh_db):
    """模板主键全局唯一；同 ID 属于其他产品时必须显式失败且不写鸣蝉目录。"""
    records = validate_manifest(_manifest())
    conflict_id = records[0].template_id
    with db.connect() as conn:
        _insert_zhaoxi_catalog(conn, ids=[conflict_id])

    report = import_presets(records, dry_run=False)
    assert report.errors == [
        f"{conflict_id}: template_id belongs to app_id=zhaoxi; "
        "use a globally unique mingchan template_id"
    ]
    assert conflict_id not in report.create_ids
    assert report.retire_ids == []
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM character_templates WHERE app_id='mingchan'"
        ).fetchone()["c"] == 0
        owner = conn.execute(
            "SELECT app_id, status FROM character_templates WHERE id=?", (conflict_id,)
        ).fetchone()
    assert (owner["app_id"], owner["status"]) == ("zhaoxi", "active")


def test_published_template_change_requires_new_id(fresh_db):
    manifest = _manifest()
    import_presets(validate_manifest(manifest), dry_run=False)
    changed = copy.deepcopy(manifest)
    changed[0]["persona_seed_json"]["SOUL.md"] = "# SOUL\n\n静默改写"
    report = import_presets(validate_manifest(changed), dry_run=False)
    assert report.errors and "immutable" in report.errors[0]
    with db.connect() as conn:
        original = conn.execute(
            "SELECT persona_seed_json FROM character_templates "
            "WHERE id='tmpl_mingchan_v1_1'"
        ).fetchone()["persona_seed_json"]
    assert "静默改写" not in original


def test_new_version_retires_old_catalog_without_deleting_history(fresh_db):
    import_presets(validate_manifest(_manifest("v1")), dry_run=False)
    report = import_presets(validate_manifest(_manifest("v2")), dry_run=False)
    assert len(report.create_ids) == 4 and len(report.retire_ids) == 4
    assert report.catalog_ready is True
    with db.connect() as conn:
        active = conn.execute(
            "SELECT id FROM character_templates "
            "WHERE app_id='mingchan' AND status='active' "
            "ORDER BY initial_candidate_rank"
        ).fetchall()
        retired = conn.execute(
            "SELECT COUNT(*) c FROM character_templates "
            "WHERE app_id='mingchan' AND status='retired'"
        ).fetchone()["c"]
    assert [row["id"] for row in active] == [
        f"tmpl_mingchan_v2_{rank}" for rank in range(1, 5)
    ]
    assert retired == 4
