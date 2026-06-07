# Intent Gate 重构 + 完整提醒功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 turn_service 从 600 行大函数重构为薄编排层，用 LLM tool use 替代正则意图识别，实现完整提醒功能（一次性 + 周期性 + 取消/更新），修复频控 PRD 偏差。

**Architecture:** `app/tools/` 定义提醒工具的 schema 和 handler；`app/llm.py` 扩展支持单轮 tool use；`turn_service.py` 仅保留 `#命令` 前置过滤，其余统一走 `generate_reply_with_tools()`；`TurnContext` dataclass 在 `app/turn_context.py` 中集中定义，供 tools handler 和 turn_service 共享。

**Tech Stack:** Python 3.11+, SQLite (via app/db.py), httpx, pytest, OpenAI-compatible chat completions API with tool use (finish_reason=tool_calls)

---

## File Map

| 操作 | 路径 | 职责 |
|---|---|---|
| 新建 | `app/turn_context.py` | TurnContext dataclass |
| 新建 | `app/reminder_utils.py` | validate_due_at, validate_recur_rule, compute_next_due_at |
| 新建 | `app/tools/__init__.py` | 导出 get_reminder_tools() |
| 新建 | `app/tools/definitions.py` | 四个工具的 OpenAI schema |
| 新建 | `app/tools/executor.py` | execute_tool_call() 分发器 |
| 新建 | `app/tools/reminder_handlers.py` | create/list/cancel/update handler |
| 修改 | `app/db.py` | 新增迁移列 + update_reminder() + create_reminder() 加 recur_rule + mark_reminder_sent() 支持 recur |
| 修改 | `app/llm.py` | 新增 generate_reply_with_tools() + _http_chat_with_tools() |
| 修改 | `app/proactive/messaging.py` | 新增 product_category 参数 |
| 修改 | `app/proactive/reminders.py` | 传 product_category="user_reminder"，dispatch 后处理 recur |
| 修改 | `app/prompt_builder.py` | 新增 tool_instructions 参数 |
| 修改 | `app/turn_service.py` | 重构：TurnContext + generate_reply_with_tools，移除 reminder_parser 引用 |
| 修改 | `tests/conftest.py` | 添加 generate_reply_with_tools mock |
| 删除 | `app/reminder_parser.py` | 意图识别逻辑下线（LLM tool use 替代） |
| 重命名测试 | `tests/test_reminder_parser.py` → 清理 | 意图识别测试下线 |
| 新建测试 | `tests/test_reminder_utils.py` | 校验工具函数 |
| 新建测试 | `tests/test_tools_definitions.py` | schema 结构校验 |
| 新建测试 | `tests/test_tools_handlers.py` | handler 逻辑测试 |
| 新建测试 | `tests/test_llm_tools.py` | generate_reply_with_tools 测试 |
| 修改测试 | `tests/test_turn_reminders.py` | 改为验证工具路径 |
| 修改测试 | `tests/test_proactive_outbound.py` | product_category 测试 |

---

## Task 1: DB 迁移 — 新增 recur 列 + update_reminder() + create_reminder() recur 支持

**Files:**
- Modify: `app/db.py`
- Test: `tests/test_reminders.py`（扩展）

- [ ] **Step 1: 在 test_reminders.py 末尾添加 recur 列测试**

```python
def test_reminder_recur_columns_exist(fresh_db):
    from app.db import create_reminder, get_reminder
    with patch("app.db.settings", fresh_db):
        from app.db import get_or_create_session
        get_or_create_session(
            account_id="acc-recur",
            channel="openclaw-weixin",
            sender_id="s",
            sender_name=None,
            chat_id="c",
            session_key="sk-recur",
        )
        r = create_reminder(
            account_id="acc-recur",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="user",
            session_key="sk-recur",
            text="每周提醒",
            due_at="2026-06-07 09:00:00",
            recur_rule="weekly:5",
        )
        assert r["recur_rule"] == "weekly:5"
        assert r["sent_count"] == 0
        assert r["last_sent_at"] is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/suchong/workspace/ai4all/weixin_bot
python -m pytest tests/test_reminders.py::test_reminder_recur_columns_exist -v
```

期望：FAIL，`create_reminder() got unexpected keyword argument 'recur_rule'`

- [ ] **Step 3: 在 `init_db()` 中添加迁移列**

在 `app/db.py` 的 `init_db()` 函数中，`CREATE TABLE IF NOT EXISTS reminders` 语句之后添加：

```python
        # Migrate: add recur columns if not present (idempotent)
        for _ddl in [
            "ALTER TABLE reminders ADD COLUMN recur_rule TEXT",
            "ALTER TABLE reminders ADD COLUMN sent_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE reminders ADD COLUMN last_sent_at TEXT",
        ]:
            try:
                conn.execute(_ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
```

- [ ] **Step 4: 给 `create_reminder()` 加 `recur_rule` 参数**

在 `app/db.py` 的 `create_reminder()` 中：

```python
def create_reminder(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    text: str,
    due_at: str,
    reminder_id: Optional[str] = None,
    recur_rule: Optional[str] = None,      # NEW
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
```

在 INSERT 语句中加入 recur_rule：

```python
        conn.execute(
            """
            INSERT INTO reminders(
                id, account_id, channel, channel_account_id, to_user_id,
                session_key, text, due_at, recur_rule, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                cleaned_reminder_id,
                cleaned_account_id,
                cleaned_channel,
                _clean_text(channel_account_id),
                cleaned_to_user_id,
                _clean_text(session_key),
                cleaned_text,
                cleaned_due_at,
                _clean_text(recur_rule) if recur_rule else None,   # NEW
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
```

- [ ] **Step 5: 新增 `update_reminder()` 函数**

在 `app/db.py` 的 `cancel_reminder()` 函数之后添加：

```python
def update_reminder(
    *,
    reminder_id: str,
    text: Optional[str] = None,
    due_at: Optional[str] = None,
    recur_rule: Optional[str] = None,
    clear_recur_rule: bool = False,
) -> Optional[Dict[str, Any]]:
    fields: list[str] = []
    values: list = []
    if text is not None:
        fields.append("text = ?")
        values.append(_clean_text(text))
    if due_at is not None:
        fields.append("due_at = ?")
        values.append(_clean_text(due_at))
    if recur_rule is not None:
        fields.append("recur_rule = ?")
        values.append(_clean_text(recur_rule))
    elif clear_recur_rule:
        fields.append("recur_rule = NULL")
    if not fields:
        return get_reminder(reminder_id=reminder_id)
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(reminder_id)
    with connect() as conn:
        conn.execute(
            f"UPDATE reminders SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    return _decode_reminder(row) if row else None
```

- [ ] **Step 6: 更新 `mark_reminder_sent()` 支持 recur 重置**

将现有的 `mark_reminder_sent()` 替换为：

```python
def mark_reminder_sent(
    *,
    reminder_id: str,
    outbound_message_id: Optional[int] = None,
    next_due_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        if next_due_at:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'pending',
                    due_at = ?,
                    sent_count = sent_count + 1,
                    last_sent_at = CURRENT_TIMESTAMP,
                    claimed_at = NULL,
                    outbound_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_due_at, outbound_message_id, reminder_id),
            )
        else:
            conn.execute(
                """
                UPDATE reminders
                SET status = 'sent',
                    sent_count = sent_count + 1,
                    last_sent_at = CURRENT_TIMESTAMP,
                    outbound_message_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (outbound_message_id, reminder_id),
            )
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    return _decode_reminder(row) if row else None
```

