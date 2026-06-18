# 技术设计：用户元属性建设 Phase 1

关联 PRD：`docs/product/user_meta_attributes_prd.md`
更新时间：2026-06-18
范围：Phase 1 全量（可计算元属性 + 陪伴类型 LLM 推断标签）

> 2026-06-18 确认：PRD 的 Phase 1 范围已调整为包含陪伴类型初版，本设计按“可计算元属性 + 陪伴类型”一次上线执行。

---

## 1. 范围与目标

本期一次上线 PRD 全部三类属性，按天级更新：

| 字段组 | 字段 | 来源 | 计算方式 |
|---|---|---|---|
| 基础事实 | `registered_at` | `accounts.created_at` | 直接复用 |
| 活跃强度 | `message_intensity_level` | `messages` 表 | `floor(ln(1 + inbound_count_until_yesterday))` |
| 安全风险 | `safety_risk_trigger_count_30d` | `content_moderation_tasks` | 近 30 天入站非 pass 风险触发次数（去重） |
| 陪伴类型 | `companion_*` 4 个字段 | LLM 分类推断 | 7 天重算一次，置信度 + 来源 + 摘要 |

派生结果写入两张表：
- `account_user_meta`：每账号一行的当前快照，每日覆盖更新
- `account_user_meta_daily`：每账号每日一行的历史快照，用于回溯分析

---

## 2. Schema 设计

### 2.1 `account_user_meta`（当前快照）

```sql
CREATE TABLE IF NOT EXISTS account_user_meta (
    account_id                       TEXT PRIMARY KEY,
    registered_at                    TEXT NOT NULL,

    -- 活跃强度（每日重算）
    message_intensity_level          INTEGER NOT NULL DEFAULT 0,

    -- 陪伴类型（LLM 推断，7 天重算）
    companion_primary_type           TEXT,                -- enum，见第 3.3 节
    companion_secondary_types        TEXT NOT NULL DEFAULT '[]',  -- JSON 数组
    companion_type_confidence        REAL,                -- 0.0-1.0
    companion_type_last_evaluated_at TEXT,                -- 最近一次 LLM 推断时间
    companion_type_source            TEXT NOT NULL DEFAULT 'auto', -- 'auto' | 'manual'
    companion_type_expires_at        TEXT,                -- NULL=使用全局 7 天规则；manual 可设定到期日
    companion_type_reasoning         TEXT,                -- LLM 推断摘要，供 admin 查看，不展示给用户

    -- 安全风险（每日重算）
    safety_risk_trigger_count_30d    INTEGER NOT NULL DEFAULT 0,

    last_evaluated_at                TEXT NOT NULL,       -- 本次批处理完成时间（北京时间）
    created_at                       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at                       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);
```

### 2.2 `account_user_meta_daily`（历史快照）

与当前快照字段一致，增加 `snapshot_date` 作为日期维度。Phase 2 的 admin 摘要和分析从此表回溯。

```sql
CREATE TABLE IF NOT EXISTS account_user_meta_daily (
    id                               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id                       TEXT NOT NULL,
    snapshot_date                    TEXT NOT NULL,       -- 'YYYY-MM-DD' 北京时间

    registered_at                    TEXT NOT NULL,
    message_intensity_level          INTEGER NOT NULL DEFAULT 0,
    companion_primary_type           TEXT,
    companion_secondary_types        TEXT NOT NULL DEFAULT '[]',
    companion_type_confidence        REAL,
    companion_type_last_evaluated_at TEXT,
    companion_type_source            TEXT NOT NULL DEFAULT 'auto',
    companion_type_expires_at        TEXT,
    companion_type_reasoning         TEXT,
    safety_risk_trigger_count_30d    INTEGER NOT NULL DEFAULT 0,
    last_evaluated_at                TEXT NOT NULL,
    created_at                       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),

    UNIQUE(account_id, snapshot_date),
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS ix_account_user_meta_daily_account_date
    ON account_user_meta_daily(account_id, snapshot_date DESC);
```

### 2.3 迁移注册

在 `app/db/_core.py` 追加第 3 版迁移：

