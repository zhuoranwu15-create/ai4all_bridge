"""PostgreSQL persistence adapter for the main AI4ALL application."""
import threading
from typing import Any, List, Optional, Sequence

from psycopg.errors import IntegrityError

from app.config import settings as _default_settings

# ---------------------------------------------------------------------------
# Persistence type aliases used by repository annotations.
# ---------------------------------------------------------------------------
Row = Any
Connection = Any


# ---------------------------------------------------------------------------
# 后端选择
# ---------------------------------------------------------------------------

def _settings():
    """读取实时 settings（兼容测试 patch("app.db.settings")）。

    与 app.db._core._settings 同理：通过包命名空间动态读取，使测试对整个 db 层
    的 settings patch 同样路由到本模块。
    """
    import app.db as _pkg

    return getattr(_pkg, "settings", _default_settings)


def _configured_database_url() -> str:
    """Return the configured URL without asserting its backend.

    Kept private so transitional offline scripts can still inspect configuration;
    every main-runtime connection goes through :func:`database_url` below.
    """
    return (getattr(_settings(), "database_url", "") or "").strip()


def database_url() -> str:
    """Return a PostgreSQL URL or fail before any database connection is opened."""
    url = _configured_database_url()
    if not url.lower().startswith(("postgres://", "postgresql://")):
        raise RuntimeError(
            "AI4ALL main application requires PostgreSQL DATABASE_URL; "
            "SQLite and empty DATABASE_URL are no longer supported"
        )
    return url


# ---------------------------------------------------------------------------
# 占位符翻译（PG 专用，纯函数，可单测）
# ---------------------------------------------------------------------------

def translate_placeholders(sql: str) -> str:
    """把 repository 的 `?` 占位符翻成 psycopg 的 `%s`，并转义字面量 `%`。

    两点要害：
    1. 单引号字符串字面量内的 `?` 不是占位符，必须原样保留（如 `LIKE 'a?b'`）。
       连续两个单引号 `''` 是字符串内的转义单引号，不切换内/外状态。
    2. psycopg 客户端绑定会把 `%` 当占位符前缀，形如 `LIKE '%x%'` 的字面 `%`
       必须转义成 `%%`。源 SQL 一律用 `?`、从不含 `%s`，所以"先转义所有 `%`、
       再把 `?` 换成 `%s`"不会误伤新引入的 `%s`。

    仅在带参数执行时调用（无参数查询 psycopg 不解析 `%`，不可转义）。
    """
    out: List[str] = []
    in_str = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            if in_str and i + 1 < n and sql[i + 1] == "'":
                # 字符串内的转义单引号 '' —— 整体保留，不翻转状态
                out.append("''")
                i += 2
                continue
            in_str = not in_str
            out.append(ch)
        elif ch == "%":
            out.append("%%")  # 字面 % 转义（串内/串外都转义，psycopg 全局扫描占位符）
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def split_sql_statements(script: str) -> List[str]:
    """把多语句 DDL 脚本切成单条语句（PG executescript 用）。

    本仓 DDL 的字符串字面量内无分号，故先去掉 `--` 行注释再按 `;` 切分即可可靠工作。
    """
    cleaned_lines: List[str] = []
    for line in script.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)
    return [stmt.strip() for stmt in text.split(";") if stmt.strip()]


# ---------------------------------------------------------------------------
# PostgreSQL adapter (temporarily exposing the repository compatibility API).
# ---------------------------------------------------------------------------
class _HybridRow:
    """同时支持 row["col"]（dict 风格）与 row[0]（index 风格）的行对象。

    保持 repository 现有双访问契约：上层既有 `row["x"]` 也有 `row[0]`、`dict(row)`、
    `for v in row`、`"x" in row`、`row.keys()` 等用法都要兼容。
    """

    __slots__ = ("_values", "_index")

    def __init__(self, values: Sequence[Any], index: dict) -> None:
        self._values = values
        self._index = index

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __contains__(self, key) -> bool:
        return key in self._index

    def get(self, key, default=None):
        idx = self._index.get(key)
        return default if idx is None else self._values[idx]

    def keys(self) -> List[str]:
        return list(self._index.keys())

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _hybrid_row_factory(cursor):
    """psycopg3 row factory：把每行物化为 _HybridRow。"""
    desc = cursor.description
    cols = [c.name for c in desc] if desc else []
    index = {name: i for i, name in enumerate(cols)}

    def make(values):
        return _HybridRow(values, index)

    return make


