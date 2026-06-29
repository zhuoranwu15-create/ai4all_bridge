"""Task 4 验收测试：seed CSV 数据 + 导入脚本。

测试直接调用 seed_icebreaker_scripts.py 中的核心函数，不测试 CLI 入口。
数据库测试使用内存 SQLite + 完整 init_db（确保 icebreaker_scripts 表存在）。
"""

import csv
import sqlite3
import tempfile
from pathlib import Path

import pytest

# CSV 文件路径（相对于项目根）
CSV_PATH = Path("data/seeds/icebreaker_scripts.csv")

# 导入目标函数
from scripts.seed_icebreaker_scripts import (
    load_seed_rows,
    validate_seed_rows,
    seed_icebreaker_scripts,
)


# ---------------------------------------------------------------------------
# Fixture：带 icebreaker_scripts 表的临时 SQLite
# ---------------------------------------------------------------------------

_CREATE_ICEBREAKER_SCRIPTS = """
CREATE TABLE IF NOT EXISTS icebreaker_scripts (
    id TEXT PRIMARY KEY,
    script_type TEXT NOT NULL,
    text TEXT NOT NULL,
    reply_cost TEXT NOT NULL,
    tone TEXT,
    suitable_for TEXT,
    avoid_when TEXT,
    follow_goal TEXT,
    signal_extract TEXT,
    fun_score INTEGER,
    reply_ease_score INTEGER,
    offense_risk INTEGER,
    marketing_feel INTEGER,
    freq_tier TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
);
"""