```python
def _migration_0003_user_meta(conn: sqlite3.Connection) -> None:
    """账号用户元属性表：当前快照 + 历史快照（含陪伴类型预留字段）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS account_user_meta ( ... );
        CREATE TABLE IF NOT EXISTS account_user_meta_daily ( ... );
        CREATE INDEX IF NOT EXISTS ix_account_user_meta_daily_account_date ...;
    """)

_MIGRATIONS = [
    (1, _migration_0001_baseline),
    (2, _migration_0002_llm_runtime_config),
    (3, _migration_0003_user_meta),   # 新增
]
```

---

## 3. ETL 口径

### 3.1 `message_intensity_level`

**口径**：截止北京时间当日 00:00:00 之前的累计用户入站消息数，取 `floor(ln(1 + x))`。

```sql
SELECT COUNT(*) AS cnt
FROM messages
WHERE account_id = :account_id
  AND direction  = 'inbound'
  AND created_at < :today_start_beijing   -- 'YYYY-MM-DD 00:00:00'
```

Python 计算：`math.floor(math.log1p(cnt))`（`log1p` 即 `ln(1+x)`，0 消息返回 0）。

`today_start_beijing` 在批处理任务启动时固定，整批一致，不随账号变化。

### 3.2 `safety_risk_trigger_count_30d`

**口径**：近 30 天滚动窗口内，该账号入站消息触发安全风险的去重次数。

```sql
SELECT COUNT(DISTINCT source_id) AS cnt
FROM content_moderation_tasks
WHERE account_id  = :account_id
  AND direction   = 'inbound'
  AND source_type = 'message'
  AND risk_level NOT IN ('pass', 'safe')
  AND created_at >= :thirty_days_ago_beijing
```

**逻辑说明**：
- `direction = 'inbound'`：只统计入站，不含出站阻断和主动消息阻断。
- `source_type = 'message'`：只统计消息来源，避免未来其他 source 类型复用同一表时串入口径。
- `risk_level NOT IN ('pass', 'safe')`：只有通过态算安全。现有代码主口径为 `pass`，历史/兼容口径可能出现 `safe`；其余 `review`、`block`、`escalate`、`unknown` 等均先计入触发，偏召回，后续实际上线后再按风控反馈优化。
- `DISTINCT source_id`：同一条入站消息（同一 `source_id`）命中多个风险类别时，仍只计 1 次。入站消息的 `source_type` 固定为 `'message'`，`source_id` 即 `message_db_id`，无需再拼 `source_type` 前缀。
- `thirty_days_ago_beijing` 整批固定，精确到分钟。

### 3.3 陪伴类型推断（LLM）

#### 触发条件

每个账号满足以下任一条件时，在当次批处理中调用 LLM 重新推断：

1. `companion_type_last_evaluated_at` 为 NULL（从未推断过）
2. `companion_type_source = 'auto'` 且距上次推断超过 7 天
3. `companion_type_source = 'manual'` 且 `companion_type_expires_at` 已过期

不满足以上条件时，直接沿用 `account_user_meta` 现有值写入历史快照（不重新推断）。

#### 跳过条件

满足以下条件时跳过 LLM 推断，保留 `companion_*` 字段为 NULL：

- `message_intensity_level < 2`（信号不足）

本期以 `message_intensity_level >= 2` 作为触发阈值，和调度器实现直接对齐；不再额外做精确 COUNT。该阈值大约对应累计入站消息达到 7 条以上，是偏早识别的低信号门槛。

#### LLM 上下文

从 `messages` 表取该账号最近 50 条 `direction='inbound'` 消息的 `content`，按 `created_at` 升序排列。不读取 AI 回复，不读取原始媒体内容（图片、语音转写结果除外，语音转写已落表）。

上下文窗口设计：
- 最多 50 条，不足 50 条取全量
- 只取 `message_type IN ('text', 'voice')` 的消息内容（忽略纯图片消息）
- 必须排除 `messages.error = MODERATION_BLOCKED_ERROR` 的入站消息，避免被审核拦截的原文进入分类 LLM 上下文
- 消息内容截断：单条超过 200 字符时截至 200 字符 + `…`

#### Prompt 设计

