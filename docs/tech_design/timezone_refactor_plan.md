# 时区改造 Plan

更新时间：2026-06-06

目标：全项目统一使用**北京时间（UTC+8）**存储时间戳，消除 UTC/本地时间混用。

---

## 现状梳理

### 已完成

- **`app/db.py` SQL 层（显式写入）**：所有 INSERT/UPDATE 中的 `CURRENT_TIMESTAMP` 已替换为 `strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))`。
- **`app/main.py` 展示层**：`_beijing_display()` 简化，假设 DB 全为北京时间；`_normalize_ts()` helper 新增；reactivation admin 视图已对齐。
- **`scripts/migrate_timestamps_to_beijing.py`**：一次性历史数据迁移脚本已写好（dry-run 模式，需手动 `--apply`）。

### 尚未完成

#### A. Schema `DEFAULT CURRENT_TIMESTAMP`（SQLite 写 UTC）

`db.py` 中所有建表语句的 `DEFAULT CURRENT_TIMESTAMP` 仍是 UTC。
风险：任何 INSERT 若漏写这些列，SQLite 自动填入的是 UTC，和应用层北京时间混存。
目前评估：所有 INSERT 路径均已显式赋值，风险低但存在隐患。

处置方案（选择其一）：
- **方案 A（推荐）**：Schema 默认值改为 `DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))`，彻底封堵漏洞，同时向后兼容（TEXT 列，格式不变）。
- **方案 B**：维持现状，靠代码规范保证不漏写，文档标注风险。

#### B. Python 侧 `datetime.now()` / `datetime.now(timezone.utc)` 调用

这些调用依赖系统时区（服务器若为 UTC 则写入 UTC 字符串），或显式写 UTC。需要统一改为北京时间。

| 文件 | 行 | 用途 | 风险等级 |
|---|---|---|---|
| `app/memory_writer.py` | 78 | `sent_at` 写入 DB metadata | **高**（显式 UTC） |
| `app/turn_service.py` | 171 | `_now` 传入 `prompt_builder`，写入 DB `now=` | **高** |
| `app/turn_service.py` | 408 | `now = datetime.now()` → `today`，与 DB 时间比较 | **高** |
| `app/dreaming_scheduler.py` | 47 | `now = datetime.now()` → 与 DB `next_scan_at` 比较 | **高** |
| `app/dreaming_scheduler.py` | 103 | `_seconds_until_next_window(datetime.now(), ...)` | **高** |
| `app/onboarding.py` | 422 | `datetime.now() - updated`，`updated` 来自 DB（已北京时间） | **高** |
| `app/session_lifecycle.py` | 165 | `current = now or datetime.now()` → 与 DB 比较 | **高** |
| `app/main.py` | 1239 | `strftime` → 返回字符串，用于 DB 写入 fallback | **中** |
| `app/main.py` | 1340, 1385 | `checked_at` response 字段（非持久化） | 低 |
| `app/main.py` | 1485, 1675 | `_now_preview / _now_lab` → prompt builder | **中** |
| `app/main.py` | 2262, 2270 | latency 计算（duration only，不存时间戳） | 无影响 |
| `app/main.py` | 3307 | `current = datetime.now()` → scheduler/proactive 逻辑 | **高** |
| `app/main.py` | 3606 | `generated_at` 写入 candidate metadata | **中** |
| `app/main.py` | 3945 | `now = datetime.now()` → reactivation dispatch | **高** |
| `app/web_search.py` | 116,174,263,347,509,644 | `retrieved_at` 写入搜索 metadata JSON | 低（非 DB 时间列） |

#### C. 历史数据迁移（脚本已就绪，未执行）

`scripts/migrate_timestamps_to_beijing.py --apply` 尚未对 `data/ai4all.sqlite3` 执行。

---

## 执行计划

### Step 1：引入 `beijing_now()` 统一工具函数

**文件**：`app/time_utils.py`（新建）

```python
from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))

def beijing_now() -> datetime:
    """Return current time as timezone-aware Beijing (UTC+8) datetime."""
    return datetime.now(BEIJING_TZ)

def beijing_now_str() -> str:
    """Return current Beijing time as naive string for DB storage: 'YYYY-MM-DD HH:MM:SS'."""
    return beijing_now().strftime("%Y-%m-%d %H:%M:%S")
```

所有 Python 侧时间调用统一改用 `beijing_now()` / `beijing_now_str()`。

---

### Step 2：修复高风险 Python datetime 调用

