"""P2-2d: 存量磁盘 profile 文件一次性导入 account_profile_files 的测试。

storage 读写经 app.db._core.connect() 路由到 fresh_db 隔离库；
导入计划函数接收显式 profiles_dir，故直接调用、无需 patch 脚本内 settings。
"""
from pathlib import Path

from app import profile_storage
from scripts.import_profiles_to_db import (
    STATUS_NEW,
    STATUS_OVERWRITE,
    STATUS_SKIP_EXISTS,
    apply_plan,
    collect_import_plans,
    plan_account_import,
)


def _seed_account_dir(profiles_dir: Path, dir_name: str) -> Path:
    """在磁盘铺一个典型账号 profile 目录（顶层 *.md + memory/ daily note）。"""
    acc_dir = profiles_dir / dir_name
    (acc_dir / "memory").mkdir(parents=True)
    (acc_dir / "SOUL.md").write_text("# SOUL\n\n你是专属陪伴。\n", encoding="utf-8")
    (acc_dir / "IDENTITY.md").write_text("# IDENTITY\n\n- AI 名字：小太阳\n", encoding="utf-8")
    (acc_dir / "USER.md").write_text("# USER\n\n- 用户喜欢简洁\n", encoding="utf-8")
    (acc_dir / "MEMORY.md").write_text("# MEMORY\n\n- 长期记忆\n", encoding="utf-8")
    (acc_dir / "user_profile.md").write_text("legacy profile body", encoding="utf-8")
    (acc_dir / "memory" / "2026-06-20.md").write_text("# 2026-06-20\n\n- 当日记录\n", encoding="utf-8")
    return acc_dir


def test_apply_imports_all_files_keyed_by_account_id(fresh_db, tmp_path):
    """整目录导入后，storage 应按 account_id + 相对 posix 路径存到每个文件。"""
    profiles_dir = tmp_path / "profiles"
    account_id = "86f866663cf9-im-bot"  # 真实形态 id：净化 == 原始
    _seed_account_dir(profiles_dir, account_id)

    plans = collect_import_plans(profiles_dir, db_account_ids=[account_id])
    assert len(plans) == 1
    plan = plans[0]
    assert plan.account_id == account_id
    assert plan.known_account is True
    assert {it.status for it in plan.items} == {STATUS_NEW}

    written = apply_plan(plan)
    assert written == 6

    assert profile_storage.read_file(account_id, "SOUL.md") == "# SOUL\n\n你是专属陪伴。\n"
    assert profile_storage.read_file(account_id, "user_profile.md") == "legacy profile body"
    assert (
        profile_storage.read_file(account_id, "memory/2026-06-20.md")
        == "# 2026-06-20\n\n- 当日记录\n"
    )
    assert sorted(profile_storage.list_filenames(account_id)) == [
        "IDENTITY.md",
        "MEMORY.md",
        "SOUL.md",
        "USER.md",
        "memory/2026-06-20.md",
        "user_profile.md",
    ]


def test_dry_run_does_not_write(fresh_db, tmp_path):
    """collect_import_plans 仅构建计划，不应写库。"""
    profiles_dir = tmp_path / "profiles"
    account_id = "acc-dry"
    _seed_account_dir(profiles_dir, account_id)

    collect_import_plans(profiles_dir, db_account_ids=[account_id])

    assert profile_storage.list_filenames(account_id) == []


def test_skip_existing_is_idempotent(fresh_db, tmp_path):
    """二次导入：storage 已有的文件标记 skip，apply 不重复写、不覆盖新内容。"""
    profiles_dir = tmp_path / "profiles"
    account_id = "acc-idem"
    _seed_account_dir(profiles_dir, account_id)

    apply_plan(collect_import_plans(profiles_dir, db_account_ids=[account_id])[0])
    # 模拟切换后 app 产生的新写入，导入不得覆盖它。
    profile_storage.write_file(account_id, "SOUL.md", "切换后的新内容")

    plan2 = collect_import_plans(profiles_dir, db_account_ids=[account_id])[0]
    assert {it.status for it in plan2.items} == {STATUS_SKIP_EXISTS}
    assert plan2.to_write == []
    assert apply_plan(plan2) == 0
    assert profile_storage.read_file(account_id, "SOUL.md") == "切换后的新内容"


def test_overwrite_replaces_existing(fresh_db, tmp_path):
    """--overwrite：storage 已存在的文件标记 overwrite，apply 用磁盘内容整文件覆盖。"""
    profiles_dir = tmp_path / "profiles"
    account_id = "acc-ow"
    _seed_account_dir(profiles_dir, account_id)
    profile_storage.write_file(account_id, "SOUL.md", "旧值")

    plan = collect_import_plans(profiles_dir, db_account_ids=[account_id], overwrite=True)[0]
    assert any(it.status == STATUS_OVERWRITE and it.filename == "SOUL.md" for it in plan.items)
    apply_plan(plan)
    assert profile_storage.read_file(account_id, "SOUL.md") == "# SOUL\n\n你是专属陪伴。\n"


def test_account_filter_targets_single_dir(fresh_db, tmp_path):
    """--account 只处理指定原始 id 的目录。"""
    profiles_dir = tmp_path / "profiles"
    _seed_account_dir(profiles_dir, "acc-a")
    _seed_account_dir(profiles_dir, "acc-b")

    plans = collect_import_plans(
        profiles_dir, account_filter="acc-a", db_account_ids=["acc-a", "acc-b"]
    )
    assert len(plans) == 1
    assert plans[0].account_id == "acc-a"


def test_unknown_dir_falls_back_to_dir_name(fresh_db, tmp_path):
    """DB 中没有的孤儿目录：以目录名兜底为 account_id 并标 unknown。"""
    profiles_dir = tmp_path / "profiles"
    _seed_account_dir(profiles_dir, "orphan-acc")

    plans = collect_import_plans(profiles_dir, db_account_ids=[])
    assert len(plans) == 1
    assert plans[0].account_id == "orphan-acc"
    assert plans[0].known_account is False


def test_pycache_and_hidden_files_skipped(fresh_db, tmp_path):
    """__pycache__ 与隐藏文件不应被导入。"""
    profiles_dir = tmp_path / "profiles"
    acc_dir = _seed_account_dir(profiles_dir, "acc-skip")
    (acc_dir / "__pycache__").mkdir()
    (acc_dir / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")
    (acc_dir / ".DS_Store").write_text("junk", encoding="utf-8")

    plan = plan_account_import("acc-skip", acc_dir, known_account=True)
    names = {it.filename for it in plan.items}
    assert "__pycache__/x.pyc" not in names
    assert ".DS_Store" not in names
    assert "SOUL.md" in names
