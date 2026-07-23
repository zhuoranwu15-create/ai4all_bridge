"""Nooki Memory Adapter — Nooki 的任务行为记忆实现。

记忆策略（Nooki 自己定义，AI4ALL Core 不感知）：

Nooki 记录的不是通用用户画像（那是 AI4ALL 的事），而是：
- 用户的任务执行偏好（小步骤 vs 大步骤）
- 用户的放弃规律（时间段、任务类型）
- 用户的完成模式（成功完成的条件）
- 用户的常见任务类型

这些是 Nooki 的产品核心资产，不属于通用记忆，必须和 AI4ALL 通用记忆分开存储。

存储路径（本地缓存）：apps/nooki/users/{account_id}/memory.json

Dreaming 接入说明：
record_task_completion / record_task_abandonment 会同步往
profile_storage 的 memory/{date}.md 追写一行，这样现有的
DreamingScheduler 无需任何改动即可把 Nooki 任务事件纳入 dreaming 材料。
"""
from __future__ import annotations

import abc
import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nooki.memory_adapter")

_USERS_DIR = Path(__file__).parent / "users"


# ── 轻量 KV 记忆存储（仅供 Nooki 内部使用） ───────────────────────────────────


class MemoryEntry:
    """一条记忆条目（KV，带 tags）。"""
    __slots__ = ("key", "content", "tags", "created_at", "updated_at")

    def __init__(
        self,
        key: str,
        content: str,
        tags: Optional[List[str]] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow().isoformat()
        self.key = key
        self.content = content
        self.tags = tags or []
        self.created_at = created_at or now
        self.updated_at = updated_at or now

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryEntry":
        return cls(
            key=d["key"],
            content=d["content"],
            tags=d.get("tags", []),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
        )


class _FileMemoryStore:
    """JSON 文件 KV 存储后端。"""

    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._cache: Optional[Dict[str, MemoryEntry]] = None

    def _load(self) -> Dict[str, MemoryEntry]:
        if self._cache is not None:
            return self._cache
        if not self._store_path.exists():
            self._cache = {}
            return self._cache
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
            self._cache = {k: MemoryEntry.from_dict(v) for k, v in data.items()}
        except Exception as exc:
            logger.error("failed to load memory store %s: %s", self._store_path, exc)
            self._cache = {}
        return self._cache

    def _flush(self) -> None:
        store = self._load()
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._store_path.write_text(
            json.dumps({k: v.to_dict() for k, v in store.items()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save(self, entry: MemoryEntry) -> None:
        store = self._load()
        now = datetime.utcnow().isoformat()
        if entry.key in store:
            entry.created_at = store[entry.key].created_at
        entry.updated_at = now
        store[entry.key] = entry
        self._flush()

    def get(self, key: str) -> Optional[MemoryEntry]:
        return self._load().get(key)

    def list_all(self) -> List[MemoryEntry]:
        return list(self._load().values())


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _append_to_daily_notes(account_id: str, line: str) -> None:
    """往 profile_storage 的 memory/{today}.md 追写一行。

    DreamingScheduler 运行时会读该文件作为 daily_notes 材料。
    写失败只记日志，不影响主链路。
    """
    try:
        from app import profile_storage  # 延迟导入，避免模块初始化时循环依赖
        today = date.today().isoformat()
        filename = f"memory/{today}.md"
        existing = profile_storage.read_file(account_id, filename) or ""
        separator = "\n" if existing.strip() else ""
        profile_storage.write_file(account_id, filename, existing + separator + line + "\n")
    except Exception as exc:
        logger.warning("append_to_daily_notes failed account=%s: %s", account_id, exc)


# ── NookiMemoryAdapter ────────────────────────────────────────────────────────


class NookiMemoryAdapter:
    """Nooki 任务行为记忆。

    Adapter 层只做读写接口；Nooki 业务侧（如 after-turn 处理）
    决定何时调用 record_* 方法来更新记忆。
    """

    KEY_TASK_PREFERENCES = "task_preferences"
    KEY_ABANDONMENT_PATTERNS = "abandonment_patterns"
    KEY_COMPLETION_PATTERNS = "completion_patterns"
    KEY_COMMON_TASKS = "common_tasks"

    def __init__(self, account_id: str) -> None:
        if not account_id:
            raise ValueError("account_id 不能为空")
        self._account_id = account_id
        store_path = _USERS_DIR / account_id / "memory.json"
        self._runtime = _FileMemoryStore(store_path)

    def save(self, entry: MemoryEntry) -> None:
        self._runtime.save(entry)

    def retrieve_relevant(self, context: Dict[str, Any]) -> List[Dict[str, str]]:
        """根据当前 context 返回相关记忆，注入 AgentContext.memory.relevant。"""
        all_entries = self._runtime.list_all()
        if not all_entries:
            return []
        return [
            {"key": e.key, "content": e.content}
            for e in all_entries if e.content.strip()
        ]

    def summarize(self) -> str:
        """返回行为模式摘要（供 adapter 注入 memory context 使用）。"""
        entries = self._runtime.list_all()
        if not entries:
            return ""
        return "\n".join(
            f"[{e.key}] {e.content[:200]}"
            for e in entries if e.content.strip()
        )

    # ── 业务记录方法（供 after-turn 逻辑调用）────────────────────────────

    def record_task_completion(self, task_title: str, step_mode: str, duration_minutes: int) -> None:
        """记录一次任务完成。同步往 profile_storage memory/{today}.md 追写。"""
        existing = self._runtime.get(self.KEY_COMPLETION_PATTERNS)
        content = existing.content if existing else ""
        ts = datetime.now().strftime("%m-%d %H:%M")
        line = f"- [{ts}] 完成「{task_title}」，模式={step_mode}，耗时={duration_minutes}分钟"
        content += f"\n{line}"
        self._runtime.save(MemoryEntry(
            key=self.KEY_COMPLETION_PATTERNS,
            content=content.strip(),
            tags=["completion", "task"],
        ))
        _append_to_daily_notes(self._account_id, f"[nooki/task_complete] {line.lstrip('- ')}")

    def record_task_abandonment(self, task_title: str, reason: str = "") -> None:
        """记录一次任务放弃。同步往 profile_storage memory/{today}.md 追写。"""
        existing = self._runtime.get(self.KEY_ABANDONMENT_PATTERNS)
        content = existing.content if existing else ""
        ts = datetime.now().strftime("%m-%d %H:%M")
        hour = datetime.now().hour
        time_segment = "凌晨" if hour < 6 else "上午" if hour < 12 else "下午" if hour < 18 else "晚上"
        detail = f"「{task_title}」" + (f"（原因：{reason}）" if reason else "")
        line = f"- [{ts} {time_segment}] 放弃 {detail}"
        content += f"\n{line}"
        self._runtime.save(MemoryEntry(
            key=self.KEY_ABANDONMENT_PATTERNS,
            content=content.strip(),
            tags=["abandonment", "task"],
        ))
        _append_to_daily_notes(self._account_id, f"[nooki/task_abandon] {line.lstrip('- ')}")