存放于 `app/prompts/user_meta_companion_type.py`（常量字符串）：

```
你是一个用户行为分析器，根据用户最近向 AI 发送的消息，从以下 10 种陪伴类型中判断该用户的使用场景。

## 类型定义
- emotional_support：倾诉、安慰、被理解
- life_reflection：价值观、选择、意义、长期规划
- romance_roleplay：恋爱/暧昧扮演，用户希望更亲密或角色化互动
- practical_assistant：任务、效率、信息整理
- learning_growth：学知识、练技能、求解释
- work_career：工作、创业、产品、管理
- parenting_family：育儿、家庭沟通、亲子关系
- daily_chat：日常闲聊、打发时间
- content_explorer：新闻、话题、兴趣内容
- creative_expression：写作、脑暴、角色设定、文案

## 要求
- 只依据用户侧消息判断，不参考 AI 的回复
- primary_type 必填，选最主要的 1 个
- secondary_types 可多选（0-3 个），选次要的
- confidence 反映你的确信程度（0.0-1.0）；消息少或信号混合时给低值
- reasoning 一句话，供内部审查，不展示给用户

## 用户最近消息（共 {n} 条）
{messages}

输出严格 JSON，不加其他文字：
{"primary_type": "...", "secondary_types": [...], "confidence": 0.0, "reasoning": "..."}
```

#### LLM 调用规格

- 模型：`claude-haiku-4-5-20251001`（独立于 runtime config，batch 专用廉价模型）
- Temperature：0（分类任务）
- max_tokens：128（JSON 输出极短）
- 调用方式：复用项目统一 LLM provider 抽象，不直接引入 Anthropic SDK。构造 batch 专用 `LLMProviderConfig(protocol='anthropic_messages')`，复用 `settings.llm_anthropic_base_url`、`settings.llm_anthropic_api_key`、timeout/retry 配置，model 固定为 `claude-haiku-4-5-20251001`，通过 `asyncio.to_thread(generate_completion, messages, provider=batch_provider)` 调用
- 失败处理：LLM 调用失败或输出解析失败时，保留现有 `companion_*` 字段不变，记录错误到调度器摘要

### 3.4 `registered_at`

直接读取 `accounts.created_at`，无需额外计算。

---

## 4. 新文件：`app/db/user_meta.py`

```python
# 公开接口（签名）

def compute_message_intensity(*, account_id: str, today_start: str) -> int:
    """统计截止 today_start 之前的入站消息数，返回 floor(ln(1+x))。"""

def compute_safety_risk_count_30d(*, account_id: str, thirty_days_ago: str) -> int:
    """统计近 30 天入站 risk_level NOT IN ('pass', 'safe') 的去重风险事件数。"""

def fetch_recent_inbound_messages(
    *, account_id: str, limit: int = 50
) -> list[dict]:
    """取最近 limit 条 inbound 文本/语音消息，排除审核拦截内容，供陪伴类型推断使用。"""

def upsert_account_user_meta(
    *,
    account_id: str,
    registered_at: str,
    message_intensity_level: int,
    companion_primary_type: Optional[str],
    companion_secondary_types: list[str],
    companion_type_confidence: Optional[float],
    companion_type_last_evaluated_at: Optional[str],
    companion_type_source: str,
    companion_type_expires_at: Optional[str],
    companion_type_reasoning: Optional[str],
    safety_risk_trigger_count_30d: int,
    last_evaluated_at: str,
) -> None:
    """INSERT OR REPLACE 当前快照（含陪伴类型字段）。"""

def insert_account_user_meta_daily(
    *,
    account_id: str,
    snapshot_date: str,
    # 与 upsert_account_user_meta 相同字段，去掉 account_id 以外的 PK
    ...
) -> None:
    """INSERT OR IGNORE 历史快照（同一账号同一天幂等）。"""

def get_account_user_meta(*, account_id: str) -> Optional[dict]:
    """读取账号当前元属性快照，不存在返回 None。"""

def list_accounts_for_meta_refresh(*, offset: int = 0, limit: int = 100) -> list[dict]:
    """分页返回非 debug 账号的 (id, created_at)，供批处理迭代。"""

def set_companion_type_manual(
    *,
    account_id: str,
    primary_type: str,
    secondary_types: list[str],
    confidence: float,
    expires_at: Optional[str],
    reasoning: Optional[str],
    now: str,
) -> None:
    """Admin 人工覆盖陪伴类型，写 source='manual'。"""
```

