"""AI4ALL 数据备份脚本。

每日由 systemd ai4all-backup.timer 触发，产出一份带完整性校验的本地快照：
  data/backups/ai4all_<时间戳>/
    ├── db.dump             pg_dump -Fc 自定义格式快照，含 account_profile_files
    │                       （P2 后账号 profile 的真相所在）
    ├── user_profiles.tar.gz 磁盘 user_profiles 目录（P2 后为存量副本，不含最新 profile 数据；
    │                        真实数据在 PostgreSQL 的 account_profile_files 表）
    ├── system.tar.gz        data/system 下系统文件（AGENTS.md/TOOLS.md 仍为磁盘文件）
    ├── env.bak              .env（含凭证，chmod 600）
    └── manifest.json        校验结果、各产物大小、关键表行数、git commit

任何步骤失败都会向飞书（FEISHU_ALERT_WEBHOOK_URL）发脱敏告警并以非零码退出；
成功默认静默，仅写状态文件供监控检测陈旧。备份范围按 account 全量，不涉及按账号查询。
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db._backend import database_url  # noqa: E402

DEFAULT_STATE_FILE = "/tmp/ai4all_backup_state.json"
BACKUP_PREFIX = "ai4all_"
# manifest 里记录行数的关键表（不存在则记 None，不报错）。
# account_profile_files：P2 后账号 profile 真相在此表，加入行数可验证备份完整性。
_COUNTED_TABLES = ("accounts", "platform_users", "messages", "outbound_messages", "account_profile_files")


class BackupError(Exception):
    """备份过程中的可预期失败，统一捕获后告警退出。"""


def _now_stamp() -> str:
    """生成北京时区时间戳目录名（运行环境本地时区即北京时）。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _git_commit() -> Optional[str]:
    """返回当前 git commit 短号，失败返回 None（备份不依赖 git）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        return None
    return None


def _backup_sqlite(src_path: Path, dest_path: Path) -> None:
    """用 sqlite3 在线备份 API 拷出一致快照（并发写下仍一致，不锁库）。"""
    if not src_path.exists():
        raise BackupError(f"database not found: {src_path}")
    source = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _integrity_check(db_path: Path) -> str:
    """对备份副本跑 PRAGMA integrity_check，返回结果字符串（正常为 'ok'）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return (row[0] if row else "").strip()
    finally:
        conn.close()


def _pg_env_from_url(database_url: str) -> Dict[str, str]:
    """把 postgresql:// URL 解析成 libpq 连接环境变量。

    口令经 PGPASSWORD 传入子进程，**绝不出现在命令行**（避免 ps/日志泄露）。
    返回值由调用方 merge 进 os.environ 副本后传给 pg_dump/psql。
    """
    parsed = urlparse(database_url)
    env: Dict[str, str] = {}
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)
    if parsed.username:
        env["PGUSER"] = unquote(parsed.username)
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    dbname = (parsed.path or "").lstrip("/")
    if dbname:
        env["PGDATABASE"] = dbname
    return env