@pytest.fixture
def tmp_db():
    """返回一个包含 icebreaker_scripts 表的临时 SQLite 文件路径。

    seed 脚本用 sqlite3.connect(db_path) 直接操作，不走 app 后端，
    因此这里也直接用 stdlib sqlite3 建表，避免 PG pool 初始化问题。
    """
    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_ICEBREAKER_SCRIPTS)
    conn.commit()
    conn.close()

    yield db_path

    Path(db_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 1. CSV 文件基本检查
# ---------------------------------------------------------------------------

def test_csv_file_exists():
    assert CSV_PATH.exists(), f"CSV 文件不存在: {CSV_PATH}"


def test_csv_has_100_rows():
    rows = load_seed_rows(str(CSV_PATH))
    assert len(rows) == 100


def test_csv_ids_unique():
    rows = load_seed_rows(str(CSV_PATH))
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "CSV 中存在重复 id"


def test_csv_bingbing024_disabled():
    rows = load_seed_rows(str(CSV_PATH))
    row = next((r for r in rows if r["id"] == "破冰024"), None)
    assert row is not None, "破冰024 不存在"
    assert row["enabled"] == "0", f"破冰024 应为 enabled=0，实际={row['enabled']}"


def test_csv_enabled_count():
    rows = load_seed_rows(str(CSV_PATH))
    enabled = sum(1 for r in rows if r["enabled"] == "1")
    disabled = sum(1 for r in rows if r["enabled"] == "0")
    assert enabled == 99
    assert disabled == 1


def test_csv_freq_tier_values():
    """所有 freq_tier 值必须是合法枚举。"""
    rows = load_seed_rows(str(CSV_PATH))
    valid = {"common", "mid_low", "low_freq"}
    bad = {r["freq_tier"] for r in rows if r["freq_tier"] not in valid}
    assert not bad, f"非法 freq_tier 值: {bad}"


def test_csv_no_empty_text():
    rows = load_seed_rows(str(CSV_PATH))
    empty = [r["id"] for r in rows if not r.get("text", "").strip()]
    assert not empty, f"text 为空的行: {empty}"


# ---------------------------------------------------------------------------
# 2. validate_seed_rows 校验逻辑
# ---------------------------------------------------------------------------

def _make_valid_rows(n=100):
    """生成 n 条合法行。"""
    return [
        {
            "id": f"破冰{i:03d}",
            "script_type": "小测试",
            "text": f"测试话术{i}",
            "reply_cost": "低",
            "tone": "俏皮",
            "suitable_for": "",
            "avoid_when": "",
            "follow_goal": "",
            "signal_extract": "",
            "fun_score": "4",
            "reply_ease_score": "4",
            "offense_risk": "1",
            "marketing_feel": "1",
            "freq_tier": "common",
            "enabled": "1",
            "notes": "",
        }
        for i in range(1, n + 1)
    ]


def test_validate_rejects_wrong_row_count():
    rows = _make_valid_rows(99)
    with pytest.raises(ValueError, match="100"):
        validate_seed_rows(rows)


def test_validate_rejects_duplicate_id():
    rows = _make_valid_rows(100)
    rows[1]["id"] = rows[0]["id"]  # 造成重复
    with pytest.raises(ValueError, match="重复"):
        validate_seed_rows(rows)


def test_validate_rejects_invalid_freq_tier():
    rows = _make_valid_rows(100)
    rows[0]["freq_tier"] = "unknown_tier"
    with pytest.raises(ValueError, match="freq_tier"):
        validate_seed_rows(rows)


def test_validate_rejects_score_out_of_range():
    rows = _make_valid_rows(100)
    rows[0]["fun_score"] = "6"  # 超出 1-5
    with pytest.raises(ValueError, match="fun_score"):
        validate_seed_rows(rows)


def test_validate_rejects_score_zero():
    rows = _make_valid_rows(100)
    rows[0]["marketing_feel"] = "0"
    with pytest.raises(ValueError, match="marketing_feel"):
        validate_seed_rows(rows)


def test_validate_rejects_invalid_enabled():
    rows = _make_valid_rows(100)
    rows[0]["enabled"] = "2"
    with pytest.raises(ValueError, match="enabled"):
        validate_seed_rows(rows)


def test_validate_rejects_empty_text():
    rows = _make_valid_rows(100)
    rows[0]["text"] = ""
    with pytest.raises(ValueError, match="text"):
        validate_seed_rows(rows)


def test_validate_accepts_valid_csv():
    """实际 CSV 应通过校验。"""
    rows = load_seed_rows(str(CSV_PATH))
    validate_seed_rows(rows)  # 不应 raise


# ---------------------------------------------------------------------------
# 3. dry-run 不写数据库
# ---------------------------------------------------------------------------

def test_dry_run_does_not_write(tmp_db):
    stats = seed_icebreaker_scripts(tmp_db, str(CSV_PATH), dry_run=True)
    assert stats["total"] == 100

    conn = sqlite3.connect(tmp_db)
    count = conn.execute("SELECT count(*) FROM icebreaker_scripts").fetchone()[0]
    conn.close()
    assert count == 0, "dry-run 不应向数据库写入任何行"


# ---------------------------------------------------------------------------
# 4. 正式导入
# ---------------------------------------------------------------------------

def test_import_count(tmp_db):
    stats = seed_icebreaker_scripts(tmp_db, str(CSV_PATH))
    assert stats["inserted"] == 100
    assert stats["skipped"] == 0

    conn = sqlite3.connect(tmp_db)
    count = conn.execute("SELECT count(*) FROM icebreaker_scripts").fetchone()[0]
    conn.close()
    assert count == 100


def test_import_enabled_count(tmp_db):
    seed_icebreaker_scripts(tmp_db, str(CSV_PATH))
    conn = sqlite3.connect(tmp_db)
    enabled = conn.execute(
        "SELECT count(*) FROM icebreaker_scripts WHERE enabled=1"
    ).fetchone()[0]
    disabled = conn.execute(
        "SELECT count(*) FROM icebreaker_scripts WHERE enabled=0"
    ).fetchone()[0]
    conn.close()
    assert enabled == 99
    assert disabled == 1


def test_import_bingbing024_disabled(tmp_db):
    seed_icebreaker_scripts(tmp_db, str(CSV_PATH))
    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT enabled FROM icebreaker_scripts WHERE id='破冰024'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 0


# ---------------------------------------------------------------------------
# 5. 幂等：重复导入不重复插入
# ---------------------------------------------------------------------------

def test_idempotent_import(tmp_db):
    stats1 = seed_icebreaker_scripts(tmp_db, str(CSV_PATH))
    stats2 = seed_icebreaker_scripts(tmp_db, str(CSV_PATH))

    assert stats1["inserted"] == 100
    assert stats2["inserted"] == 0
    assert stats2["skipped"] == 100

    conn = sqlite3.connect(tmp_db)
    count = conn.execute("SELECT count(*) FROM icebreaker_scripts").fetchone()[0]
    conn.close()
    assert count == 100  # 仍是 100，不重复插入


# ---------------------------------------------------------------------------
# 6. --update 可以更新已有记录
# ---------------------------------------------------------------------------

def test_update_overwrites_existing(tmp_db):
    seed_icebreaker_scripts(tmp_db, str(CSV_PATH))

    # 手动修改破冰001 的 text
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "UPDATE icebreaker_scripts SET text='临时修改' WHERE id='破冰001'"
    )
    conn.commit()
    conn.close()

    # --update 应恢复原值
    seed_icebreaker_scripts(tmp_db, str(CSV_PATH), update=True)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT text FROM icebreaker_scripts WHERE id='破冰001'"
    ).fetchone()
    conn.close()

    original_rows = load_seed_rows(str(CSV_PATH))
    original_text = next(r["text"] for r in original_rows if r["id"] == "破冰001")
    assert row[0] == original_text, "update 后 text 应恢复为 CSV 原值"
