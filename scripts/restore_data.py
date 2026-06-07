"""AI4ALL 备份恢复 / 演练脚本。

给定一个备份目录（backup_data.py 产出的 ai4all_<时间戳>/），把库恢复出来并跑
integrity_check 验证。默认是**安全演练**：恢复到临时路径，绝不触碰线上库。
只有显式 --force --target <path> 才会写入指定路径，且对线上库路径要求二次确认。

用法：
  # 演练（默认）：把备份恢复到临时目录并校验
  .venv/bin/python scripts/restore_data.py data/backups/ai4all_20260607_041700

  # 真正恢复（覆盖目标，需手动停服）
  .venv/bin/python scripts/restore_data.py <备份目录> --force --target data/ai4all.sqlite3
"""

import argparse
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


def _integrity_check(db_path: Path) -> str:
    """对恢复出的库跑 PRAGMA integrity_check（正常返回 'ok'）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return (row[0] if row else "").strip()
    finally:
        conn.close()


def _live_db_path() -> Path:
    db_path = Path(settings.database_path)
    return db_path if db_path.is_absolute() else ROOT / db_path


def restore(backup_dir: Path, target: Optional[Path], force: bool, assume_yes: bool) -> int:
    db_src = backup_dir / "db.sqlite3"
    if not db_src.exists():
        print(f"backup db not found: {db_src}", file=sys.stderr)
        return 1

    # 演练模式：恢复到临时目录，只校验，不覆盖任何线上文件。
    if not force or target is None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ai4all_restore_"))
        dest = tmp_dir / "db.sqlite3"
        shutil.copy2(str(db_src), str(dest))
        integrity = _integrity_check(dest)
        print(f"[dry-run] restored copy: {dest}")
        print(f"[dry-run] integrity_check: {integrity}")
        # 列出归档内容供人工核对
        for name in ("user_profiles.tar.gz", "system.tar.gz"):
            arc = backup_dir / name
            if arc.exists():
                with tarfile.open(arc, "r:gz") as tar:
                    print(f"[dry-run] {name}: {len(tar.getnames())} entries")
        return 0 if integrity == "ok" else 2

    # 真正恢复：覆盖目标路径。对线上库路径要求二次确认。
    target = target if target.is_absolute() else ROOT / target
    if target == _live_db_path() and not assume_yes:
        reply = input(f"覆盖线上库 {target} ？请先停服。确认输入 yes： ").strip().lower()
        if reply != "yes":
            print("aborted.", file=sys.stderr)
            return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(db_src), str(target))
    integrity = _integrity_check(target)
    print(f"restored db -> {target} integrity={integrity}")
    if integrity != "ok":
        print("warning: integrity_check not ok", file=sys.stderr)
        return 2
    print("注意：user_profiles / system 磁盘文件需手动从对应 .tar.gz 解包到目标目录。")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="AI4ALL 备份恢复 / 演练")
    parser.add_argument("backup_dir", help="备份目录 ai4all_<时间戳>/")
    parser.add_argument("--target", default=None, help="恢复目标库路径（仅 --force 时生效）")
    parser.add_argument("--force", action="store_true", help="真正写入 target，否则只做临时演练")
    parser.add_argument("--yes", action="store_true", help="跳过覆盖线上库的二次确认")
    args = parser.parse_args(argv)

    backup_dir = Path(args.backup_dir)
    if not backup_dir.is_absolute():
        backup_dir = ROOT / backup_dir
    if not backup_dir.is_dir():
        print(f"backup dir not found: {backup_dir}", file=sys.stderr)
        return 1

    target = Path(args.target) if args.target else None
    return restore(backup_dir, target, args.force, args.yes)


if __name__ == "__main__":
    sys.exit(main())
