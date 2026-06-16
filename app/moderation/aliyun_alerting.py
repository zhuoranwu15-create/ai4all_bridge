"""阿里云内容审核失败率监控与飞书告警。

触发条件（满足其一即告警，受冷却时间限制）：
- 连续失败次数 >= 阈值（默认 3）。
- 滑动窗口（默认 5 分钟）内失败率 > 阈值（默认 20%），且样本量达到下限（默认 5）。

监控对象只保存调用结果的状态，阈值在每次 record 时从 settings 读取，便于运行时调整。
告警经 redact 脱敏后异步发送，绝不阻塞主对话链路。
"""

import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Deque, Optional, Tuple

from app.alerting import _send_feishu_text, redact_alert_text
from app.config import settings

logger = logging.getLogger("ai4all.moderation.aliyun_alerting")


class AliyunFailureMonitor:
    """线程安全的阿里云审核失败率监控；只持有状态，阈值由调用方传入。"""

    def __init__(self) -> None:
        self._events: Deque[Tuple[float, bool]] = deque()
        self._consecutive_failures = 0
        self._last_alert_at: Optional[float] = None
        self._lock = threading.Lock()

    def record(
        self,
        success: bool,
        *,
        window_seconds: int,
        failure_rate: float,
        min_samples: int,
        consecutive_threshold: int,
        cooldown_seconds: int,
        now: float,
    ) -> Optional[Tuple[str, str]]:
        """记录一次调用结果；需要告警且已过冷却时返回 (reason, detail)，否则 None。"""

        with self._lock:
            self._events.append((now, success))
            cutoff = now - max(1, window_seconds)
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

            if success:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

            total = len(self._events)
            failures = sum(1 for _, ok in self._events if not ok)

            reason: Optional[str] = None
            detail = ""
            if self._consecutive_failures >= max(1, consecutive_threshold):
                reason = "consecutive_failures"
                detail = f"连续失败 {self._consecutive_failures} 次"
            elif total >= max(1, min_samples) and (failures / total) > failure_rate:
                reason = "failure_rate"
                detail = f"{window_seconds}s 内失败率 {failures}/{total}={failures / total:.0%}"

            if reason is None:
                return None

            # 冷却：避免持续抖动时刷屏告警。
            if self._last_alert_at is not None and (now - self._last_alert_at) < max(0, cooldown_seconds):
                return None
            self._last_alert_at = now
            return reason, detail

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._consecutive_failures = 0
            self._last_alert_at = None


_monitor = AliyunFailureMonitor()


def _format_alert(*, reason: str, detail: str, error: Optional[str], account_id: str) -> str:
    lines = [
        "[AI4ALL][P1] 阿里云内容审核失败告警",
        f"env={str(getattr(settings, 'app_env', '') or '')}",
        f"reason={reason}",
        f"detail={detail}",
        f"last_error={error or ''}",
        f"account={account_id}",
        f"alerted_at={datetime.now().isoformat(timespec='seconds')}",
    ]
    return redact_alert_text("\n".join(lines))


def _dispatch_async(webhook_url: str, message: str, timeout: float) -> None:
    def _run() -> None:
        try:
            _send_feishu_text(webhook_url, message, timeout)
        except Exception as err:  # 告警发送失败不得影响主链路
            logger.warning("aliyun moderation alert send failed error=%s", err)

    threading.Thread(target=_run, daemon=True).start()


def record_outcome(*, success: bool, error: Optional[str] = None, account_id: str = "") -> None:
    """记录一次阿里云审核调用结果，必要时触发飞书告警。"""

    if not bool(getattr(settings, "moderation_aliyun_alert_enabled", True)):
        return

    fired = _monitor.record(
        success,
        window_seconds=int(getattr(settings, "moderation_aliyun_alert_window_seconds", 300)),
        failure_rate=float(getattr(settings, "moderation_aliyun_alert_failure_rate", 0.2)),
        min_samples=int(getattr(settings, "moderation_aliyun_alert_min_samples", 5)),
        consecutive_threshold=int(getattr(settings, "moderation_aliyun_alert_consecutive", 3)),
        cooldown_seconds=int(getattr(settings, "moderation_aliyun_alert_cooldown_seconds", 300)),
        now=time.monotonic(),
    )
    if fired is None:
        return

    webhook_url = str(getattr(settings, "feishu_alert_webhook_url", "") or "").strip()
    if not webhook_url:
        return

    reason, detail = fired
    logger.warning("aliyun moderation failure alert reason=%s detail=%s", reason, detail)
    message = _format_alert(reason=reason, detail=detail, error=error, account_id=account_id)
    timeout = float(getattr(settings, "feishu_error_log_alert_timeout_seconds", 3.0))
    _dispatch_async(webhook_url, message, timeout)
