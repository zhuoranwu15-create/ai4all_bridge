"""scripts/backup_data.py 的聚焦测试：用 tmp_path 造临时库+目录，不起服务。"""

import json
import sqlite3
from pathlib import Path

import pytest


def _make_sqlite(path: Path) -> None:
    """造一个最小可校验的 SQLite 库（含 accounts/messages 便于行数统计）。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, account_id TEXT)")
    conn.execute("INSERT INTO accounts(id) VALUES ('a1'), ('a2')")
    conn.execute("INSERT INTO messages(account_id) VALUES ('a1'), ('a1'), ('a2')")
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """搭建临时库/目录并把 backup_data.settings 指向它们。"""
    from scripts import backup_data

    db = tmp_path / "ai4all.sqlite3"
    profiles = tmp_path / "user_profiles"
    system = tmp_path / "system"
    profiles.mkdir()
    system.mkdir()
    (profiles / "acc1").mkdir()
    (profiles / "acc1" / "SOUL.md").write_text("soul", encoding="utf-8")
    (system / "TOOLS.md").write_text("tools", encoding="utf-8")
    _make_sqlite(db)

    # 强制 SQLite 后端：清空 database_url，使测试不受运行环境 .env（可能配 PG）影响，
    # 确定性地走 SQLite 备份分支（这些用例针对的就是 SQLite 路径）。
    monkeypatch.setattr(backup_data.settings, "database_url", "", raising=False)
    monkeypatch.setattr(backup_data.settings, "database_path", str(db), raising=False)
    monkeypatch.setattr(backup_data.settings, "user_profiles_dir", str(profiles), raising=False)
    monkeypatch.setattr(backup_data.settings, "system_dir", str(system), raising=False)
    return backup_data, tmp_path


def test_backup_produces_complete_artifacts(env, tmp_path):
    backup_data, _ = env
    backups_dir = tmp_path / "backups"
    state_file = str(tmp_path / "state.json")

    manifest = backup_data.run_backup(
        backups_dir=backups_dir,
        retention=14,
        rsync_target="",
        dry_run=False,
        state_file=state_file,
    )

    target = Path(manifest_target(backups_dir))
    assert (target / "db.sqlite3").exists()
    assert (target / "user_profiles.tar.gz").exists()
    assert (target / "system.tar.gz").exists()
    assert (target / "manifest.json").exists()
    assert manifest["integrity_check"] == "ok"
    assert manifest["table_counts"]["accounts"] == 2
    assert manifest["table_counts"]["messages"] == 3
    # 缺失的表记 None，不报错
    assert manifest["table_counts"]["outbound_messages"] is None
    assert manifest["offsite"] == "disabled"
    # 状态文件写成功
    state = json.loads(Path(state_file).read_text(encoding="utf-8"))
    assert state["integrity_check"] == "ok"


def manifest_target(backups_dir: Path) -> Path:
    dirs = [p for p in backups_dir.iterdir() if p.is_dir() and p.name.startswith("ai4all_")]
    assert len(dirs) == 1
    return dirs[0]


def test_rotation_keeps_only_latest_n(env, tmp_path):
    backup_data, _ = env
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    # 造 5 个更旧的假备份目录
    for stamp in ["20260101_000000", "20260102_000000", "20260103_000000",
                  "20260104_000000", "20260105_000000"]:
        (backups_dir / f"ai4all_{stamp}").mkdir()

    removed = backup_data._rotate(backups_dir, retention=3)
    remaining = sorted(p.name for p in backups_dir.iterdir() if p.is_dir())
    assert len(remaining) == 3
    # 保留的是字典序最大的（最新）三个
    assert remaining == ["ai4all_20260103_000000", "ai4all_20260104_000000", "ai4all_20260105_000000"]
    assert set(removed) == {"ai4all_20260101_000000", "ai4all_20260102_000000"}


def test_failure_triggers_alert_and_nonzero_exit(env, tmp_path, monkeypatch):
    backup_data, _ = env
    # 让数据库源不存在 → _backup_sqlite 抛 BackupError
    monkeypatch.setattr(backup_data.settings, "database_path", str(tmp_path / "missing.sqlite3"), raising=False)
    monkeypatch.setattr(backup_data.settings, "feishu_alert_webhook_url", "https://example/hook", raising=False)

    alerts = []
    # 拦截真正的飞书发送
    import app.platform.observability.alerting as alerting
    monkeypatch.setattr(alerting, "_send_feishu_text", lambda url, text, timeout: alerts.append(text))

    rc = backup_data.main([
        "--backups-dir", str(tmp_path / "backups"),
        "--state-file", str(tmp_path / "state.json"),
    ])
    assert rc == 1
    assert len(alerts) == 1
    assert "备份失败" in alerts[0]


def test_pg_env_from_url_parses_and_keeps_password_out_of_argv():
    """PG DSN 正确拆成 libpq 环境变量；含转义字符的口令/用户名正确 unquote。"""
    from scripts.backup_data import _pg_env_from_url

    env = _pg_env_from_url("postgresql://ai4all:p%40ss%3Aword@172.24.16.141:5432/ai4all")
    assert env == {
        "PGHOST": "172.24.16.141",
        "PGPORT": "5432",
        "PGUSER": "ai4all",
        "PGPASSWORD": "p@ss:word",
        "PGDATABASE": "ai4all",
    }
    # 缺省端口/无口令时不产出对应键（让 libpq 用默认）
    sparse = _pg_env_from_url("postgresql://u@host/db")
    assert sparse == {"PGHOST": "host", "PGUSER": "u", "PGDATABASE": "db"}


def test_restore_dry_run_validates(env, tmp_path):
    backup_data, _ = env
    backups_dir = tmp_path / "backups"
    backup_data.run_backup(
        backups_dir=backups_dir, retention=14, rsync_target="",
        dry_run=False, state_file=str(tmp_path / "state.json"),
    )
    target = manifest_target(backups_dir)

    from scripts import restore_data
    rc = restore_data.restore(target, target=None, force=False, assume_yes=False)
    assert rc == 0