`app/db/__init__.py` 补充 re-export：

```python
from app.db.user_meta import (
    get_account_user_meta,
    upsert_account_user_meta,
    insert_account_user_meta_daily,
    compute_message_intensity,
    compute_safety_risk_count_30d,
    fetch_recent_inbound_messages,
    list_accounts_for_meta_refresh,
    set_companion_type_manual,
)
```

---

## 5. 新文件：`app/prompts/user_meta_companion_type.py`

存放陪伴类型分类 prompt 模板（常量字符串 + 格式化函数）。

```python
COMPANION_TYPE_ENUM = [
    "emotional_support", "life_reflection", "romance_roleplay",
    "practical_assistant", "learning_growth", "work_career",
    "parenting_family", "daily_chat", "content_explorer", "creative_expression",
]

COMPANION_CLASSIFY_PROMPT_TEMPLATE = "..."  # 见第 3.3 节 prompt 设计

def build_companion_classify_prompt(*, messages: list[dict]) -> str:
    """接收消息列表，返回完整 prompt 字符串。截断超长单条消息。"""
```

---

## 6. 调度器：`app/user_meta_scheduler.py`

参照 `DreamingScheduler` 模式，每日一次，默认北京时间 3 点触发（避开 dreaming 的 4 点）。

### 6.1 核心流程

```python
class UserMetaScheduler:
    def __init__(
        self,
        *,
        page_size: int = 100,          # 每页账号数，分页拉取
        inter_account_sleep: float = 0.5,  # 账号间间隔（秒），减少 DB 冲击
        start_hour: int = 3,
    ): ...

    async def run_once(self, *, now: Optional[datetime] = None) -> dict:
        """
        1. 固定时间基准：today_start_beijing, thirty_days_ago
        2. 分页拉取非 debug 账号（list_accounts_for_meta_refresh，offset 递增）
        3. 逐账号处理：
           a. 计算 message_intensity_level、safety_risk_trigger_count_30d
           b. 判断是否需要重新推断陪伴类型
              - 需要且 intensity >= 2：调用 LLM，写入推断结果
              - 需要但 intensity < 2：保留 companion_* 为 NULL
              - 不需要：沿用当前快照的 companion_* 值
           c. upsert_account_user_meta
           d. insert_account_user_meta_daily（snapshot_date = today_beijing）
           e. await asyncio.sleep(inter_account_sleep)
        4. record_scheduler_heartbeat(service="user_meta_scheduler", ...)
        5. 返回 {processed, skipped, companion_evaluated, companion_failed, errors}
        """
```

### 6.2 并发控制

- 调度器本身单线程顺序处理，LLM 调用通过 `asyncio.to_thread` 避免阻塞事件循环
- `_is_running` flag 防止定时触发与手动 run-once 并发
- 每账号 0.5 秒间隔：1000 账号约 8-10 分钟，离线任务可接受

### 6.3 Config（`app/config.py`）

```python
USER_META_SCHEDULER_ENABLED: bool = False
USER_META_SCHEDULER_HOUR: int = 3
USER_META_SCHEDULER_PAGE_SIZE: int = 100
USER_META_SCHEDULER_INTER_ACCOUNT_SLEEP: float = 0.5  # 秒
```

`.env.example` 同步补充说明。

### 6.4 main.py 接线

```python
# startup_event
if settings.USER_META_SCHEDULER_ENABLED:
    app.state.user_meta_scheduler = UserMetaScheduler(...)
    await app.state.user_meta_scheduler.start()

# shutdown_event
if hasattr(app.state, "user_meta_scheduler"):
    await app.state.user_meta_scheduler.stop()
```

---

## 7. Admin API 扩展

### 7.1 查询账号元属性

```
GET /admin/accounts/{account_id}/meta
```

权限：admin + staff token。

**响应示例**：

