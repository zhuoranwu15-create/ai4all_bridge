#!/usr/bin/env python3
"""Full system reset for AI4ALL Weixin Bot.

Clears ALL user data: database rows, user_profiles files, and OpenClaw WeChat
session state. After reset, users must re-register (phone) and re-bind (WeChat).

IMPORTANT: Requires an interactive TTY and explicit "FULL-RESET" confirmation.
           Cannot be run non-interactively or by automated tools.

Usage:
    .venv/bin/python scripts/full_system_reset.py            # run for real
    .venv/bin/python scripts/full_system_reset.py --dry-run  # preview only
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "ai4all.sqlite3"
USER_PROFILES_DIR = REPO_ROOT / "data" / "user_profiles"
OPENCLAW_DIR = Path.home() / ".openclaw"
OPENCLAW_WEIXIN_DIR = OPENCLAW_DIR / "openclaw-weixin"
OPENCLAW_ACCOUNTS_JSON = OPENCLAW_WEIXIN_DIR / "accounts.json"
OPENCLAW_ACCOUNTS_DIR = OPENCLAW_WEIXIN_DIR / "accounts"
LAUNCHAGENT_LABEL = "ai.openclaw.gateway"
LAUNCHAGENT_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHAGENT_LABEL}.plist"
)

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# ---------------------------------------------------------------------------
# Tables to clear (FK-safe order: leaf nodes first)
# ---------------------------------------------------------------------------

TABLES_TO_CLEAR = [
    # dreaming / memory
    "memory_events",
    "dreaming_memory_items",
    "dreaming_runs",
    # traces + messages
    "debug_traces",
    "messages",
    # billing
    "cost_events",
    "entitlement_ledger",
    "entitlement_wallets",
    # proactive / outbound
    "reminders",
    "proactive_commitments",
    "content_invitations",
    "content_invitation_preferences",
    "outbound_messages",
    "proactive_account_state",
    # misc account-scoped
    "daily_usage",
    "profiles",
    "channel_bindings",
    # binding / ownership
    "binding_intents",
    "account_owner_bindings",
    # core
    "sessions",
    "accounts",
    # platform user
    "platform_user_sessions",
    "subscriptions",
    "platform_users",
    # standalone
    "phone_verifications",
]

TABLES_TO_KEEP = {"admin_users", "admin_access_events", "admin_plaintext_grants"}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _hdr(title):
    print(f"\n{'═' * 62}")
    print(f"  {title}")
    print(f"{'═' * 62}")


def _section(title):
    print(f"\n── {title}")


def _ok(msg):
    print(f"  ✓  {msg}")


def _warn(msg):
    print(f"  ⚠  {msg}", file=sys.stderr)


def _err(msg):
    print(f"  ✗  {msg}", file=sys.stderr)


def _dry(msg):
    print(f"  [DRY RUN]  {msg}")


# ---------------------------------------------------------------------------
# Pre-flight summary
# ---------------------------------------------------------------------------

def _db_counts() -> dict:
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    counts = {}
    for tbl in TABLES_TO_CLEAR:
        try:
            counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[tbl] = None  # table missing
    conn.close()
    return counts


def _openclaw_accounts() -> list:
    if not OPENCLAW_ACCOUNTS_JSON.exists():
        return []
    try:
        return json.loads(OPENCLAW_ACCOUNTS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []


def _profile_dirs() -> list:
    if not USER_PROFILES_DIR.exists():
        return []
    return sorted(p for p in USER_PROFILES_DIR.iterdir() if p.is_dir())


def print_summary():
    _hdr("AI4ALL 全系统重置 — 预检摘要")

    print(f"\n📊  数据库  ({DB_PATH.relative_to(REPO_ROOT)})")
    if not DB_PATH.exists():
        _warn("数据库文件不存在")
    else:
        counts = _db_counts()
        total = 0
        for tbl, n in counts.items():
            if n:
                print(f"      {tbl:<44}  {n:>6} 行")
                total += n
        print(f"      {'合计':<44}  {total:>6} 行")

    print(f"\n📁  用户 Profile 文件  (data/user_profiles/)")
    dirs = _profile_dirs()
    if dirs:
        for d in dirs:
            print(f"      {d.name}")
        print(f"      合计 {len(dirs)} 个目录")
    else:
        print("      (空)")

    print(f"\n🔌  OpenClaw 微信账号  (~/.openclaw/openclaw-weixin/)")
    accounts = _openclaw_accounts()
    if accounts:
        for a in accounts:
            print(f"      {a}")
    else:
        print("      accounts.json 为空或不存在")
    n_files = len(list(OPENCLAW_ACCOUNTS_DIR.glob("*"))) if OPENCLAW_ACCOUNTS_DIR.exists() else 0
    if n_files:
        print(f"      session 文件: {n_files} 个")

    print(f"\n{'─' * 62}")
    print(f"  保留不变:  {', '.join(sorted(TABLES_TO_KEEP))}")
    print(f"  重置后需要: 重新注册手机号 → 重新扫码绑定 → 全新 onboarding")
    print(f"{'─' * 62}")


# ---------------------------------------------------------------------------
# Phase 1: Database
# ---------------------------------------------------------------------------

def _backup_db(dry_run: bool) -> Path:
    bak = DB_PATH.parent / f"ai4all_{TIMESTAMP}.sqlite3.bak"
    if dry_run:
        _dry(f"备份 DB → data/{bak.name}")
    else:
        shutil.copy2(DB_PATH, bak)
        _ok(f"DB 备份 → data/{bak.name}")
    return bak


def phase_db(dry_run: bool):
    _section("阶段 1 — 数据库清理")

    if not DB_PATH.exists():
        _warn("数据库不存在，跳过")
        return

    _backup_db(dry_run)

    counts = _db_counts()

    if dry_run:
        for tbl in TABLES_TO_CLEAR:
            n = counts.get(tbl) or 0
            _dry(f"DELETE FROM {tbl:<44}  (当前 {n} 行)")
        return

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        for tbl in TABLES_TO_CLEAR:
            try:
                cur = conn.execute(f"DELETE FROM {tbl}")
                if cur.rowcount:
                    _ok(f"DELETE FROM {tbl:<44}  {cur.rowcount:>6} 行")
            except sqlite3.OperationalError as exc:
                _warn(f"跳过 {tbl}: {exc}")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Phase 2: user_profiles filesystem
# ---------------------------------------------------------------------------

def phase_profiles(dry_run: bool):
    _section("阶段 2 — 用户 Profile 文件清理")

    dirs = _profile_dirs()
    if not dirs:
        print("  (无文件，跳过)")
        return

    bak = REPO_ROOT / "data" / f"user_profiles_backup_{TIMESTAMP}.tar.gz"
    if dry_run:
        _dry(f"备份 user_profiles/ → data/{bak.name}")
        for d in dirs:
            _dry(f"rm -rf user_profiles/{d.name}/")
        return

    subprocess.run(
        ["tar", "-czf", str(bak), "-C", str(REPO_ROOT / "data"), "user_profiles"],
        check=True,
    )
    _ok(f"文件备份 → data/{bak.name}")

    for d in dirs:
        shutil.rmtree(d)
    _ok(f"已删除 {len(dirs)} 个 account profile 目录")


# ---------------------------------------------------------------------------
# Phase 3: OpenClaw WeChat channel reset
# ---------------------------------------------------------------------------

def _gateway_running() -> bool:
    try:
        result = subprocess.run(
            ["launchctl", "list", LAUNCHAGENT_LABEL],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def _stop_gateway(dry_run: bool) -> bool:
    if dry_run:
        _dry(f"launchctl stop {LAUNCHAGENT_LABEL}")
        return True
    if not _gateway_running():
        _warn("OpenClaw gateway 未运行，跳过停止步骤")
        return True
    result = subprocess.run(
        ["launchctl", "stop", LAUNCHAGENT_LABEL],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _warn(f"launchctl stop 返回 {result.returncode}，继续执行")
    else:
        _ok(f"OpenClaw gateway 已停止 ({LAUNCHAGENT_LABEL})")
    time.sleep(2)
    return True


def _start_gateway(dry_run: bool):
    if dry_run:
        _dry(f"launchctl start {LAUNCHAGENT_LABEL}")
        return
    result = subprocess.run(
        ["launchctl", "start", LAUNCHAGENT_LABEL],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _warn(f"launchctl start 失败 (rc={result.returncode}) — 请手动运行: launchctl start {LAUNCHAGENT_LABEL}")
    else:
        _ok(f"OpenClaw gateway 已重启 ({LAUNCHAGENT_LABEL})")


def phase_openclaw(dry_run: bool):
    _section("阶段 3 — OpenClaw 微信通道重置")

    if not OPENCLAW_WEIXIN_DIR.exists():
        _warn(f"目录不存在: {OPENCLAW_WEIXIN_DIR}，跳过")
        return

    bak = OPENCLAW_DIR / f"openclaw-weixin.bak.{TIMESTAMP}"

    accounts = _openclaw_accounts()
    session_files = list(OPENCLAW_ACCOUNTS_DIR.glob("*")) if OPENCLAW_ACCOUNTS_DIR.exists() else []

    if dry_run:
        _dry(f"备份 ~/.openclaw/openclaw-weixin/ → {bak.name}/")
        _dry(f"停止 OpenClaw gateway (launchctl stop {LAUNCHAGENT_LABEL})")
        for a in accounts:
            _dry(f"清除账号 session: {a}")
        _dry(f"删除 {len(session_files)} 个 session 文件 (accounts/)")
        _dry("accounts.json → []")
        _dry(f"重启 OpenClaw gateway (launchctl start {LAUNCHAGENT_LABEL})")
        return

    # Backup entire openclaw-weixin dir
    shutil.copytree(str(OPENCLAW_WEIXIN_DIR), str(bak))
    _ok(f"OpenClaw weixin 备份 → {bak.name}/")

    # Stop gateway before touching session files
    _stop_gateway(dry_run)

    # Delete session files
    if OPENCLAW_ACCOUNTS_DIR.exists():
        for f in list(OPENCLAW_ACCOUNTS_DIR.iterdir()):
            f.unlink() if f.is_file() else shutil.rmtree(f)
        _ok(f"已删除 {len(session_files)} 个 session 文件 (accounts/)")
    else:
        _ok("accounts/ 目录不存在，跳过")

    # Reset accounts.json
    OPENCLAW_ACCOUNTS_JSON.write_text("[]", encoding="utf-8")
    _ok("accounts.json 已重置为 []")

    # Restart gateway
    _start_gateway(dry_run)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Hard guard: must be interactive TTY
    if not sys.stdin.isatty():
        _err("此脚本需要交互式终端（不能通过管道或非 TTY 方式运行）。")
        sys.exit(2)

    parser = argparse.ArgumentParser(
        description="AI4ALL 全系统重置（需手动输入 FULL-RESET 确认）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览将要执行的操作，不实际执行",
    )
    args = parser.parse_args()

    print_summary()

    if args.dry_run:
        print("\n\n⚙️   DRY RUN 模式 — 以下为将要执行的操作\n")
        phase_db(dry_run=True)
        phase_profiles(dry_run=True)
        phase_openclaw(dry_run=True)
        print("\n✅  DRY RUN 完成。去掉 --dry-run 并输入确认码来实际执行。\n")
        return

    # Warn about running server
    print()
    _warn("执行前请确认：uvicorn / dreaming_scheduler 等进程已停止，否则可能在重置时写入新数据。")

    print()
    print("⚠️  " * 21)
    print()
    print("  此操作将永久删除所有用户数据：")
    print("    • 数据库：platform_users, accounts, messages, sessions 等 24 张表")
    print("    • 文件：data/user_profiles/ 下全部 account 目录")
    print("    • OpenClaw：微信 bot 的 session token（需重新扫码登录）")
    print()
    print("  备份会自动创建，但确认后立即开始执行，无法中途撤销。")
    print()
    print("⚠️  " * 21)
    print()

    try:
        answer = input("  输入 FULL-RESET 确认执行（其他输入均取消）: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n\n已取消。\n")
        sys.exit(0)

    if answer != "FULL-RESET":
        print("\n已取消（输入不匹配）。\n")
        sys.exit(0)

    print("\n开始执行...\n")

    try:
        phase_db(dry_run=False)
        phase_profiles(dry_run=False)
        phase_openclaw(dry_run=False)
    except Exception as exc:
        _err(f"\n执行中断: {exc}")
        _err("请检查备份文件（data/*.bak, ~/.openclaw/openclaw-weixin.bak.*/）并手动恢复。")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    _hdr("重置完成 ✓")
    print()
    print("  下一步：")
    print("  1. 确认 OpenClaw gateway 正常运行: openclaw status")
    print("  2. 启动 uvicorn: .venv/bin/uvicorn app.main:app --reload --port 8180")
    print("  3. 浏览器打开 http://localhost:8180 → 用手机号重新注册")
    print("  4. 扫码绑定微信")
    print("  5. 发一条微信消息触发 onboarding")
    print()
    print(f"  备份位置:")
    print(f"    data/ai4all_{TIMESTAMP}.sqlite3.bak")
    print(f"    data/user_profiles_backup_{TIMESTAMP}.tar.gz")
    print(f"    ~/.openclaw/openclaw-weixin.bak.{TIMESTAMP}/")
    print()


if __name__ == "__main__":
    main()
