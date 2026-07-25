"""app/profile_storage.py 存储层单测（厚节点改造 P2 / 2a）。

覆盖：读缺失 vs 空文件、整文件写入与 version 递增、append 新建/续写、删除、
按前缀列举、账号隔离、delete_account 全删。fresh_db 注入隔离临时库（两后端通用）。
"""
from app.agent_runtime.persistence import profile_storage as ps


def _version(account_id: str, filename: str) -> int:
    from app.db import connect

    with connect() as conn:
        row = conn.execute(
            "SELECT version FROM account_profile_files WHERE account_id = ? AND filename = ?",
            (account_id, filename),
        ).fetchone()
    return None if row is None else row["version"]


def test_read_missing_returns_none(fresh_db):
    assert ps.read_file("acc1", "SOUL.md") is None
    assert ps.exists("acc1", "SOUL.md") is False


def test_write_then_read(fresh_db):
    ps.write_file("acc1", "SOUL.md", "# SOUL\n\nhello")
    assert ps.read_file("acc1", "SOUL.md") == "# SOUL\n\nhello"
    assert ps.exists("acc1", "SOUL.md") is True
    assert _version("acc1", "SOUL.md") == 1


def test_empty_content_is_not_missing(fresh_db):
    """空字符串内容应被读成 ''（存在），与 None（缺失）区分。"""
    ps.write_file("acc1", "USER.md", "")
    assert ps.read_file("acc1", "USER.md") == ""
    assert ps.exists("acc1", "USER.md") is True


def test_overwrite_bumps_version(fresh_db):
    ps.write_file("acc1", "MEMORY.md", "v1")
    ps.write_file("acc1", "MEMORY.md", "v2")
    assert ps.read_file("acc1", "MEMORY.md") == "v2"
    assert _version("acc1", "MEMORY.md") == 2


def test_append_creates_with_prefix_then_appends(fresh_db):
    ps.append_file("acc1", "memory/2026-06-20.md", "block1", new_file_prefix="# 2026-06-20\n\n")
    assert ps.read_file("acc1", "memory/2026-06-20.md") == "# 2026-06-20\n\nblock1"
    ps.append_file("acc1", "memory/2026-06-20.md", "block2", new_file_prefix="# 2026-06-20\n\n")
    assert ps.read_file("acc1", "memory/2026-06-20.md") == "# 2026-06-20\n\nblock1\nblock2"


def test_delete_file(fresh_db):
    ps.write_file("acc1", "SOUL.md", "x")
    assert ps.delete_file("acc1", "SOUL.md") is True
    assert ps.exists("acc1", "SOUL.md") is False
    # 再删不存在的返回 False
    assert ps.delete_file("acc1", "SOUL.md") is False


def test_list_filenames_with_prefix(fresh_db):
    ps.write_file("acc1", "SOUL.md", "s")
    ps.write_file("acc1", "memory/2026-06-19.md", "a")
    ps.write_file("acc1", "memory/2026-06-20.md", "b")
    assert ps.list_filenames("acc1") == ["SOUL.md", "memory/2026-06-19.md", "memory/2026-06-20.md"]
    assert ps.list_filenames("acc1", prefix="memory/") == [
        "memory/2026-06-19.md",
        "memory/2026-06-20.md",
    ]


def test_account_isolation(fresh_db):
    ps.write_file("acc1", "SOUL.md", "one")
    ps.write_file("acc2", "SOUL.md", "two")
    assert ps.read_file("acc1", "SOUL.md") == "one"
    assert ps.read_file("acc2", "SOUL.md") == "two"


def test_delete_account_only_removes_target(fresh_db):
    ps.write_file("acc1", "SOUL.md", "s")
    ps.write_file("acc1", "USER.md", "u")
    ps.write_file("acc2", "SOUL.md", "keep")
    removed = ps.delete_account("acc1")
    assert removed == 2
    assert ps.list_filenames("acc1") == []
    assert ps.read_file("acc2", "SOUL.md") == "keep"
