"""账号级滑动窗口限流（厚节点改造 P3，见 docs/tech_design/thick_node_postgres_refactor.md §5）。

check_rpm 在单一 DB 事务内：清过期命中行、计数、未达限则插入。
无进程内状态，多节点共享同一 PG 库时天然跨进程隔离正确。

并发正确性：DELETE→COUNT→INSERT 这套「检查后插入」在 PG 多写者（aliyun1+aliyun2
并发写同一库，READ COMMITTED）下存在 TOCTOU——两请求可同时 COUNT 到未超限再各自
INSERT，击穿 RPM。故 PG 路径在事务开头按账号取**事务级 advisory 锁**串行化同账号的
检查-插入（不同账号互不阻塞）。SQLite 单写者本就串行，无需加锁（_advisory 为 no-op）。
"""
import hashlib
import time
from typing import Optional

from app.db._backend import is_postgres
from app.db._core import _tx, product_quota_subject


def product_rpm_subject(*, platform_user_id: str, app_id: str) -> str:
    """返回产品级 RPM 存储 subject，与 daily advisory lock 使用同一编码。"""
    return product_quota_subject(
        platform_user_id=platform_user_id,
        app_id=app_id,
    )


def _advisory_key(account_id: str) -> int:
    """把 account_id 映射成稳定的 64 位有符号整数，作 pg_advisory_xact_lock 的键。

    用 blake2b 取 8 字节（而非 PG 内置 hashtext 的 32 位），跨进程稳定且碰撞极低；
    偶发碰撞只会让两个无关账号短暂互斥，仅轻微竞争、不影响正确性。
    """
    digest = hashlib.blake2b(account_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


class RateLimiter:
    """DB-backed 滑动窗口 RPM 限流器。无实例状态，线程安全由 DB 事务保证。"""

    def check_rpm(
        self,
        account_id: str,
        limit: int,
        *,
        window_seconds: float = 60.0,
        _now: Optional[float] = None,
    ) -> bool:
        """检查并记录一次请求；True=通过，False=已达限额（被拒请求不计入）。

        _now: 注入 Unix epoch 时间（仅供测试时控制时钟，生产不传）。
        """
        if limit == 0:
            return True
        now = _now if _now is not None else time.time()
        cutoff = now - max(float(window_seconds), 0.001)
        with _tx(None) as tx:
            if is_postgres():
                # 事务级 advisory 锁：同账号的检查-插入串行化，避免并发击穿 RPM。
                # 锁随事务提交/回滚自动释放，无需显式解锁。SQLite 路径跳过（单写者已串行）。
                tx.execute("SELECT pg_advisory_xact_lock(?)", (_advisory_key(account_id),))
            tx.execute(
                "DELETE FROM rpm_hits WHERE account_id = ? AND hit_at < ?",
                (account_id, cutoff),
            )
            row = tx.execute(
                "SELECT COUNT(*) AS cnt FROM rpm_hits WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row["cnt"] >= limit:
                return False
            tx.execute(
                "INSERT INTO rpm_hits(account_id, hit_at) VALUES (?, ?)",
                (account_id, now),
            )
            return True

    def check_product_rpm(
        self,
        *,
        platform_user_id: str,
        app_id: str,
        limit: int,
        window_seconds: float = 60.0,
        _now: Optional[float] = None,
    ) -> bool:
        """按 ``(platform_user_id, app_id)`` 检查并记录一次产品 RPM。"""
        return self.check_rpm(
            product_rpm_subject(
                platform_user_id=platform_user_id,
                app_id=app_id,
            ),
            limit,
            window_seconds=window_seconds,
            _now=_now,
        )


rate_limiter = RateLimiter()