- [ ] **Step 7: 运行测试确认通过**

```bash
python -m pytest tests/test_reminders.py -v
```

期望：所有测试 PASS

- [ ] **Step 8: Commit**

```bash
git add app/db.py tests/test_reminders.py
git commit -m "feat: add recur_rule/sent_count/last_sent_at columns, update_reminder(), recur-aware mark_reminder_sent()"
```

---

## Task 2: reminder_utils.py — 校验函数 + compute_next_due_at()

**Files:**
- Create: `app/reminder_utils.py`
- Create: `tests/test_reminder_utils.py`

- [ ] **Step 1: 新建测试文件**

```python
# tests/test_reminder_utils.py
import pytest
from datetime import datetime
from app.reminder_utils import compute_next_due_at, validate_due_at, validate_recur_rule


def test_validate_due_at_standard_format():
    dt = validate_due_at("2026-06-01 10:00:00")
    assert dt == datetime(2026, 6, 1, 10, 0, 0)


def test_validate_due_at_iso_format():
    dt = validate_due_at("2026-06-01T10:00:00")
    assert dt == datetime(2026, 6, 1, 10, 0, 0)


def test_validate_due_at_invalid_raises():
    with pytest.raises(ValueError):
        validate_due_at("not-a-date")


def test_validate_recur_rule_daily():
    assert validate_recur_rule("daily") == "daily"


def test_validate_recur_rule_weekly():
    assert validate_recur_rule("weekly:6") == "weekly:6"


def test_validate_recur_rule_monthly():
    assert validate_recur_rule("monthly:15") == "monthly:15"


def test_validate_recur_rule_invalid_raises():
    with pytest.raises(ValueError):
        validate_recur_rule("hourly")


def test_compute_next_due_at_daily():
    base = datetime(2026, 5, 30, 9, 0, 0)
    nxt = compute_next_due_at("daily", base)
    assert nxt == datetime(2026, 5, 31, 9, 0, 0)


def test_compute_next_due_at_weekly():
    # base is Saturday (weekday=5), target is Sunday (6)
    base = datetime(2026, 5, 30, 9, 0, 0)  # Saturday
    nxt = compute_next_due_at("weekly:6", base)
    assert nxt == datetime(2026, 5, 31, 9, 0, 0)  # next Sunday


def test_compute_next_due_at_weekly_wraps():
    # base is Sunday (6), target is Monday (0): should be next Monday
    base = datetime(2026, 6, 7, 9, 0, 0)  # Sunday
    nxt = compute_next_due_at("weekly:0", base)
    assert nxt == datetime(2026, 6, 8, 9, 0, 0)  # next Monday


def test_compute_next_due_at_monthly():
    base = datetime(2026, 5, 15, 9, 0, 0)
    nxt = compute_next_due_at("monthly:15", base)
    assert nxt == datetime(2026, 6, 15, 9, 0, 0)


def test_compute_next_due_at_monthly_clamps_to_month_end():
    # monthly:31 in February → clamps to Feb 28
    base = datetime(2026, 1, 31, 9, 0, 0)
    nxt = compute_next_due_at("monthly:31", base)
    assert nxt.month == 2
    assert nxt.day == 28
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_reminder_utils.py -v
```

期望：FAIL，`ModuleNotFoundError: No module named 'app.reminder_utils'`

- [ ] **Step 3: 创建 `app/reminder_utils.py`**

```python
import calendar
import re
from datetime import datetime, timedelta
from typing import Optional

_VALID_RECUR_RE = re.compile(
    r"^(daily|weekly:[0-6]|monthly:([1-9]|[12][0-9]|3[01]))$"
)


def validate_due_at(value: str) -> datetime:
    """Parse and validate LLM-provided due_at. Raises ValueError on bad input."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid due_at format: {value!r}")


def validate_recur_rule(value: str) -> str:
    """Validate and normalize recur_rule. Raises ValueError on bad input."""
    normalized = value.strip().lower()
    if not _VALID_RECUR_RE.match(normalized):
        raise ValueError(f"Invalid recur_rule: {value!r}")
    return normalized


def compute_next_due_at(recur_rule: str, last_due_at: datetime) -> datetime:
    """Compute the next trigger datetime for a recurring reminder."""
    if recur_rule == "daily":
        return last_due_at + timedelta(days=1)

    if recur_rule.startswith("weekly:"):
        target_weekday = int(recur_rule.split(":")[1])  # 0=Mon, 6=Sun
        days_ahead = target_weekday - last_due_at.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return last_due_at + timedelta(days=days_ahead)

    if recur_rule.startswith("monthly:"):
        target_day = int(recur_rule.split(":")[1])
        month = last_due_at.month + 1
        year = last_due_at.year
        if month > 12:
            month = 1
            year += 1
        max_day = calendar.monthrange(year, month)[1]
        day = min(target_day, max_day)
        return last_due_at.replace(year=year, month=month, day=day)

    raise ValueError(f"Unknown recur_rule: {recur_rule!r}")
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_reminder_utils.py -v
```

期望：所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add app/reminder_utils.py tests/test_reminder_utils.py
git commit -m "feat: add reminder_utils with validate_due_at, validate_recur_rule, compute_next_due_at"
```

---

## Task 3: 频控修复 — product_category 参数

**Files:**
- Modify: `app/proactive/messaging.py`
- Test: `tests/test_proactive_outbound.py`（扩展）

- [ ] **Step 1: 在 test_proactive_outbound.py 末尾添加 product_category 测试**

```python
def test_user_reminder_bypasses_quiet_hours(fresh_db):
    from app.proactive.messaging import enqueue_proactive_text
    from unittest.mock import patch

    with patch("app.proactive.messaging.settings", fresh_db):
        with patch("app.db.settings", fresh_db):
            from app.db import get_or_create_session
            get_or_create_session(
                account_id="acc-cat",
                channel="openclaw-weixin",
                sender_id="s",
                sender_name=None,
                chat_id="c",
                session_key="sk-cat",
            )
        # 22:30 is inside quiet hours (22:00–08:00)
        result = enqueue_proactive_text(
            account_id="acc-cat",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="user",
            session_key="sk-cat",
            source="reminder",
            text="时间到了",
            now=__import__("datetime").datetime(2026, 5, 30, 22, 30),
            product_category="user_reminder",
        )
        assert result["status"] == "pending", f"Expected pending, got {result['status']}: {result.get('error')}"


def test_companion_followup_blocked_by_quiet_hours(fresh_db):
    from app.proactive.messaging import enqueue_proactive_text
    from unittest.mock import patch

    with patch("app.proactive.messaging.settings", fresh_db):
        with patch("app.db.settings", fresh_db):
            from app.db import get_or_create_session
            get_or_create_session(
                account_id="acc-comp",
                channel="openclaw-weixin",
                sender_id="s",
                sender_name=None,
                chat_id="c",
                session_key="sk-comp",
            )
        result = enqueue_proactive_text(
            account_id="acc-comp",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="user",
            session_key="sk-comp",
            source="commitment",
            text="跟进一下",
            now=__import__("datetime").datetime(2026, 5, 30, 22, 30),
            product_category="companion_followup",
        )
        assert result["status"] == "cancelled"
        assert result["error"] == "quiet_hours"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_proactive_outbound.py::test_user_reminder_bypasses_quiet_hours tests/test_proactive_outbound.py::test_companion_followup_blocked_by_quiet_hours -v