class _PgCursor:
    """psycopg cursor wrapper accepting repository ``?`` placeholders."""

    def __init__(self, cur) -> None:
        self._cur = cur

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        if params is None:
            self._cur.execute(sql)
        else:
            self._cur.execute(translate_placeholders(sql), params)
        return self

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]):
        self._cur.executemany(translate_placeholders(sql), seq_of_params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size=None):
        return self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # PG 无 lastrowid，用 lastval() 实现 repository 契约：返回本连接(会话)最近一次
        # nextval 的值。IDENTITY 自增列底层走序列，INSERT 后 lastval() 即新生成的 id，
        # 自增值。仅在自增表 INSERT 后读取
        # （本仓所有读 lastrowid 的插入目标主键列均为 id）；其间不应有其他 nextval 介入。
        with self._cur.connection.cursor() as c:
            c.execute("SELECT lastval()")
            return int(c.fetchone()[0])

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PgConnection:
    """Expose the repository connection API on top of psycopg."""

    def __init__(self, conn, pool=None) -> None:
        self._conn = conn
        # 来自连接池则 close() 归还而非物理关闭;无池(理论兜底)时退化为直接关闭。
        self._pool = pool

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> _PgCursor:
        cur = _PgCursor(self._conn.cursor())
        return cur.execute(sql, params)

    def executescript(self, script: str) -> None:
        # psycopg 没有 sqlite3.executescript；迁移脚本按单条执行以保留错误定位。
        for stmt in split_sql_statements(script):
            self._conn.execute(stmt)

    def cursor(self) -> _PgCursor:
        return _PgCursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        # 池连接归还(池内部会 reset/rollback 还原干净状态);非池连接物理关闭。
        if self._pool is not None:
            self._pool.putconn(self._conn)
        else:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# PG 连接池（厚节点每轮 turn 跨机访问 PG，连接复用避免 connect churn / 降延迟 /
# 限 PG 连接数：Σ(各节点 db_pool_max_size)+中心 ≤ PG max_connections）。
# ---------------------------------------------------------------------------
_pg_pool = None
_pg_pool_conninfo: Optional[str] = None
_pg_pool_lock = threading.Lock()


def _get_pg_pool():
    """惰性构建并复用全局 PG 连接池。

    - 池大小取 settings.db_pool_min_size / db_pool_max_size；每条连接的 autocommit /
      row_factory 与单连接旧实现一致，借出/归还对上层透明，行为不变。
    - 线程安全：psycopg_pool 自身线程安全，这里仅用锁保证「只构建一次」。
    - conninfo 变化（理论上仅重配/测试）时重建并关闭旧池，避免连到旧库或句柄泄漏。
    """
    global _pg_pool, _pg_pool_conninfo
    from psycopg_pool import ConnectionPool  # 惰性：仅 PG 部署需要

    conninfo = database_url()
    if _pg_pool is not None and _pg_pool_conninfo == conninfo:
        return _pg_pool
    with _pg_pool_lock:
        if _pg_pool is not None and _pg_pool_conninfo == conninfo:
            return _pg_pool
        s = _settings()
        min_size = max(1, int(getattr(s, "db_pool_min_size", 1) or 1))
        max_size = max(min_size, int(getattr(s, "db_pool_max_size", 8) or 8))
        pool = ConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            kwargs={"autocommit": False, "row_factory": _hybrid_row_factory},
            open=False,
            name="ai4all-pg",
        )
        pool.open()
        old = _pg_pool
        _pg_pool, _pg_pool_conninfo = pool, conninfo
    if old is not None:  # 锁外关旧池，避免与归还路径互等
        try:
            old.close()
        except Exception:
            pass
    return _pg_pool


def close_pg_pool() -> None:
    """关闭全局 PG 连接池（进程优雅退出时调用；无池则 no-op）。"""
    global _pg_pool, _pg_pool_conninfo
    with _pg_pool_lock:
        pool, _pg_pool, _pg_pool_conninfo = _pg_pool, None, None
    if pool is not None:
        pool.close()


def _connect_postgres() -> "_PgConnection":
    pool = _get_pg_pool()
    conn = pool.getconn()  # 从池借出；上层 _PgConnection.close() 负责归还
    return _PgConnection(conn, pool=pool)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def connect_raw():
    """Open a PostgreSQL connection from the application pool.

    Transaction commit/rollback/close remains owned by ``app.db._core.connect``.
    ``database_url()`` validates the required PostgreSQL configuration before the
    pool is created, so an empty URL can never fall back to a local database file.
    """
    return _connect_postgres()
