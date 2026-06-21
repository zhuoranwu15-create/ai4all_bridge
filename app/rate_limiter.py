"""账号级滑动窗口限流（厚节点改造 P3，见 docs/tech_design/thick_node_postgres_refactor.md §5）。

check_rpm 在单一 DB 事务内：清过期命中行、计数、未达限则插入。
无进程内状态，多节点共享同一 PG 库时天然跨进程隔离正确。
"""
import time
from typing import Optional

from app.db._core import _tx


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


rate_limiter = RateLimiter()