def _backup_postgres(database_url: str, dest_path: Path) -> None:
    """用 pg_dump -Fc（自定义格式，压缩 + 可选择性恢复）导出一致快照到 dest_path。

    pg_dump 在单事务快照中导出，并发写下仍得到一致镜像。
    --no-owner/--no-privileges 让 dump 可恢复到任意角色，降低跨环境恢复摩擦。
    """
    env = dict(os.environ)
    env.update(_pg_env_from_url(database_url))
    completed = subprocess.run(
        ["pg_dump", "-Fc", "--no-owner", "--no-privileges", "-f", str(dest_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:800]
        raise BackupError(f"pg_dump failed rc={completed.returncode}: {detail}")
    if not dest_path.exists() or dest_path.stat().st_size == 0:
        raise BackupError("pg_dump produced empty file")


def _pg_dump_integrity(dump_path: Path) -> str:
    """用 pg_restore --list 校验 dump 可解析（未截断/未损坏）；成功返回 'ok'。"""
    completed = subprocess.run(
        ["pg_restore", "--list", str(dump_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:500]
        return f"pg_restore list failed rc={completed.returncode}: {detail}"
    return "ok"


def _pg_table_counts(database_url: str) -> Dict[str, Optional[int]]:
    """对活库统计关键表行数写入 manifest；缺表/查询失败记 None。

    读活库而非 dump（dump 行数不便直接统计），仅作完整性参考，轻微时间偏移可接受。
    _COUNTED_TABLES 全为受信常量，无注入面。
    """
    env = dict(os.environ)
    env.update(_pg_env_from_url(database_url))
    counts: Dict[str, Optional[int]] = {}
    for table in _COUNTED_TABLES:
        completed = subprocess.run(
            ["psql", "-tAXqc", f"SELECT COUNT(*) FROM {table}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if completed.returncode == 0:
            try:
                counts[table] = int((completed.stdout or "").strip())
            except ValueError:
                counts[table] = None
        else:
            counts[table] = None
    return counts


def _archive_dir(src_dir: Path, dest_tar_gz: Path) -> int:
    """把目录打成 .tar.gz，返回归档文件字节数；源目录不存在则建空档。"""
    with tarfile.open(dest_tar_gz, "w:gz") as tar:
        if src_dir.exists():
            tar.add(str(src_dir), arcname=src_dir.name)
    return dest_tar_gz.stat().st_size


def _copy_env(dest_path: Path) -> Optional[int]:
    """复制 .env 到备份目录并 chmod 600；无 .env 时返回 None。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return None
    shutil.copy2(str(env_path), str(dest_path))
    os.chmod(dest_path, 0o600)
    return dest_path.stat().st_size


def _rotate(backups_dir: Path, retention: int) -> List[str]:
    """保留最新 retention 份备份目录，删除更旧的，返回被删目录名。"""
    if retention <= 0:
        return []
    dirs = sorted(
        (p for p in backups_dir.iterdir() if p.is_dir() and p.name.startswith(BACKUP_PREFIX)),
        key=lambda p: p.name,
        reverse=True,
    )
    removed: List[str] = []
    for stale in dirs[retention:]:
        shutil.rmtree(stale, ignore_errors=True)
        removed.append(stale.name)
    return removed


def _rsync_offsite(target: str, src_dir: Path) -> None:
    """把本次备份目录推送到异地 rsync 目标；失败抛 BackupError（仅告警，不影响本地成功）。"""
    completed = subprocess.run(
        ["rsync", "-a", f"{src_dir}/", f"{target.rstrip('/')}/{src_dir.name}/"],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:800]
        raise BackupError(f"rsync offsite failed rc={completed.returncode}: {detail}")


def _send_failure_alert(message: str) -> None:
    """复用 app.platform.observability.alerting 的飞书发送 + 脱敏，向运维通道报警；无 webhook 时静默跳过。"""
    webhook = str(getattr(settings, "feishu_alert_webhook_url", "") or "").strip()
    if not webhook:
        return
    try:
        from app.platform.observability.alerting import _send_feishu_text, redact_alert_text

        text = redact_alert_text(f"[ai4all][backup] 备份失败：{message}")
        _send_feishu_text(webhook, text, 3.0)
    except Exception as err:  # 告警本身失败不能掩盖原始错误
        print(f"failed to send backup alert: {err}", file=sys.stderr)


def _save_state(path: str, state: Dict) -> None:
    """原子写状态文件，供 monitor 检测备份是否陈旧。"""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(state_path)


def run_backup(
    *,
    backups_dir: Path,
    retention: int,
    rsync_target: str,
    dry_run: bool,
    state_file: str,
) -> Dict:
    """执行一次完整备份，返回 manifest dict；失败抛 BackupError。

    主数据库固定走 pg_dump -Fc + pg_restore --list 校验。nearline facts 若存在，
    仍使用 SQLite online backup API 生成一致快照。其余产物、轮转、异地、告警和
    state 逻辑保持不变。
    """
    dsn = database_url()
    db_source_desc = "PostgreSQL(pg_dump -Fc)"
    profiles_dir = Path(settings.user_profiles_dir)
    if not profiles_dir.is_absolute():
        profiles_dir = ROOT / profiles_dir
    system_dir = Path(settings.system_dir)
    if not system_dir.is_absolute():
        system_dir = ROOT / system_dir

    stamp = _now_stamp()
    target_dir = backups_dir / f"{BACKUP_PREFIX}{stamp}"

    if dry_run:
        print(f"[dry-run] would back up:\n  db={db_source_desc}\n  profiles={profiles_dir}\n  system={system_dir}")
        print(f"[dry-run] target dir: {target_dir}")
        print(f"[dry-run] retention={retention} rsync_target={'<set>' if rsync_target else '<none>'}")
        return {"dry_run": True, "target_dir": str(target_dir)}

    backups_dir.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        raise BackupError(f"backup dir already exists: {target_dir}")
    target_dir.mkdir(parents=True)

    # 1) PostgreSQL 一致快照 + 完整性校验。
    db_artifact_name = "db.dump"
    db_dest = target_dir / db_artifact_name
    _backup_postgres(dsn, db_dest)
    integrity = _pg_dump_integrity(db_dest)
    if integrity != "ok":
        raise BackupError(f"pg_dump integrity check failed: {integrity[:500]}")
    db_counts = _pg_table_counts(dsn)

    # 1b) nearline facts.sqlite3（分析事实基线，存在才备份；marts 可重建不入备份）
    facts_src = ROOT / "nearline" / "data" / "facts.sqlite3"
    facts_size: Optional[int] = None
    facts_integrity = "absent"
    if facts_src.exists():
        facts_dest = target_dir / "facts.sqlite3"
        _backup_sqlite(facts_src, facts_dest)
        facts_integrity = _integrity_check(facts_dest)
        if facts_integrity != "ok":
            raise BackupError(f"facts integrity_check failed: {facts_integrity[:500]}")
        facts_size = facts_dest.stat().st_size

    # 2) 磁盘目录 + .env
    profiles_size = _archive_dir(profiles_dir, target_dir / "user_profiles.tar.gz")
    system_size = _archive_dir(system_dir, target_dir / "system.tar.gz")
    env_size = _copy_env(target_dir / "env.bak")

    # 3) manifest
    manifest = {
        "created_at": stamp,
        "git_commit": _git_commit(),
        "integrity_check": integrity,
        "facts_integrity_check": facts_integrity,
        "table_counts": db_counts,
        "artifacts": {
            db_artifact_name: db_dest.stat().st_size,
            "facts.sqlite3": facts_size,
            "user_profiles.tar.gz": profiles_size,
            "system.tar.gz": system_size,
            "env.bak": env_size,
        },
    }
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 4) 轮转
    manifest["rotated_out"] = _rotate(backups_dir, retention)

    # 5) 异地（可选；失败仅告警，不影响本地备份成功判定）
    offsite_error: Optional[str] = None
    if rsync_target:
        try:
            _rsync_offsite(rsync_target, target_dir)
            manifest["offsite"] = "ok"
        except BackupError as err:
            offsite_error = str(err)
            manifest["offsite"] = "failed"
            _send_failure_alert(offsite_error)
    else:
        manifest["offsite"] = "disabled"

    # 6) 状态文件（成功）
    _save_state(
        state_file,
        {
            "last_success_at": stamp,
            "target_dir": str(target_dir),
            "integrity_check": integrity,
            "offsite": manifest["offsite"],
        },
    )

    print(f"backup ok: {target_dir} integrity={integrity} offsite={manifest['offsite']}")
    if offsite_error:
        print(f"warning: offsite push failed: {offsite_error}", file=sys.stderr)
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AI4ALL 数据备份")
    parser.add_argument("--backups-dir", default=None, help="备份根目录，默认取 settings.backup_dir")
    parser.add_argument("--retention", type=int, default=None, help="保留份数，默认 settings.backup_retention_count")
    parser.add_argument("--no-offsite", action="store_true", help="本次跳过异地推送")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="成功状态文件路径")
    args = parser.parse_args(argv)

    backups_dir = Path(args.backups_dir) if args.backups_dir else Path(settings.backup_dir)
    if not backups_dir.is_absolute():
        backups_dir = ROOT / backups_dir
    retention = args.retention if args.retention is not None else settings.backup_retention_count
    rsync_target = "" if args.no_offsite else str(getattr(settings, "backup_rsync_target", "") or "").strip()

    try:
        run_backup(
            backups_dir=backups_dir,
            retention=retention,
            rsync_target=rsync_target,
            dry_run=args.dry_run,
            state_file=args.state_file,
        )
    except BackupError as err:
        _send_failure_alert(str(err))
        print(f"backup failed: {err}", file=sys.stderr)
        return 1
    except Exception as err:  # 兜底：未预期异常也要告警
        _send_failure_alert(f"unexpected: {err}")
        print(f"backup failed (unexpected): {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
