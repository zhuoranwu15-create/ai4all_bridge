"""DB 后端中立垫片层（SQLite 默认 / PostgreSQL 厚节点改造）。

目的：把 SQLite 与 PostgreSQL 的方言差异收敛到本文件，让上层 app/db/*.py 的
`?` 占位符、`sqlite3.*` 引用无需逐处手改即可在两套后端运行。
设计依据见 docs/architecture/shared/data/thick_node_postgres_refactor.md §4.2。

后端由 settings.database_url 选择：
- 空字符串（默认）→ SQLite。connect_raw() 直接返回原生 sqlite3 连接，
  连接对象与改造前逐字节一致，**零行为变化**。
- postgresql://… → PostgreSQL。惰性 import psycopg；游标自动把 `?`→`%s`、
  转义字面量 `%`，Row 同时支持 row["col"] 与 row[0]。

SQLite 路径**不依赖 psycopg**；psycopg 仅在选 PG 时 import，未安装也不影响
SQLite 部署与测试。
"""
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from app.config import settings as _default_settings

# ---------------------------------------------------------------------------
# 后端中立类型 / 异常别名（供上层 type hint 与 except 使用）
# ---------------------------------------------------------------------------
# 注意：这些名字用于注解与异常捕获，不做运行时强校验。SQLite 下即 sqlite3 的
# 同名对象；PG 连接对象 duck-type 兼容，注解不受影响。
Row = sqlite3.Row
Connection = sqlite3.Connection


def _build_integrity_errors() -> Tuple[type, ...]:
    """聚合两套后端的 IntegrityError，供 `except IntegrityError` 同时捕获。

    psycopg 未安装（纯 SQLite 部署）时退化为单元素元组，except 语义不变。
    返回元组而非单类型，故 `from app.db._backend import IntegrityError` 与
    `app.db._backend.IntegrityError` 两种引用方式都稳定可用。
    """
    errors: List[type] = [sqlite3.IntegrityError]
    try:  # psycopg 仅 PG 部署存在；缺失时忽略
        import psycopg  # noqa: WPS433 (惰性 import 是有意为之)

        errors.append(psycopg.errors.IntegrityError)
    except Exception:
        pass
    return tuple(errors)


IntegrityError = _build_integrity_errors()


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


def database_url() -> str:
    return (getattr(_settings(), "database_url", "") or "").strip()


def is_postgres() -> bool:
    return database_url().lower().startswith(("postgres://", "postgresql://"))


# ---------------------------------------------------------------------------
# 占位符翻译（PG 专用，纯函数，可单测）
# ---------------------------------------------------------------------------

