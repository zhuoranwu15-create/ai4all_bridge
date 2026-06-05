import threading
import time
from collections import defaultdict, deque
from typing import Dict


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: Dict[str, deque] = defaultdict(deque)

    def check_rpm(self, account_id: str, limit: int, *, window_seconds: float = 60.0) -> bool:
        if limit == 0:
            return True
        now = time.monotonic()
        window_seconds = max(float(window_seconds), 0.001)
        cutoff = now - window_seconds
        with self._lock:
            window = self._windows[account_id]
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True


rate_limiter = RateLimiter()