```json
{
  "account_id": "aid_xxx",
  "registered_at": "2025-08-01 10:23:45",
  "message_intensity_level": 4,
  "safety_risk_trigger_count_30d": 2,
  "companion_primary_type": "emotional_support",
  "companion_secondary_types": ["daily_chat"],
  "companion_type_confidence": 0.82,
  "companion_type_last_evaluated_at": "2026-06-18 03:05:12",
  "companion_type_source": "auto",
  "companion_type_expires_at": null,
  "companion_type_reasoning": "用户频繁表达压力和孤独感，偶有日常闲聊",
  "last_evaluated_at": "2026-06-18 03:05:12"
}
```

若 `account_user_meta` 无该行（批处理尚未运行），返回 `{"meta": null}`（不返回 404，让调用方明确区分"账号不存在"和"元属性未就绪"）。

### 7.2 人工覆盖陪伴类型

```
PATCH /admin/accounts/{account_id}/meta/companion
```

权限：仅 admin token（staff 只读）。

**请求体**：

```json
{
  "primary_type": "practical_assistant",
  "secondary_types": [],
  "confidence": 1.0,
  "expires_at": "2026-07-18 00:00:00",  // 可选，NULL 表示永不自动重算
  "reason": "用户明确反馈"               // 仅内部记录，不入 DB
}
```

调用 `set_companion_type_manual(...)` 写 `companion_type_source='manual'`。下次批处理时若 `expires_at` 未到期，跳过 LLM 重推。

### 7.3 手动触发 run-once

```
POST /admin/ops/user-meta/run-once
```

权限：仅 admin token。复用 admin ops 路由，参照现有 dreaming run-once 接口加 `_is_running` 防并发。

### 7.4 列表接口不改动

`GET /admin/accounts/` 不修改返回结构。

---

## 8. 实现文件清单

| 文件 | 变更类型 | 改动摘要 |
|---|---|---|
| `app/db/_core.py` | 修改 | 新增 `_migration_0003_user_meta`，追加到 `_MIGRATIONS` |
| `app/db/user_meta.py` | 新建 | 全部 user_meta DB 操作 |
| `app/db/__init__.py` | 修改 | re-export user_meta 公开接口 |
| `app/prompts/user_meta_companion_type.py` | 新建 | 分类 prompt 模板 + `COMPANION_TYPE_ENUM` |
| `app/user_meta_scheduler.py` | 新建 | `UserMetaScheduler`（含 LLM 推断步骤） |
| `app/config.py` | 修改 | 4 个新配置变量 |
| `.env.example` | 修改 | 补充 USER_META_* 变量说明 |
| `app/main.py` | 修改 | startup/shutdown 接线 |
| `app/routers/admin_accounts.py` | 修改 | 新增 GET meta + PATCH companion 端点 |
| `app/routers/admin_ops.py` | 修改 | 新增 POST user-meta/run-once 端点 |
| `app/db/lifecycle.py` | 修改 | `wipe_account_data` 删除账号元属性当前/历史快照 |
| `scripts/check_user_meta_companion_type.py` | 新建 | 本地验证陪伴类型 prompt + LLM 解析链路，便于上线前抽测 |
| `tests/test_user_meta.py` | 新建 | 单测（见第 9 节） |

---

## 9. 测试方案

使用内存 SQLite，不启动服务，LLM 调用通过 mock 替换。

### 9.1 DB 层单测

```python
# tests/test_user_meta.py

# message_intensity
def test_message_intensity_zero():           # 0 消息 → 0
def test_message_intensity_today_excluded(): # 只有今天消息 → 0（截止昨日）
def test_message_intensity_formula():        # 插入 7 条 → floor(ln(8)) = 2

# safety_risk
def test_safety_excludes_pass():             # risk_level='pass' 不计入
def test_safety_excludes_safe_compat():      # 兼容历史 risk_level='safe' 不计入
def test_safety_includes_unknown():          # risk_level='unknown' 计入
def test_safety_includes_review_block():     # review/block/escalate 均计入
def test_safety_dedup_same_source():         # 同一 source_id 两行 → 计 1 次
def test_safety_excludes_outbound():         # direction='outbound' 不计入
def test_safety_excludes_non_message_source(): # 非 message source_type 不计入
def test_safety_window_boundary():           # 31 天前事件不计入

# upsert / daily
def test_upsert_idempotent():                # 同账号两次 upsert → 一行，值取最后一次
def test_daily_insert_or_ignore():           # 同账号同 snapshot_date 两次 insert → 一行

# companion manual override
def test_set_companion_manual_sets_source(): # source='manual', expires_at 正确写入

# recent inbound context
def test_fetch_recent_inbound_excludes_moderation_blocked():
    # messages.error=MODERATION_BLOCKED_ERROR 的入站原文不进入陪伴类型 LLM 上下文
```