```

期望：FAIL，`enqueue_proactive_text() got unexpected keyword argument 'product_category'`

- [ ] **Step 3: 修改 `app/proactive/messaging.py`**

在 `enqueue_proactive_text()` 中加入 `product_category` 参数，并据此覆盖 `bypass_quiet_hours` 和日上限行为：

```python
_UNLIMITED_CATEGORIES = {"user_reminder", "task_result"}
_BYPASS_QUIET_HOURS_CATEGORIES = {"user_reminder", "task_result"}


def enqueue_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,   # NEW
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    current = now or datetime.now()
    quota_date = current.date().isoformat()
    status = "pending"
    error = None
    merged_metadata = {
        **(metadata or {}),
        "policy_checked_at": current.isoformat(timespec="seconds"),
    }
    if product_category:
        merged_metadata["product_category"] = product_category

    # product_category overrides bypass_quiet_hours
    effective_bypass = bypass_quiet_hours or (
        product_category in _BYPASS_QUIET_HOURS_CATEGORIES
    )
    skip_daily_limit = product_category in _UNLIMITED_CATEGORIES

    if not getattr(settings, "proactive_outbound_enabled", True):
        # user_reminder ignores the global toggle
        if product_category != "user_reminder":
            status = "cancelled"
            error = "proactive_outbound_disabled"
    
    if status == "pending" and not effective_bypass and is_quiet_hours(
        now=current,
        start=getattr(settings, "proactive_quiet_hours_start", "22:00"),
        end=getattr(settings, "proactive_quiet_hours_end", "08:00"),
    ):
        status = "cancelled"
        error = "quiet_hours"

    if status == "pending" and not skip_daily_limit:
        max_per_day = int(getattr(settings, "proactive_outbound_daily_limit", 0) or 0)
        if max_per_day > 0:
            current_count = get_outbound_daily_usage(
                account_id=account_id,
                quota_date=quota_date,
            )
            if current_count >= max_per_day:
                status = "cancelled"
                error = "daily_limit_exceeded"
                merged_metadata["daily_count"] = current_count
                merged_metadata["daily_limit"] = max_per_day

    if error:
        merged_metadata["policy_error"] = error

    return create_outbound_message(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
        quota_date=quota_date,
        status=status,
        error=error,
        metadata=merged_metadata,
    )