按文件逐一改：

**2a. `app/memory_writer.py:78`**
```python
# 改前
timestamp = sent_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
# 改后
timestamp = sent_at or beijing_now_str()
```

**2b. `app/turn_service.py:171`**
```python
# 改前
_now = now or datetime.now()
# 改后
_now = now or beijing_now()
```

**2c. `app/turn_service.py:408`**
```python
# 改前
now = datetime.now()
# 改后
now = beijing_now()
```

**2d. `app/dreaming_scheduler.py:47`**
```python
# 改前
now = datetime.now()
# 改后
now = beijing_now()
```

**2e. `app/dreaming_scheduler.py:103`**
```python
# 改前
_seconds_until_next_window(datetime.now(), ...)
# 改后
_seconds_until_next_window(beijing_now(), ...)
```

**2f. `app/onboarding.py:422`**
```python
# 改前
idle_minutes = (datetime.now() - updated).total_seconds() / 60
# 改后（updated 来自 DB，已是 naive 北京时间，需用 naive beijing_now）
idle_minutes = (beijing_now().replace(tzinfo=None) - updated).total_seconds() / 60
```

**2g. `app/session_lifecycle.py:165`**
```python
# 改前
current = now or datetime.now()
# 改后
current = now or beijing_now()
```

**2h. `app/main.py:1239`**
```python
# 改前
return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
# 改后
return beijing_now_str()
```

**2i. `app/main.py:1485, 1675`**（`_now_preview`, `_now_lab`）
```python
# 改前
_now_preview = datetime.now()
# 改后
_now_preview = beijing_now()
```

**2j. `app/main.py:3307`**
```python
# 改前
current = datetime.now()
# 改后
current = beijing_now()
```

**2k. `app/main.py:3606`**
```python
# 改前
"generated_at": datetime.now().replace(microsecond=0).isoformat(sep=" ")
# 改后
"generated_at": beijing_now().replace(microsecond=0, tzinfo=None).isoformat(sep=" ")
```

**2l. `app/main.py:3945`**
```python
# 改前
now = datetime.now()
# 改后
now = beijing_now()
```

---

### Step 3：修复 Schema DEFAULT CURRENT_TIMESTAMP（方案 A）

`app/db.py` 所有建表语句中的：
```sql
DEFAULT CURRENT_TIMESTAMP
```
统一改为：
```sql
DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
```

涉及约 60+ 处。用脚本或编辑器批量替换即可，改完后验证建表语句语法。

> ⚠️ 这是 DDL 改动，不影响已有数据，只影响新建表时的默认值。线上 SQLite 表结构不会自动 ALTER，但迁移后的新实例会正确。测试环境使用内存 SQLite，每次测试都重建表，所以改完即生效。

---

### Step 4：执行历史数据迁移

```bash
# 先 dry-run 确认行数
.venv/bin/python scripts/migrate_timestamps_to_beijing.py

# 执行迁移（自动备份 DB）
.venv/bin/python scripts/migrate_timestamps_to_beijing.py --apply
```

**执行前确认**：
- `main.py` / `turn_service.py` 等变更已 commit 且测试通过
- 有足够磁盘空间存备份

---

### Step 5：`web_search.py` retrieved_at（低优先级）

`retrieved_at` 只写入搜索 metadata JSON，不作为 DB 时间列参与排序/过滤。暂不强制改，后续有一致性需求再统一。

---

## 测试要点

```bash
# 快速回归（改动覆盖模块）
.venv/bin/pytest tests/test_turn_service.py tests/test_session_lifecycle.py tests/test_db.py -v

# 全量
.venv/bin/pytest tests/ -v
```

手工验证：
1. 发一条消息，查 `messages.created_at` 是否为北京时间（约当前时间，不早 8 小时）
2. Scheduler dry-run：`python scripts/diagnose_reactivation.py --dispatch-dry-run` 观察 due 判断是否正确
3. Admin UI 时间显示是否正常（无 +8h 偏移）

---

## 进度跟踪

- [x] `db.py` SQL 显式写入全部替换
- [x] `main.py` 展示层对齐
- [x] 迁移脚本编写完成
- [ ] **Step 1**：新建 `app/time_utils.py`
- [ ] **Step 2**：修复 Python `datetime.now()` 调用（a–l）
- [ ] **Step 3**：Schema DEFAULT 替换
- [ ] **Step 4**：执行历史数据迁移（需用户确认）
- [ ] **Step 5**：全量测试通过