### 9.2 调度器集成测试

```python
def test_run_once_skips_debug_accounts():
    # debug 账号不写 meta 行
def test_run_once_skips_low_signal_companion():
    # intensity < 2 的账号不触发 LLM，companion_* 为 NULL
def test_run_once_companion_reval_after_7d():
    # last_evaluated_at 8 天前 → 触发 LLM mock
def test_run_once_companion_no_reval_within_7d():
    # last_evaluated_at 3 天前 → 不触发 LLM
def test_run_once_manual_override_not_overwritten():
    # source='manual' 且未过期 → 不触发 LLM，manual 值保持
def test_run_once_idempotent_same_day():
    # 同一天运行两次 → account_user_meta 最新值，daily 表只有一行
def test_run_once_lm_failure_keeps_existing():
    # LLM 抛异常 → 保留现有 companion_* 值，不报错中断整批
```

### 9.3 Admin API 测试

```python
def test_get_meta_returns_all_fields():        # 字段完整，无聊天正文
def test_get_meta_null_when_not_evaluated():   # 未批处理 → {"meta": null}
def test_patch_companion_sets_manual_source(): # PATCH 后 source='manual'
def test_patch_companion_requires_admin():     # staff token → 403
```

### 9.4 开发验证脚本

```bash
.venv/bin/python scripts/check_user_meta_companion_type.py --account 86f866663cf9-im-bot --limit 50
```

脚本职责：
- 读取指定账号最近入站文本/语音消息，复用生产过滤逻辑（含审核拦截过滤）
- 打印 prompt 摘要、调用 batch 专用 Claude provider、解析 JSON
- 校验 `primary_type` / `secondary_types` 均在 `COMPANION_TYPE_ENUM` 内，`confidence` 在 0.0-1.0
- 支持 `--dry-run` 只构造 prompt，不调用 LLM，便于无 key 环境验证格式

---

## 10. 已确认事项

| 问题 | 当前方案 | 备注 |
|---|---|---|
| Admin 无 meta 行返回什么 | `{"meta": null}` | 不返回 404，区分"账号不存在"与"未就绪" |
| 陪伴类型是否本期做 | 是，Phase 1 一次上线 | PRD 已同步调整 |
| 陪伴类型推断最低消息阈值 | `message_intensity_level >= 2`（约 7 条） | 不额外做精确 COUNT |
| 安全风险统计口径 | `risk_level NOT IN ('pass', 'safe')` | 偏召回；unknown 先计入 |
| 陪伴类型 LLM 调用方式 | 复用项目 LLM provider 抽象 | 不直接引入 Anthropic SDK |
| 被审核拦截消息是否进入分类上下文 | 不进入 | 过滤 `messages.error = MODERATION_BLOCKED_ERROR` |
| `companion_type_expires_at` 为 NULL 的 manual override 是否永久有效 | 永不自动重算 | 需人工再次 PATCH 或设置 expires_at 才会恢复 auto |
| `run_once` 是否限制每次最大 LLM 调用数 | 不限制，依赖 7 天缓存自然控制 | 首次全量运行时若账号多，可接受较慢；后续每天只有 7 天到期账号需推断 |
| `companion_type_reasoning` 是否进入历史快照 | 是（写入 `account_user_meta_daily`） | 供回溯分析，存储代价低 |
| 账号 wipe 是否清理元属性 | 是 | `wipe_account_data` 同步删除当前/历史快照 |