```

同样给 `send_proactive_text()` 加上 `product_category` 并透传给 `enqueue_proactive_text()`：

```python
def send_proactive_text(
    *,
    account_id: str,
    channel: str,
    channel_account_id: Optional[str],
    to_user_id: str,
    session_key: Optional[str],
    source: str,
    text: str,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
    product_category: Optional[str] = None,   # NEW
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    outbound = enqueue_proactive_text(
        account_id=account_id,
        channel=channel,
        channel_account_id=channel_account_id,
        to_user_id=to_user_id,
        session_key=session_key,
        source=source,
        text=text,
        idempotency_key=idempotency_key,
        now=now,
        bypass_quiet_hours=bypass_quiet_hours,
        product_category=product_category,    # NEW
        metadata=metadata,
    )
    # rest unchanged
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_proactive_outbound.py -v
```

期望：所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add app/proactive/messaging.py tests/test_proactive_outbound.py
git commit -m "feat: add product_category to enqueue/send_proactive_text, user_reminder bypasses quiet_hours and daily limit"
```

---

## Task 4: TurnContext dataclass

**Files:**
- Create: `app/turn_context.py`
- Create: `tests/test_turn_context.py`

- [ ] **Step 1: 新建测试文件**

```python
# tests/test_turn_context.py
from app.turn_context import TurnContext


def test_turn_context_fields():
    ctx = TurnContext(
        account_id="acc-1",
        account={"id": "acc-1"},
        session={"id": 1},
        identity=None,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id="msg-1",
        text="你好",
        today="2026-05-30",
        business_day="2026-05-30",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )
    assert ctx.account_id == "acc-1"
    assert ctx.binding["chat_id"] == "chat-1"
    assert ctx.recent_messages == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_turn_context.py -v
```

期望：FAIL，`ModuleNotFoundError: No module named 'app.turn_context'`

- [ ] **Step 3: 创建 `app/turn_context.py`**

```python
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional


@dataclass
class TurnContext:
    account_id: str
    account: dict
    session: dict
    identity: Any                     # OpenClawIdentity
    binding: dict
    message_id: str
    text: str
    today: str
    business_day: str
    profile_path: Optional[Path]
    debug_trace_enabled: bool
    onboarding_state: str
    onboarding_active: bool
    recent_messages: List[dict] = field(default_factory=list)
    background_loop: Optional[asyncio.AbstractEventLoop] = None
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_turn_context.py -v
```

期望：PASS

- [ ] **Step 5: Commit**

```bash
git add app/turn_context.py tests/test_turn_context.py
git commit -m "feat: add TurnContext dataclass"
```

---

## Task 5: Tool 定义 + executor

**Files:**
- Create: `app/tools/__init__.py`
- Create: `app/tools/definitions.py`
- Create: `app/tools/executor.py`
- Create: `tests/test_tools_definitions.py`

- [ ] **Step 1: 新建测试文件**

```python
# tests/test_tools_definitions.py
from app.tools import get_reminder_tools


def test_get_reminder_tools_returns_four_tools():
    tools = get_reminder_tools()
    names = {t["function"]["name"] for t in tools}
    assert names == {"create_reminder", "list_reminders", "cancel_reminder", "update_reminder"}


def test_each_tool_has_required_fields():
    for tool in get_reminder_tools():
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"


def test_create_reminder_requires_text_and_due_at():
    tools = get_reminder_tools()
    create = next(t for t in tools if t["function"]["name"] == "create_reminder")
    required = create["function"]["parameters"]["required"]
    assert "text" in required
    assert "due_at" in required


def test_cancel_reminder_requires_reminder_id():
    tools = get_reminder_tools()
    cancel = next(t for t in tools if t["function"]["name"] == "cancel_reminder")
    assert "reminder_id" in cancel["function"]["parameters"]["required"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_tools_definitions.py -v
```

期望：FAIL，`ModuleNotFoundError: No module named 'app.tools'`

- [ ] **Step 3: 创建 `app/tools/definitions.py`**

```python
# app/tools/definitions.py


def get_reminder_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "create_reminder",
                "description": (
                    "创建一个提醒。用于用户明确要求在未来某个时间收到提醒的场景。"
                    "due_at 格式为 YYYY-MM-DD HH:MM:SS。"
                    "recur_rule 可选：daily（每天）、weekly:N（每周，N=0 周一…6 周日）、"
                    "monthly:D（每月第 D 日）。不填为一次性提醒。"
                    "时间不明确时不要猜测，告知用户需要补充具体日期和时间。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "提醒内容，简短描述要提醒的事情",
                        },
                        "due_at": {
                            "type": "string",
                            "description": "触发时间，格式 YYYY-MM-DD HH:MM:SS",
                        },
                        "recur_rule": {
                            "type": "string",
                            "description": "周期规则，可选。daily / weekly:N / monthly:D",
                        },
                    },
                    "required": ["text", "due_at"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_reminders",
                "description": (
                    "列出用户当前所有待发送的提醒。"
                    "查看、取消或修改提醒前应先调用此工具确认。"
                    "返回的每条提醒包含 id 字段，cancel_reminder 和 update_reminder 需要用到。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_reminder",
                "description": (
                    "取消一个待发送的提醒。"
                    "如不确定 reminder_id，先调用 list_reminders 确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reminder_id": {
                            "type": "string",
                            "description": "要取消的提醒的 id（从 list_reminders 获得）",
                        },
                    },
                    "required": ["reminder_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_reminder",
                "description": (
                    "修改已有提醒的内容、时间或周期。只传要修改的字段，其余保持不变。"
                    "如不确定 reminder_id，先调用 list_reminders 确认。"
                    "recur_rule 传 null 表示改为一次性提醒。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reminder_id": {
                            "type": "string",
                            "description": "要修改的提醒的 id",
                        },
                        "text": {
                            "type": "string",
                            "description": "新的提醒内容，可选",
                        },
                        "due_at": {
                            "type": "string",
                            "description": "新的触发时间，可选，格式 YYYY-MM-DD HH:MM:SS",
                        },
                        "recur_rule": {
                            "type": ["string", "null"],
                            "description": "新的周期规则，可选。传 null 改为一次性。",
                        },
                    },
                    "required": ["reminder_id"],
                },
            },
        },
    ]
```

- [ ] **Step 4: 创建 `app/tools/__init__.py`**

```python
from app.tools.definitions import get_reminder_tools

__all__ = ["get_reminder_tools"]
```

- [ ] **Step 5: 创建 `app/tools/executor.py`**

```python
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.executor")


def execute_tool_call(name: str, args: dict, ctx: "TurnContext") -> dict:
    """Dispatch a tool call to the appropriate handler. Never raises — returns error dict on failure."""
    from app.tools.reminder_handlers import (
        handle_cancel_reminder,
        handle_create_reminder,
        handle_list_reminders,
        handle_update_reminder,
    )

    handlers = {
        "create_reminder": handle_create_reminder,
        "list_reminders": handle_list_reminders,
        "cancel_reminder": handle_cancel_reminder,
        "update_reminder": handle_update_reminder,
    }
    handler = handlers.get(name)
    if handler is None:
        logger.warning("execute_tool_call unknown tool: %s", name)
        return {"error": f"未知工具: {name}"}
    try:
        return handler(args, ctx)
    except Exception as err:
        logger.exception("tool handler failed tool=%s error=%s", name, err)
        return {"error": str(err)}
```

- [ ] **Step 6: 运行测试确认通过**

```bash
python -m pytest tests/test_tools_definitions.py -v
```

期望：所有测试 PASS

- [ ] **Step 7: Commit**

```bash
git add app/tools/ tests/test_tools_definitions.py
git commit -m "feat: add app/tools/ with tool definitions and executor"
```

---

## Task 6: Reminder tool handlers

**Files:**
- Create: `app/tools/reminder_handlers.py`
- Create: `tests/test_tools_handlers.py`

- [ ] **Step 1: 新建测试文件**

```python
# tests/test_tools_handlers.py
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest

from app.turn_context import TurnContext


def _make_ctx(account_id="acc-tool", fresh_db=None):
    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = "sk-1"
    return TurnContext(
        account_id=account_id,
        account={"id": account_id},
        session={"id": 1},
        identity=identity,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id="msg-1",
        text="测试",
        today="2026-05-30",
        business_day="2026-05-30",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )


def _setup_account(account_id: str) -> None:
    from app.db import get_or_create_session
    get_or_create_session(
        account_id=account_id,
        channel="openclaw-weixin",
        sender_id="s",
        sender_name=None,
        chat_id="chat-1",
        session_key=f"sk-{account_id}",
    )


def test_handle_create_reminder_one_shot(fresh_db):
    from app.tools.reminder_handlers import handle_create_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-tool")
    ctx = _make_ctx()
    with patch("app.db.settings", fresh_db), patch("app.tools.reminder_handlers.settings", fresh_db, create=True):
        result = handle_create_reminder(
            {"text": "检查事情A", "due_at": "2026-06-01 10:00:00"},
            ctx,
        )
    assert result["status"] == "created"
    assert "reminder_id" in result
    assert result["recur_rule"] is None


def test_handle_create_reminder_recurring(fresh_db):
    from app.tools.reminder_handlers import handle_create_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-tool2")
    ctx = _make_ctx("acc-tool2")
    with patch("app.db.settings", fresh_db):
        result = handle_create_reminder(
            {"text": "给爸妈打电话", "due_at": "2026-06-07 09:00:00", "recur_rule": "weekly:6"},
            ctx,
        )
    assert result["status"] == "created"
    assert result["recur_rule"] == "weekly:6"


def test_handle_create_reminder_invalid_due_at(fresh_db):
    from app.tools.reminder_handlers import handle_create_reminder
    ctx = _make_ctx()
    result = handle_create_reminder({"text": "test", "due_at": "not-a-date"}, ctx)
    assert "error" in result


def test_handle_list_reminders_empty(fresh_db):
    from app.tools.reminder_handlers import handle_list_reminders
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-list")
    ctx = _make_ctx("acc-list")
    with patch("app.db.settings", fresh_db):
        result = handle_list_reminders({}, ctx)
    assert result["reminders"] == []


def test_handle_cancel_reminder(fresh_db):
    from app.db import create_reminder
    from app.tools.reminder_handlers import handle_cancel_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-cancel")
        r = create_reminder(
            account_id="acc-cancel",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="chat-1",
            session_key="sk-cancel",
            text="要取消的提醒",
            due_at="2026-06-01 10:00:00",
        )
    ctx = _make_ctx("acc-cancel")
    with patch("app.db.settings", fresh_db):
        result = handle_cancel_reminder({"reminder_id": r["id"]}, ctx)
    assert result["status"] == "cancelled"


def test_handle_cancel_reminder_wrong_account(fresh_db):
    from app.db import create_reminder
    from app.tools.reminder_handlers import handle_cancel_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-owner")
        r = create_reminder(
            account_id="acc-owner",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="chat-1",
            session_key="sk-o",
            text="别人的提醒",
            due_at="2026-06-01 10:00:00",
        )
    ctx = _make_ctx("acc-intruder")  # different account
    with patch("app.db.settings", fresh_db):
        result = handle_cancel_reminder({"reminder_id": r["id"]}, ctx)
    assert "error" in result


def test_handle_update_reminder(fresh_db):
    from app.db import create_reminder
    from app.tools.reminder_handlers import handle_update_reminder
    with patch("app.db.settings", fresh_db):
        _setup_account("acc-upd")
        r = create_reminder(
            account_id="acc-upd",
            channel="openclaw-weixin",
            channel_account_id="bot",
            to_user_id="chat-1",
            session_key="sk-upd",
            text="原内容",
            due_at="2026-06-01 10:00:00",
        )
    ctx = _make_ctx("acc-upd")
    with patch("app.db.settings", fresh_db):
        result = handle_update_reminder(
            {"reminder_id": r["id"], "text": "新内容", "due_at": "2026-06-02 10:00:00"},
            ctx,
        )
    assert result["status"] == "updated"
    assert result["reminder"]["text"] == "新内容"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_tools_handlers.py -v
```

期望：FAIL，`ModuleNotFoundError: No module named 'app.tools.reminder_handlers'`

- [ ] **Step 3: 创建 `app/tools/reminder_handlers.py`**

```python
import logging
from typing import TYPE_CHECKING

from app.db import (
    cancel_reminder,
    create_reminder,
    get_reminder,
    list_reminders_for_account,
    update_reminder,
)
from app.reminder_utils import validate_due_at, validate_recur_rule

if TYPE_CHECKING:
    from app.turn_context import TurnContext

logger = logging.getLogger("ai4all.tools.reminder_handlers")


def handle_create_reminder(args: dict, ctx: "TurnContext") -> dict:
    text = str(args.get("text", "")).strip()
    due_at_raw = str(args.get("due_at", "")).strip()
    recur_rule_raw = args.get("recur_rule")

    if not text:
        return {"error": "提醒内容不能为空"}
    try:
        due_dt = validate_due_at(due_at_raw)
    except ValueError as e:
        return {"error": f"时间格式无效：{e}"}

    recur_rule = None
    if recur_rule_raw:
        try:
            recur_rule = validate_recur_rule(str(recur_rule_raw))
        except ValueError as e:
            return {"error": f"周期规则无效：{e}"}

    to_user_id = ctx.binding.get("chat_id") or getattr(ctx.identity, "chat_id", None)
    if not to_user_id:
        return {"error": "缺少发送目标，无法创建提醒"}

    reminder = create_reminder(
        account_id=ctx.account_id,
        channel=ctx.identity.channel,
        channel_account_id=ctx.identity.channel_account_id,
        to_user_id=to_user_id,
        session_key=ctx.identity.session_key,
        text=text,
        due_at=due_dt.strftime("%Y-%m-%d %H:%M:%S"),
        recur_rule=recur_rule,
        metadata={"source": "tool_use", "source_message_id": ctx.message_id},
    )
    return {
        "status": "created",
        "reminder_id": reminder["id"],
        "due_at": reminder["due_at"],
        "recur_rule": recur_rule,
    }


def handle_list_reminders(args: dict, ctx: "TurnContext") -> dict:
    reminders = list_reminders_for_account(
        account_id=ctx.account_id,
        status="pending",
        limit=20,
    )
    return {
        "reminders": [
            {
                "id": r["id"],
                "text": r["text"],
                "due_at": r["due_at"],
                "recur_rule": r.get("recur_rule"),
            }
            for r in reminders
        ]
    }


def handle_cancel_reminder(args: dict, ctx: "TurnContext") -> dict:
    reminder_id = str(args.get("reminder_id", "")).strip()
    if not reminder_id:
        return {"error": "reminder_id 不能为空"}
    reminder = get_reminder(reminder_id=reminder_id)
    if not reminder or reminder["account_id"] != ctx.account_id:
        return {"error": "提醒不存在或无权操作"}
    if reminder["status"] != "pending":
        return {"error": f"该提醒状态为 {reminder['status']}，无法取消"}
    cancel_reminder(reminder_id=reminder_id)
    return {"status": "cancelled", "reminder_id": reminder_id, "text": reminder["text"]}


def handle_update_reminder(args: dict, ctx: "TurnContext") -> dict:
    reminder_id = str(args.get("reminder_id", "")).strip()
    if not reminder_id:
        return {"error": "reminder_id 不能为空"}
    reminder = get_reminder(reminder_id=reminder_id)
    if not reminder or reminder["account_id"] != ctx.account_id:
        return {"error": "提醒不存在或无权操作"}
    if reminder["status"] != "pending":
        return {"error": f"该提醒状态为 {reminder['status']}，无法修改"}

    update_kwargs: dict = {}
    if "text" in args and args["text"] is not None:
        update_kwargs["text"] = str(args["text"]).strip()
    if "due_at" in args and args["due_at"] is not None:
        try:
            dt = validate_due_at(str(args["due_at"]))
            update_kwargs["due_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError as e:
            return {"error": f"时间格式无效：{e}"}
    if "recur_rule" in args:
        if args["recur_rule"] is None:
            update_kwargs["clear_recur_rule"] = True
        else:
            try:
                update_kwargs["recur_rule"] = validate_recur_rule(str(args["recur_rule"]))
            except ValueError as e:
                return {"error": f"周期规则无效：{e}"}

    if not update_kwargs:
        return {"error": "没有提供要修改的字段"}

    updated = update_reminder(reminder_id=reminder_id, **update_kwargs)
    if not updated:
        return {"error": "更新失败"}
    return {
        "status": "updated",
        "reminder": {
            "id": updated["id"],
            "text": updated["text"],
            "due_at": updated["due_at"],
            "recur_rule": updated.get("recur_rule"),
        },
    }
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_tools_handlers.py -v
```

期望：所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add app/tools/reminder_handlers.py app/tools/executor.py tests/test_tools_handlers.py
git commit -m "feat: add reminder tool handlers and executor"
```

---

## Task 7: llm.py — generate_reply_with_tools()

**Files:**
- Modify: `app/llm.py`
- Create: `tests/test_llm_tools.py`

- [ ] **Step 1: 新建测试文件**

```python
# tests/test_llm_tools.py
import json
from unittest.mock import MagicMock, patch

import pytest

from app.turn_context import TurnContext


def _make_ctx():
    identity = MagicMock()
    identity.channel = "openclaw-weixin"
    identity.channel_account_id = "bot-1"
    identity.chat_id = "chat-1"
    identity.session_key = "sk-1"
    return TurnContext(
        account_id="acc-1",
        account={"id": "acc-1"},
        session={"id": 1},
        identity=identity,
        binding={"id": 1, "chat_id": "chat-1"},
        message_id="msg-1",
        text="测试",
        today="2026-05-30",
        business_day="2026-05-30",
        profile_path=None,
        debug_trace_enabled=False,
        onboarding_state="complete",
        onboarding_active=False,
        recent_messages=[],
        background_loop=None,
    )


def _direct_text_response(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
    }


def _tool_call_response(tool_name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def test_generate_reply_with_tools_direct_response():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_model = "test-model"
    settings_mock.llm_base_url = "http://fake-llm"
    settings_mock.llm_timeout_seconds = 30
    settings_mock.llm_connect_timeout_seconds = 5
    settings_mock.llm_max_retries = 0
    settings_mock.llm_force_ipv4 = False
    settings_mock.llm_default_prompt = "你是助手"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=_direct_text_response("好的，我明白了")) as mock_chat:
            reply, err = generate_reply_with_tools(
                user_text="你好",
                history=[],
                system_prompt="你是助手",
                tools=[],
                ctx=_make_ctx(),
            )

    assert err is None
    assert reply == "好的，我明白了"
    mock_chat.assert_called_once()


def test_generate_reply_with_tools_tool_call():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = "test-key"
    settings_mock.llm_model = "test-model"
    settings_mock.llm_base_url = "http://fake-llm"
    settings_mock.llm_timeout_seconds = 30
    settings_mock.llm_connect_timeout_seconds = 5
    settings_mock.llm_max_retries = 0
    settings_mock.llm_force_ipv4 = False
    settings_mock.llm_default_prompt = "你是助手"

    tool_resp = _tool_call_response("create_reminder", {"text": "开会", "due_at": "2026-06-01 10:00:00"})
    tool_result = {"status": "created", "reminder_id": "rem-1", "due_at": "2026-06-01 10:00:00"}
    final_text = "好的，我会在6月1日上午10点提醒你开会。"

    with patch("app.llm.settings", settings_mock):
        with patch("app.llm._http_chat_with_tools", return_value=tool_resp):
            with patch("app.llm._http_chat", return_value=final_text):
                with patch("app.tools.executor.execute_tool_call", return_value=tool_result):
                    reply, err = generate_reply_with_tools(
                        user_text="明天上午10点提醒我开会",
                        history=[],
                        system_prompt="你是助手",
                        tools=[{"type": "function", "function": {"name": "create_reminder"}}],
                        ctx=_make_ctx(),
                    )

    assert err is None
    assert reply == final_text


def test_generate_reply_with_tools_no_api_key_returns_fallback():
    from app.llm import generate_reply_with_tools

    settings_mock = MagicMock()
    settings_mock.llm_api_key = ""

    with patch("app.llm.settings", settings_mock):
        reply, err = generate_reply_with_tools(
            user_text="你好",
            history=[],
            system_prompt=None,
            tools=[],
            ctx=_make_ctx(),
        )

    assert err is None
    assert "你好" in reply
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_llm_tools.py -v
```

期望：FAIL，`ImportError: cannot import name 'generate_reply_with_tools' from 'app.llm'`

- [ ] **Step 3: 在 `app/llm.py` 中添加 `_http_chat_with_tools()` 和 `generate_reply_with_tools()`**

在 `generate_completion()` 之后添加：

```python
def _http_chat_with_tools(messages: List[Dict], tools: List[Dict]) -> Dict:
    """LLM call with tool definitions. Returns raw response dict."""
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.7,
        "tools": tools,
        "tool_choice": "auto",
    }
    max_attempts = max(1, int(settings.llm_max_retries) + 1)
    last_request_error: Optional[httpx.RequestError] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(
                timeout=_chat_timeout(),
                trust_env=False,
                transport=_chat_transport(),
            ) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {settings.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as err:
            logger.error("llm http error status=%s body=%s", err.response.status_code, err.response.text[:500])
            raise RuntimeError(f"LLM HTTP error: {err.response.status_code}") from err
        except httpx.RequestError as err:
            last_request_error = err
            logger.warning("llm request failed attempt=%s/%s error=%s", attempt, max_attempts, err)
            if attempt >= max_attempts:
                raise RuntimeError("LLM request failed") from err
            time.sleep(min(0.2 * attempt, 1.0))
    raise RuntimeError("LLM request failed") from last_request_error


def generate_reply_with_tools(
    *,
    user_text: str,
    history: List[Dict],
    system_prompt: Optional[str],
    tools: List[Dict],
    ctx,
) -> tuple:
    """LLM call with tool use support. Returns (reply_text, error_str | None)."""
    if not settings.llm_api_key:
        return _fallback_reply(user_text), None

    prompt = system_prompt or settings.llm_default_prompt
    messages: List[Dict] = [{"role": "system", "content": prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    try:
        response = _http_chat_with_tools(messages, tools)
    except RuntimeError as err:
        return "", str(err)

    choice = response.get("choices", [{}])[0]
    finish_reason = choice.get("finish_reason", "")
    message = choice.get("message", {})

    if finish_reason in ("stop", "end_turn"):
        content = (message.get("content") or "").strip()
        if not content:
            return "", "llm_empty_response"
        return content, None

    if finish_reason == "tool_calls":
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return "", "tool_calls_missing"
        tool_call = tool_calls[0]
        tool_name = tool_call["function"]["name"]
        try:
            tool_args = json.loads(tool_call["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            tool_args = {}

        from app.tools.executor import execute_tool_call
        tool_result = execute_tool_call(tool_name, tool_args, ctx)
        tool_result_str = json.dumps(tool_result, ensure_ascii=False)

        messages2 = messages + [
            {"role": "assistant", "tool_calls": [tool_call]},
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id", "call_0"),
                "content": tool_result_str,
            },
        ]
        try:
            final_text = _http_chat(messages2)
        except RuntimeError as err:
            return "", str(err)
        return final_text, None

    return "", f"unexpected_finish_reason:{finish_reason}"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_llm_tools.py -v
```

期望：所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add app/llm.py tests/test_llm_tools.py
git commit -m "feat: add generate_reply_with_tools() and _http_chat_with_tools() to llm.py"
```

---

## Task 8: turn_service.py 重构

**Files:**
- Modify: `app/turn_service.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_turn_reminders.py`

- [ ] **Step 1: 更新 conftest.py 中的 client fixture**

在 `tests/conftest.py` 的 `client` fixture 的 patches 列表中添加：

```python
patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)),
```

完整 patches 列表变为：

```python
    patches = [
        patch("app.main.settings", fresh_db),
        patch("app.turn_service.settings", fresh_db),
        patch("app.dreaming.settings", fresh_db),
        patch("app.session_lifecycle.settings", fresh_db),
        patch("app.user_profiles.settings", fresh_db),
        patch("app.turn_service.rate_limiter", RateLimiter()),
        patch("app.turn_service.generate_reply", return_value="mock reply"),
        patch("app.turn_service.generate_reply_with_tools", return_value=("mock reply", None)),
    ]
```

- [ ] **Step 2: 运行现有测试套件，确认 baseline**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

记录当前 PASS/FAIL 数量作为 baseline。

- [ ] **Step 3: 重构 turn_service.py 的 import 区**

将现有 import 中的 `reminder_parser` 相关行替换，并添加新 import：

移除：
```python
from app.reminder_parser import looks_like_reminder_request, parse_explicit_reminder
```

添加：
```python
from app.turn_context import TurnContext
from app.tools import get_reminder_tools
from app.llm import generate_reply, generate_reply_with_tools
```

（注意：`generate_reply` 保留，onboarding 路径仍用它）

- [ ] **Step 4: 在 turn_service.py 中替换 else 分支**

找到当前大 else 分支（约第 385 行），将下面这段：

```python
        parsed_reminder = (
            parse_explicit_reminder(text)
            ...
        )
        ...
        elif reminder_intent:
            reply = "可以，我现在支持明确时间的一次性提醒。..."
        else:
            try:
                history = list_recent_messages(...)
                ...
                reply = generate_reply(...)
                normal_reply_generated = True
            except Exception as err:
                ...
```

替换为：

```python
        try:
            history = list_recent_messages(
                session_id=session["id"],
                limit=settings.llm_context_messages,
            )
            profile = session_state.get("profile") or {}
            file_profile = read_user_profile(account_id)
            soul = extract_section(file_profile, "Soul")
            user_prefs = extract_section(file_profile, "User Preferences")
            long_term_memory = extract_section(file_profile, "Long-term Memory")
            agent_context = read_agent_context(
                account_id,
                display_name=account.get("display_name"),
            )
            debug_metadata.update({
                "history_count": len(history),
                "soul_chars": len(soul),
                "user_prefs_chars": len(user_prefs),
                "long_term_memory_chars": len(long_term_memory),
                "agent_context": agent_context.metadata(),
                "system_prompt_override": bool(profile.get("system_prompt")),
                "style": profile.get("style"),
                "display_name": account.get("display_name"),
            })
            onboarding_ctx = ""
            if onboarding_active:
                onboarding_ctx = build_onboarding_prompt_context(
                    state=onboarding_state,
                    user_name=_extract_user_name_from_context(agent_context.blocks),
                    ai_name=_extract_ai_name_from_context(agent_context.blocks),
                    persona=None,
                    user_name_ask_count=_count_user_name_asks(session.get("turn_count", 0), onboarding_state),
                    persona_ask_count=0 if onboarding_state != ONBOARDING_STEP3_SENT else 1,
                )
            builder = PromptBuilder()
            system_prompt = builder.build(
                display_name=account.get("display_name"),
                soul=soul,
                user_prefs=user_prefs,
                long_term_memory=long_term_memory,
                daily_notes=None,
                carryover_summary=session.get("carryover_summary"),
                system_prompt_override=profile.get("system_prompt"),
                style=profile.get("style"),
                agent_context=agent_context.blocks,
                onboarding_context=onboarding_ctx,
                today=today,
                model_name=settings.llm_model,
            )
            llm_messages = [{"role": "system", "content": system_prompt}]
            llm_messages.extend(history)

            ctx = TurnContext(
                account_id=account_id,
                account=account,
                session=session,
                identity=identity,
                binding=binding,
                message_id=message_id,
                text=text,
                today=today,
                business_day=business_day,
                profile_path=profile_path,
                debug_trace_enabled=debug_trace_enabled,
                onboarding_state=onboarding_state,
                onboarding_active=onboarding_active,
                recent_messages=history,
                background_loop=background_loop,
            )

            if onboarding_active:
                reply = generate_reply(
                    user_text=text,
                    history=history,
                    system_prompt=system_prompt,
                )
                generation_error = None
            else:
                tools = get_reminder_tools()
                reply, generation_error = generate_reply_with_tools(
                    user_text=text,
                    history=history,
                    system_prompt=system_prompt,
                    tools=tools,
                    ctx=ctx,
                )
            normal_reply_generated = True
        except Exception as err:
            logger.exception("reply generation failed: %s", err)
            generation_error = str(err)
            reply = "我这边刚刚有点卡住了，你可以稍后再发我一次。"
```

- [ ] **Step 5: 运行完整测试套件**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -40
```

期望：之前 PASS 的测试仍然 PASS，test_turn_reminders.py 可能有失败（下一步处理）

- [ ] **Step 6: 更新 test_turn_reminders.py**

`test_turn_reminders.py` 之前测试的是"规则解析后写 DB"的路径，现在需要改为测试"工具调用路径"。将其内容替换为：

```python
# tests/test_turn_reminders.py
"""
End-to-end tests for reminder creation via the /openclaw/turn endpoint
using LLM tool use. The LLM is mocked to return a tool_calls response.
"""
import json
from unittest.mock import patch, MagicMock
import pytest


def test_turn_creates_reminder_via_tool_call(client, fresh_db):
    """When LLM returns create_reminder tool call, reminder is created in DB."""
    from app.db import list_reminders_for_account

    tool_call_response = (
        "好的，我会在2026年6月1日上午10点提醒你检查事情A。",
        None,
    )

    with patch("app.turn_service.generate_reply_with_tools", return_value=tool_call_response):
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer dev-secret"},
            json={
                "session_key": "test-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "明天上午10点提醒我检查事情A",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "提醒" in data.get("reply", "")


def test_turn_command_bypasses_llm(client, fresh_db):
    """#重置会话 special command does not call generate_reply_with_tools."""
    with patch("app.turn_service.generate_reply_with_tools") as mock_llm:
        resp = client.post(
            "/openclaw/turn",
            headers={"Authorization": "Bearer dev-secret"},
            json={
                "session_key": "test-session",
                "channel": "openclaw-weixin",
                "channel_account_id": "bot-1",
                "sender_id": "user-1",
                "chat_id": "chat-1",
                "message_type": "text",
                "text": "#重置会话",
                "chat_type": "private",
            },
        )

    assert resp.status_code == 200
    mock_llm.assert_not_called()
```

- [ ] **Step 7: 运行完整测试套件确认通过**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -40
```

期望：所有测试 PASS

- [ ] **Step 8: Commit**

```bash
git add app/turn_service.py tests/conftest.py tests/test_turn_reminders.py
git commit -m "refactor: turn_service uses TurnContext + generate_reply_with_tools, removes reminder_parser intent detection"
```

---

## Task 9: Recurring reminder dispatch

**Files:**
- Modify: `app/proactive/reminders.py`
- Test: `tests/test_reminders.py`（扩展）

- [ ] **Step 1: 在 test_reminders.py 中添加 recur dispatch 测试**

```python
def test_recurring_reminder_resets_after_dispatch(fresh_db):
    from unittest.mock import patch, MagicMock
    from app.db import create_reminder, get_reminder

    with patch("app.db.settings", fresh_db):
        _create_account("acc-recur-dispatch")
        create_reminder(
            reminder_id="rem-recur-1",
            account_id="acc-recur-dispatch",
            channel="openclaw-weixin",
            channel_account_id="bot-1",
            to_user_id="user@wechat",
            session_key="sk-rd",
            text="每周提醒",
            due_at="2026-05-30 09:00:00",
            recur_rule="weekly:5",  # 每周六
        )

    mock_send = MagicMock(return_value={"id": "out-1", "status": "sent"})
    with patch("app.db.settings", fresh_db), \
         patch("app.proactive.reminders.send_proactive_text", mock_send):
        from app.proactive.reminders import dispatch_reminder
        from datetime import datetime
        result = dispatch_reminder(
            reminder_id="rem-recur-1",
            now=datetime(2026, 5, 30, 9, 0, 0),
        )

    assert result["status"] == "sent"
    with patch("app.db.settings", fresh_db):
        updated = get_reminder(reminder_id="rem-recur-1")
    # Should have reset to pending with next Saturday's date
    assert updated["status"] == "pending"
    assert updated["sent_count"] == 1
    assert updated["due_at"] == "2026-06-06 09:00:00"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_reminders.py::test_recurring_reminder_resets_after_dispatch -v
```

期望：FAIL（`dispatch_reminder` 不处理 recur，且 `send_proactive_text` 没有 `product_category`）

- [ ] **Step 3: 更新 `app/proactive/reminders.py`**

将现有 `dispatch_reminder()` 中 `send_proactive_text` 的调用更新为传 `product_category`，并在成功后处理 recur 重置：

```python
from app.reminder_utils import compute_next_due_at
from datetime import datetime as _datetime

def dispatch_reminder(
    *,
    reminder_id: str,
    now: Optional[datetime] = None,
    bypass_quiet_hours: bool = False,
) -> Dict[str, Any]:
    current = now or datetime.now()
    claimed = claim_due_reminder(
        reminder_id=reminder_id,
        now=format_scheduler_time(current),
    )
    if claimed is None:
        return {"status": "skipped", "reason": "not_due_or_already_claimed", "reminder_id": reminder_id}

    try:
        outbound = send_proactive_text(
            account_id=claimed["account_id"],
            channel=claimed["channel"],
            channel_account_id=claimed.get("channel_account_id"),
            to_user_id=claimed["to_user_id"],
            session_key=claimed.get("session_key"),
            source="reminder",
            text=claimed["text"],
            idempotency_key=f"reminder-{claimed['id']}",
            now=current,
            product_category="user_reminder",          # NEW
            metadata={
                "reminder_id": claimed["id"],
                "reminder_due_at": claimed["due_at"],
            },
        )
    except Exception as err:
        reminder = mark_reminder_failed(reminder_id=claimed["id"], error=str(err))
        return {"status": "failed", "reminder": reminder, "error": str(err)}

    outbound_id = int(outbound["id"]) if outbound.get("id") is not None else None
    outbound_status = outbound.get("status")

    if outbound_status == "sent":
        recur_rule = claimed.get("recur_rule")
        next_due_at = None
        if recur_rule:
            last_due = _datetime.strptime(claimed["due_at"], "%Y-%m-%d %H:%M:%S")
            next_dt = compute_next_due_at(recur_rule, last_due)
            next_due_at = next_dt.strftime("%Y-%m-%d %H:%M:%S")
        reminder = mark_reminder_sent(
            reminder_id=claimed["id"],
            outbound_message_id=outbound_id,
            next_due_at=next_due_at,
        )
        return {"status": "sent", "reminder": reminder, "outbound_message": outbound}

    if outbound_status == "cancelled":
        reminder = cancel_reminder(
            reminder_id=claimed["id"],
            outbound_message_id=outbound_id,
            error=outbound.get("error") or "outbound_cancelled",
        )
        return {"status": "cancelled", "reminder": reminder, "outbound_message": outbound}

    reminder = mark_reminder_failed(
        reminder_id=claimed["id"],
        outbound_message_id=outbound_id,
        error=outbound.get("error") or f"outbound_status:{outbound_status}",
    )
    return {"status": "failed", "reminder": reminder, "outbound_message": outbound}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python -m pytest tests/test_reminders.py -v
```

期望：所有测试 PASS

- [ ] **Step 5: Commit**

```bash
git add app/proactive/reminders.py tests/test_reminders.py
git commit -m "feat: recurring reminder resets after dispatch, pass product_category=user_reminder"
```

---

## Task 10: prompt_builder.py 工具使用说明

**Files:**
- Modify: `app/prompt_builder.py`
- Test: `tests/test_prompt_builder.py`（扩展）

- [ ] **Step 1: 在 test_prompt_builder.py 末尾添加测试**

```python
def test_build_includes_tool_instructions_when_provided():
    from app.prompt_builder import PromptBuilder
    builder = PromptBuilder()
    prompt = builder.build(
        soul="",
        user_prefs="",
        long_term_memory="",
        today="2026-05-30",
        tool_instructions="## 提醒工具使用规则\n- 时间不明确时告知用户需要补充",
    )
    assert "提醒工具使用规则" in prompt


def test_build_excludes_tool_instructions_when_none():
    from app.prompt_builder import PromptBuilder
    builder = PromptBuilder()
    prompt = builder.build(
        soul="",
        user_prefs="",
        long_term_memory="",
        today="2026-05-30",
        tool_instructions=None,
    )
    assert "提醒工具使用规则" not in prompt
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python -m pytest tests/test_prompt_builder.py::test_build_includes_tool_instructions_when_provided -v
```

期望：FAIL，`build() got unexpected keyword argument 'tool_instructions'`

- [ ] **Step 3: 修改 `app/prompt_builder.py` 的 `build()` 方法**

在 `build()` 方法签名中添加 `tool_instructions: Optional[str] = None` 参数，并在适当位置（safety block 之后，在返回值组装时）追加：

```python
def build(
    self,
    *,
    display_name: Optional[str] = None,
    soul: str = "",
    user_prefs: str = "",
    long_term_memory: str = "",
    daily_notes: Optional[str] = None,
    carryover_summary: Optional[str] = None,
    system_prompt_override: Optional[str] = None,
    style: Optional[str] = None,
    agent_context: Optional[Dict[str, str]] = None,
    onboarding_context: str = "",
    today: str = "",
    model_name: str = "",
    tool_instructions: Optional[str] = None,   # NEW
) -> str:
    ...
    # 在 blocks 列表末尾添加（safety block 之后）：
    if tool_instructions:
        blocks.append(tool_instructions.strip())
    ...
```

- [ ] **Step 4: 在 turn_service.py 中传入 tool_instructions**

在 `builder.build(...)` 调用中添加：

```python
            system_prompt = builder.build(
                ...
                tool_instructions=(
                    None if onboarding_active else (
                        "## 提醒工具使用规则\n\n"
                        "- 用户明确要求在未来某个时间收到提醒时，调用 create_reminder。\n"
                        "- 时间不明确时，不要猜测，告知用户需要补充具体日期和时间。\n"
                        "- 取消或修改提醒前，先调用 list_reminders 确认提醒存在再操作。\n"
                        "- 多个提醒且用户描述不精确时，列出让用户选择，不要盲目操作。\n"
                        "- 不要承诺任何工具之外的功能（如网络搜索、发图片等）。"
                    )
                ),
            )
```

- [ ] **Step 5: 运行完整测试套件确认通过**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

期望：所有测试 PASS

- [ ] **Step 6: Commit**

```bash
git add app/prompt_builder.py app/turn_service.py tests/test_prompt_builder.py
git commit -m "feat: add tool_instructions block to prompt_builder, inject Chinese tool usage rules in system prompt"
```

---

## Task 11: 清理 reminder_parser.py

**Files:**
- Delete: `app/reminder_parser.py`
- Delete: `tests/test_reminder_parser.py`

- [ ] **Step 1: 确认无其他模块引用 reminder_parser**

```bash
grep -r "reminder_parser" /Users/suchong/workspace/ai4all/weixin_bot/app /Users/suchong/workspace/ai4all/weixin_bot/tests --include="*.py"
```

期望：零输出（turn_service.py 已在 Task 8 移除引用）。如有残余引用先修复再继续。

- [ ] **Step 2: 删除文件**

```bash
rm app/reminder_parser.py tests/test_reminder_parser.py
```

- [ ] **Step 3: 运行完整测试套件确认仍然通过**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

期望：所有测试 PASS

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: remove reminder_parser.py and its tests (replaced by LLM tool use)"
```

---

## 最终验收

- [ ] 运行完整测试套件

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

- [ ] 确认新文件已创建

```bash
ls app/tools/ app/turn_context.py app/reminder_utils.py
```

期望：`definitions.py  executor.py  reminder_handlers.py  __init__.py`，`turn_context.py`，`reminder_utils.py` 均存在

- [ ] 确认 reminder_parser.py 已删除

```bash
ls app/reminder_parser.py 2>&1
```

期望：`No such file or directory`