def translate_placeholders(sql: str) -> str:
    """把 SQLite 风格 `?` 占位符翻成 psycopg 的 `%s`，并转义字面量 `%`。

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


# ---------------------------------------------------------------------------
# 语句方言翻译（PG 专用，纯函数，可单测）
# ---------------------------------------------------------------------------
# 把仓库里用 SQLite 方言写的 DDL/DML，在 PG 后端执行前翻成 PG 版。只解决
# 「PG 跑不起来」的硬方言差异（见设计文档 §4.4），DDL 与 DML 共用同一翻译器
# （DML 里的 strftime 默认值、INSERT OR IGNORE 同样需要翻译，故不限于 DDL）：
#   1. 自增主键    INTEGER PRIMARY KEY AUTOINCREMENT → BIGINT GENERATED ... IDENTITY
#   2. 北京时间默认 strftime(... datetime('now','+8 hours')) → to_char(now() AT TIME ZONE ...)
#      —— 既出现在 DDL 列默认值，也出现在 INSERT VALUES / ON CONFLICT ... DO UPDATE SET。
#   3. 外键内联     CREATE TABLE 内的 FOREIGN KEY(...) REFERENCES ... 一律剥离
#      —— SQLite 不校验建表顺序、PG 校验，而本仓 baseline 存在前向外键引用
#      （如 tool_invocations 被先定义的表引用）。账号隔离由 app 层 WHERE account_id
#      保证，不依赖 DB 级 FK；剥离后建表顺序无关，最小改动跑通 PG。后续如需 DB 级
#      FK，可用 ALTER TABLE ADD CONSTRAINT 在建表后延迟补（留作可选优化）。
#   4. INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
#      —— PG 裸 DO NOTHING（不指定冲突目标）捕获任意唯一/主键冲突，语义等价 SQLite。
#   5. 查询体日期函数（窗口/限流/分析）：
#      - datetime('now','+8 hours'[, <mod>])  → to_char((now() AT TIME ZONE ...)[ + (<mod>)::interval], 'YYYY-MM-DD HH24:MI:SS')
#        <mod> 各形态（'-1 hour' / ? / ?||' minutes' / f-string 字面）恰好都是合法 PG interval 表达式。
#        保持 to_char 文本输出，匹配 TEXT 时间戳列的字典序（=按时间）比较语义。
#      - date('now','+8 hours')             → to_char((now() AT TIME ZONE ...), 'YYYY-MM-DD')
#      - (julianday(A)-julianday(B))*86400000 → EXTRACT(EPOCH FROM ((A)::timestamp-(B)::timestamp))*1000
# 类型/语义一律不动（TEXT 时间戳、INTEGER 布尔、TEXT json 全保留）。

_PG_NOW_BJ_TS = "(now() AT TIME ZONE 'Asia/Shanghai')"
_PG_NOW_BJ = "to_char(" + _PG_NOW_BJ_TS + ",'YYYY-MM-DD HH24:MI:SS')"
_PG_NOW_BJ_DATE = "to_char(" + _PG_NOW_BJ_TS + ",'YYYY-MM-DD')"
# 带修饰符的替换串（\1 = SQLite 修饰符表达式，作为 PG interval）。括号在 re 替换串里是字面量。
_PG_NOW_BJ_MOD_REPL = (
    r"to_char((now() AT TIME ZONE 'Asia/Shanghai') + (\1)::interval,'YYYY-MM-DD HH24:MI:SS')"
)

# strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))，容忍空白差异
_STRFTIME_BJ_RE = re.compile(
    r"strftime\(\s*'%Y-%m-%d %H:%M:%S'\s*,\s*"
    r"datetime\(\s*'now'\s*,\s*'\+8 hours'\s*\)\s*\)"
)

# strftime(... datetime('now','+8 hours', <mod>) ...) —— 带第三参修饰符，整体一并翻译
_STRFTIME_BJ_MOD_RE = re.compile(
    r"strftime\(\s*'%Y-%m-%d %H:%M:%S'\s*,\s*"
    r"datetime\(\s*'now'\s*,\s*'\+8 hours'\s*,\s*([^()]+?)\s*\)\s*\)"
)

# datetime('now', '+8 hours', <mod>)（裸用于 WHERE/VALUES，带第三参修饰符）
_DATETIME_BJ_MOD_RE = re.compile(
    r"datetime\(\s*'now'\s*,\s*'\+8 hours'\s*,\s*([^()]+?)\s*\)"
)

# datetime('now', '+8 hours')（裸形，无 strftime 包裹、无修饰符）
_DATETIME_BJ_RE = re.compile(r"datetime\(\s*'now'\s*,\s*'\+8 hours'\s*\)")

# datetime('now', <mod>) —— 不带 '+8 hours' 的裸 UTC 形（SQLite 'now' 即 UTC）。
# 业务代码一律带 '+8 hours'（北京时间），此形仅见于测试辅助 SQL（如强制过期
# datetime('now','-1 minute')）。须在上面两条 '+8 hours' 规则之后运行，避免误抢。
_DATETIME_UTC_MOD_RE = re.compile(r"datetime\(\s*'now'\s*,\s*([^()]+?)\s*\)")
_PG_DATETIME_UTC_MOD_REPL = r"to_char(now() + (\1)::interval,'YYYY-MM-DD HH24:MI:SS')"

# date('now', '+8 hours') —— 北京当日 YYYY-MM-DD
_DATE_BJ_RE = re.compile(r"date\(\s*'now'\s*,\s*'\+8 hours'\s*\)")

# (julianday(A) - julianday(B)) * 86400000 —— 两个 TEXT 时间戳的毫秒差
_JULIANDAY_MS_RE = re.compile(
    r"\(\s*julianday\(\s*([^()]+?)\s*\)\s*-\s*julianday\(\s*([^()]+?)\s*\)\s*\)\s*\*\s*86400000"
)
_PG_JULIANDAY_MS_REPL = r"EXTRACT(EPOCH FROM ((\1)::timestamp - (\2)::timestamp)) * 1000"

# ---------------------------------------------------------------------------
# JSON 函数：SQLite json_extract/json_valid/json_patch → PG jsonb 运算（TEXT 列存 JSON）
# ---------------------------------------------------------------------------
# json_extract(X, '$.a.b') → (X::jsonb #>> '{a,b}')（取文本值；缺失路径得 NULL，语义一致）
_JSON_EXTRACT_RE = re.compile(r"json_extract\(\s*([^,()]+?)\s*,\s*'\$\.([^']*)'\s*\)")


def _json_extract_repl(m: "re.Match") -> str:
    col = m.group(1)
    # 路径各段分别作为 ARRAY 元素传入注册的 PG 函数，避免 ::jsonb 强转对脏数据抛异常
    parts = ", ".join(f"'{p}'" for p in m.group(2).split("."))
    return f"safe_json_extract_text({col}, ARRAY[{parts}])"


# json_valid(X)：不在垫片层翻译，由 _ensure_pg_functions 注册同名 PG 函数处理
# （原 IS JSON 谓词要求 PG16；注册函数兼容 PG14+，且对脏 JSON 安全返回 false）

# json_patch(A, ?) → json_patch(A::jsonb, (?)::jsonb)::text
# PG 无内置 json_patch；init_db 时建一个 RFC 7396 递归实现的同名函数（见 _core._ensure_pg_functions），
# 严格保留「patch 值为 null 即删除该键」语义（浅 || 合并做不到，会把键留成 null）。结果转回 TEXT 存列。
# A 可含一层括号/逗号如 COALESCE(col,'{}')，故用 DOTALL 非贪婪到 `, ?)`。
_JSON_PATCH_RE = re.compile(r"json_patch\(\s*(.+?)\s*,\s*\?\s*\)", re.DOTALL)
_PG_JSON_PATCH_REPL = r"json_patch((\1)::jsonb, (?)::jsonb)::text"

# CASE WHEN ? —— SQLite 把整型 0/1 当布尔；PG 的 CASE WHEN 必须是 boolean。
# 改写为 `CASE WHEN ? = 1`（参数仍是 0/1 整型，两后端都得 boolean）。
# 容忍 CASE 与 WHEN 间的换行/多空白（如多行 CASE\n  WHEN ?），保留原前导空白。
_CASE_WHEN_PARAM_RE = re.compile(r"(CASE\s+WHEN\s+)\?(?!\s*=)")
_PG_CASE_WHEN_REPL = r"\1? = 1"

# CREATE TABLE 内的列类型 INTEGER → BIGINT：SQLite INTEGER 是 64 位，PG INTEGER 仅 32 位，
# 大额计数（micros 等）会溢出 int4。仅在建表语句内替换，避免误伤 DML 里的 CAST AS INTEGER。
_INTEGER_TYPE_RE = re.compile(r"\bINTEGER\b")

# CREATE TABLE 内联外键子句（含可选前导逗号），整段剥离
_INLINE_FK_RE = re.compile(
    r",?\s*FOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s+\w+\s*\([^)]*\)",
    re.IGNORECASE,
)

# 剥外键后可能在 ) 前留下悬挂逗号，收尾清理
_DANGLING_COMMA_RE = re.compile(r",(\s*)\)")

# INSERT OR IGNORE INTO ... —— 仅匹配语句前缀，容忍大小写与空白
_INSERT_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)


def translate_statement(sql: str) -> str:
    """把单条 SQLite 方言语句（DDL 或 DML）翻成 PG 版（仅 PG 路径调用）。

    纯字符串变换，幂等；不含任何方言标记的语句快速返回，对运行期普通查询零开销。
    必须按「单条语句」调用：INSERT OR IGNORE 的 ON CONFLICT 追加在语句末尾，多语句
    脚本须先 split_sql_statements 再逐条翻译（见 _PgConnection.executescript）。
    """
    if not any(tok in sql for tok in (
        "AUTOINCREMENT", "strftime", "FOREIGN", "IGNORE",
        "datetime", "julianday", "date('now'", "json_", "CASE WHEN ?",
    )):
        return sql
    out = sql
    # 日期/时间函数：带修饰符的形态必须先于无修饰符替换，否则无修饰符正则会先吃掉
    # `datetime('now','+8 hours'` 前缀、留下悬挂的 `, <mod>)`。
    if "strftime" in out:
        out = _STRFTIME_BJ_MOD_RE.sub(_PG_NOW_BJ_MOD_REPL, out)
        out = _STRFTIME_BJ_RE.sub(_PG_NOW_BJ, out)
    if "datetime" in out:
        out = _DATETIME_BJ_MOD_RE.sub(_PG_NOW_BJ_MOD_REPL, out)
        out = _DATETIME_BJ_RE.sub(_PG_NOW_BJ, out)
        # 兜底：剩余的裸 datetime('now', <mod>)（UTC，仅测试辅助 SQL）
        out = _DATETIME_UTC_MOD_RE.sub(_PG_DATETIME_UTC_MOD_REPL, out)
    if "date('now'" in out:
        out = _DATE_BJ_RE.sub(_PG_NOW_BJ_DATE, out)
    if "julianday" in out:
        out = _JULIANDAY_MS_RE.sub(_PG_JULIANDAY_MS_REPL, out)
    # JSON 函数
    if "json_extract" in out:
        out = _JSON_EXTRACT_RE.sub(_json_extract_repl, out)
    if "json_patch" in out:
        out = _JSON_PATCH_RE.sub(_PG_JSON_PATCH_REPL, out)
    # json_valid(X) 不翻译——由 _ensure_pg_functions 注册的同名 PG 函数直接处理
    # CASE WHEN ?（整型布尔）
    if "CASE WHEN ?" in out:
        out = _CASE_WHEN_PARAM_RE.sub(_PG_CASE_WHEN_REPL, out)
    if "AUTOINCREMENT" in out:
        out = out.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
        )
    if "CREATE TABLE" in out and "INTEGER" in out:
        # 建表列类型 INTEGER → BIGINT（须在 AUTOINCREMENT→IDENTITY 之后，避免重复命中）
        out = _INTEGER_TYPE_RE.sub("BIGINT", out)
    if "FOREIGN" in out:
        # 仅在确有外键时才做剥离 + 悬挂逗号清理，避免对普通 DML 误伤 `, )` 序列
        out = _INLINE_FK_RE.sub("", out)
        out = _DANGLING_COMMA_RE.sub(r"\1)", out)
    if _INSERT_OR_IGNORE_RE.search(out):
        out = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", out)
        out = out.rstrip().rstrip(";")
        out = out + " ON CONFLICT DO NOTHING"
    return out


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
# SQLite 后端（默认；与改造前完全一致）
# ---------------------------------------------------------------------------

def _connect_sqlite() -> sqlite3.Connection:
    """打开 SQLite 连接并设置 bridge 级 PRAGMA（搬自原 _core.connect）。"""
    from app.db._core import _db_path  # 延迟 import 避免与 _core 循环依赖

    conn = sqlite3.connect(_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        # WAL 的标准搭档：commit 不再每次 fsync，仅在 checkpoint 时落盘。
        # 最坏情况（OS 崩溃/断电）只丢断电前最后几条已提交事务，绝不损坏库。
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# PostgreSQL 后端（惰性；psycopg 包装为 sqlite3 兼容接口）
# ---------------------------------------------------------------------------
# 说明：本环境暂无 psycopg / 本地 PG，PG 路径尚未运行期验证，待 PG 实例就绪后
# 按 §4.8 验收。SQLite 路径已通过现有测试套验证。

class _HybridRow:
    """同时支持 row["col"]（dict 风格）与 row[0]（index 风格）的行对象。

    复刻 sqlite3.Row 的双访问能力：上层既有 `row["x"]` 也有 `row[0]`、`dict(row)`、
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
    """psycopg 游标的 sqlite3 兼容包装：execute 时翻译占位符。"""

    def __init__(self, cur) -> None:
        self._cur = cur

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None):
        # 先做方言翻译（strftime/AUTOINCREMENT/FK/INSERT OR IGNORE），无参/带参同等处理；
        # 普通查询会被 translate_statement 快速放行，零额外开销。
        sql = translate_statement(sql)
        if params is None:
            self._cur.execute(sql)
        else:
            # 占位符翻译须在方言翻译之后：strftime 的 '%...' 已先被替换，避免被 % 转义误伤
            self._cur.execute(translate_placeholders(sql), params)
        return self

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]):
        self._cur.executemany(translate_placeholders(translate_statement(sql)), seq_of_params)
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
        # PG 无 lastrowid，用 lastval() 复刻 sqlite 语义：返回本连接(会话)最近一次
        # nextval 的值。IDENTITY 自增列底层走序列，INSERT 后 lastval() 即新生成的 id，
        # 与 sqlite3 lastrowid「本连接最后插入行 rowid」对齐。仅在自增表 INSERT 后读取
        # （本仓所有读 lastrowid 的插入目标主键列均为 id）；其间不应有其他 nextval 介入。
        with self._cur.connection.cursor() as c:
            c.execute("SELECT lastval()")
            return int(c.fetchone()[0])

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PgConnection:
    """psycopg 连接的 sqlite3 兼容包装。

    复刻 sqlite3.Connection 的关键用法：`conn.execute(sql, params).fetchone()`、
    `conn.cursor()`、`conn.commit()/rollback()/close()`。
    """

    def __init__(self, conn, pool=None) -> None:
        self._conn = conn
        # 来自连接池则 close() 归还而非物理关闭;无池(理论兜底)时退化为直接关闭。
        self._pool = pool

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> _PgCursor:
        cur = _PgCursor(self._conn.cursor())
        return cur.execute(sql, params)

    def executescript(self, script: str) -> None:
        # 先按语句切分、再逐条翻译执行（INSERT OR IGNORE 的 ON CONFLICT 追加须按单条进行，
        # 不能在整脚本层面追加；比依赖 psycopg 多语句更确定）。
        for stmt in split_sql_statements(script):
            self._conn.execute(translate_statement(stmt))

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
# 仅 PG 部署构建；SQLite 路径完全不触达本段。
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
    """按 settings.database_url 打开底层连接（不含事务管理）。

    事务（commit/rollback/close）由上层 app.db._core.connect 的 contextmanager 负责，
    两套后端共用同一套事务语义。
    """
    if is_postgres():
        return _connect_postgres()
    return _connect_sqlite()
