"""账号级 profile 文件存储层（厚节点改造 P2，见 docs/tech_design/thick_node_postgres_refactor.md §5）。

把 SOUL/IDENTITY/USER/MEMORY/legacy user_profile.md 以及 memory/YYYY-MM-DD.md daily notes
的内容收敛进 account_profile_files 表，作为唯一真相（不再裸文件 I/O），供多节点直连共享读取。
表两后端通用（SQLite/PG 经 _backend 垫片）；account_id 严格隔离——所有读写删都以 account_id 约束。

filename 是账号 profile 目录内的相对路径（如 "SOUL.md"、"memory/2026-06-20.md"），等价于一个
按账号隔离的极简文件系统。system 级文件（AGENTS.md / TOOLS.md，在 settings.system_dir）不在本层
管辖，仍由 user_profiles.py 以文件形式维护。

所有函数都接受可选 conn：传入时复用调用方事务（供同事务串联，如 wipe），不传则各自开独立事务。
"""
from typing import List, Optional

from app.db._backend import Connection
from app.db._core import _tx
from app.time_utils import beijing_now_str


def read_file(account_id: str, filename: str, *, conn: Optional[Connection] = None) -> Optional[str]:
    """读取账号某个 profile 文件内容；文件不存在返回 None（区分「空文件」与「缺失」）。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT content FROM account_profile_files WHERE account_id = ? AND filename = ?",
            (account_id, filename),
        ).fetchone()
    return None if row is None else row["content"]


def exists(account_id: str, filename: str, *, conn: Optional[Connection] = None) -> bool:
    """账号某个 profile 文件是否存在。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT 1 FROM account_profile_files WHERE account_id = ? AND filename = ?",
            (account_id, filename),
        ).fetchone()
    return row is not None


def write_file(account_id: str, filename: str, content: str, *, conn: Optional[Connection] = None) -> None:
    """整文件写入（upsert）：不存在则插入，已存在则覆盖内容、version+1、刷新 updated_at。"""
    now = beijing_now_str()
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO account_profile_files(account_id, filename, content, version, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(account_id, filename) DO UPDATE SET
                -- 限定表名：PG 的 DO UPDATE 里 excluded 同在作用域，裸列名会歧义
                content = excluded.content,
                version = account_profile_files.version + 1,
                updated_at = excluded.updated_at
            """,
            (account_id, filename, content, now, now),
        )


def append_file(
    account_id: str,
    filename: str,
    content: str,
    *,
    new_file_prefix: str = "",
    conn: Optional[Connection] = None,
) -> None:
    """追加写（daily notes 用）。

    不存在时以 new_file_prefix + content 创建；已存在时在末尾追加 "\\n" + content。
    单条 upsert 原子执行，PG 多节点并发写同一文件不丢更新。
    """
    now = beijing_now_str()
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO account_profile_files
                (account_id, filename, content, version, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(account_id, filename) DO UPDATE SET
                content   = account_profile_files.content || '\n' || ?,
                version   = account_profile_files.version + 1,
                updated_at = excluded.updated_at
            """,
            (account_id, filename, new_file_prefix + content, now, now, content),
        )


def delete_file(account_id: str, filename: str, *, conn: Optional[Connection] = None) -> bool:
    """删除账号某个 profile 文件；返回是否删到行。"""
    with _tx(conn) as tx:
        deleted = tx.execute(
            "DELETE FROM account_profile_files WHERE account_id = ? AND filename = ?",
            (account_id, filename),
        ).rowcount
    return deleted > 0


def list_filenames(
    account_id: str, *, prefix: str = "", conn: Optional[Connection] = None
) -> List[str]:
    """列举账号下的 profile 文件名（可选按前缀过滤，如 "memory/"），按文件名升序。"""
    with _tx(conn) as tx:
        if prefix:
            rows = tx.execute(
                "SELECT filename FROM account_profile_files "
                "WHERE account_id = ? AND filename LIKE ? ORDER BY filename",
                (account_id, prefix + "%"),
            ).fetchall()
        else:
            rows = tx.execute(
                "SELECT filename FROM account_profile_files WHERE account_id = ? ORDER BY filename",
                (account_id,),
            ).fetchall()
    return [r["filename"] for r in rows]


def delete_account(account_id: str, *, conn: Optional[Connection] = None) -> int:
    """删除账号的全部 profile 文件（wipe 用），返回删除行数。"""
    with _tx(conn) as tx:
        return tx.execute(
            "DELETE FROM account_profile_files WHERE account_id = ?",
            (account_id,),
        ).rowcount
