# Intent Gate 重构 + 完整提醒功能设计

更新时间：2026-05-29

## 1. 背景与目标

当前 `turn_service.py` 约 600 行，把意图判断、LLM 调用、状态写入全部混在一个函数里，随着提醒功能扩展（周期提醒、取消/更新）和后续搜索 intent 接入，将难以维护。

本次设计做两件事：

1. **Intent Gate 重构**：把意图分类抽成独立 `app/intent/` 模块，`turn_service.py` 退化为薄编排层。
2. **完整提醒功能**：一次性提醒 + 周期性提醒 + 自然语言取消/更新 + 二次确认状态机，同时修复现有 PRD 频控偏差。

语言约束：本系统是中文对话场景，所有意图识别的触发词、回复模板、确认词表必须以中文为主，不得仅依赖英文 pattern 匹配。

## 2. 范围

### 本次做

- `app/intent/` 模块（classify_intent、IntentResult、各 intent 识别器）
- `turn_service.py` 重构为薄编排层（identity/session/ratelimit 不变，新增 classify + dispatch 两阶段）
- `TurnContext` dataclass 统一传参
- 一次性提醒（现有逻辑迁移）
- 周期性提醒（DB 扩展 + 规则解析）
- 取消/更新提醒（含歧义处理 + 二次确认状态机）
- Pending confirmation 通过消息历史传递
- 频控修复：user_reminder bypass quiet hours，日上限按 product_category 分类

### 本次不做

| 能力 | 说明 |
|---|---|
| Search worker 实现 | search intent 识别到返回"暂不支持"，完整实现见 search_async_tasks_design.md |
| 用户级 timezone | 依赖 onboarding 收集，当前用系统时间 |
| 内容推送（type C） | 独立功能 |
| 多实例 worker lease | 单进程阶段不需要 |
| LLM 辅助周期解析 | 规则层覆盖常见场景，LLM 兜底留接口但不实现 |

## 3. `app/intent/` 模块结构

```
app/intent/
  __init__.py          — 对外暴露 classify_intent()
  types.py             — IntentResult dataclass + IntentKind 常量
  commands.py          — #重置会话 / #状态 特殊命令
  confirmation.py      — 从消息历史检测 pending_confirmation
  reminder.py          — reminder_create / cancel / update / list 识别
  search.py            — 搜索意图识别（stub）
```

### IntentResult

```python
@dataclass
class IntentResult:
    kind: str       # 见 IntentKind 常量
    payload: dict   # intent-specific 数据
    raw_text: str   # 原始用户文本
```

### IntentKind 常量

```python
COMMAND              = "command"
PENDING_CONFIRMATION = "pending_confirmation"
REMINDER_CREATE      = "reminder_create"
REMINDER_CANCEL      = "reminder_cancel"
REMINDER_UPDATE      = "reminder_update"
REMINDER_LIST        = "reminder_list"
SEARCH               = "search"
NORMAL               = "normal"
```

### classify_intent() 签名

```python
def classify_intent(
    text: str,
    *,
    message_type: str,
    recent_messages: list[dict],   # 最近 3 条消息，用于 pending_confirmation 检测
) -> IntentResult:
```

### 识别优先级（从高到低）

```
1. commands           — 精确匹配 #开头特殊命令
2. pending_confirmation — 检测上条 assistant 消息是否有待确认
3. reminder_cancel    — 中文取消动词 + 提醒关键词
4. reminder_update    — 中文修改动词 + 提醒关键词
5. reminder_create    — 时间 + 提醒触发词
6. reminder_list      — 查询提醒列表触发词
7. search             — 搜索触发词（stub）
8. normal             — fallthrough
```

cancel/update 必须在 create 之前，原因：  
"取消明天上午的提醒"同时含时间信息，如果 create 先匹配会误判为新建提醒。

voice 消息直接返回 `NORMAL`，不做 intent 识别。

## 4. 中文 Intent 识别词表

### reminder_cancel 触发词（正则，中文）

```
取消.*提醒 | 删除.*提醒 | 不要.*提醒 | 不用.*提醒了
不提醒了 | 去掉.*提醒 | 移除.*提醒 | 我不需要提醒了
```

### reminder_update 触发词（正则，中文）

```
把.*提醒.*改 | 修改.*提醒 | 更新.*提醒 | 调整.*提醒
把时间改成 | 改到 | 推迟到 | 提前到 | 提醒改一下
```

### reminder_list 触发词（正则，中文）

```
我有哪些提醒 | 查看提醒 | 提醒列表 | 我设了什么提醒 | 有几个提醒
```

### search 触发词（正则，中文）

```
帮我查 | 搜一下 | 查一下 | 最近.*消息 | 最新.*新闻
网上.*有没有 | 查查看 | 给我找 | 帮我搜
```

### 肯定词表（pending_confirmation confirm）

```
好 | 是的 | 对 | 确认 | 嗯 | 可以 | 就这样 | 行 | 没问题 | 同意 | 好的
```

### 否定词表（pending_confirmation reject）

```
不 | 不要 | 不对 | 不是 | 算了 | 取消 | 不用 | 不行 | 不改 | 不用改 | 不对
```

## 5. `turn_service.py` 重构后结构

### TurnContext dataclass

```python
@dataclass
class TurnContext:
    account_id: str
    account: dict
    session: dict
    identity: Any           # OpenClawIdentity
    binding: dict
    message_id: str
    text: str
    today: str
    business_day: str
    profile_path: Any
    debug_trace_enabled: bool
    onboarding_state: str
    onboarding_active: bool
    recent_messages: list[dict]   # 已加载，供 classify_intent 和 handle_normal 复用
    background_loop: Optional[asyncio.AbstractEventLoop]
```

### handle_openclaw_turn 结构

```python
def handle_openclaw_turn(payload, *, background_loop=None):

    # ── 阶段 1：identity / session / onboarding / rate limit / dedup ──
    # 现有逻辑不变（约 150 行），onboarding 特殊路径保持原位

    # ── 阶段 2：intent 分类 ──
    recent_messages = list_recent_messages(session_id=session["id"], limit=5)
    intent = classify_intent(
        text=text,
        message_type=payload.message_type,
        recent_messages=recent_messages,
    )

    # ── 阶段 3：intent dispatch ──
    ctx = TurnContext(...)
    reply, generation_error = dispatch_intent(intent, ctx)

    # ── 阶段 4：写回复消息 / debug trace / 后台任务 ──
    # 现有逻辑不变
```

### dispatch_intent() 结构

```python
def dispatch_intent(intent: IntentResult, ctx: TurnContext):
    match intent.kind:
        case COMMAND:             return handle_command(intent, ctx)
        case PENDING_CONFIRMATION: return handle_confirmation(intent, ctx)
        case REMINDER_CREATE:     return handle_reminder_create(intent, ctx)
        case REMINDER_CANCEL:     return handle_reminder_cancel(intent, ctx)
        case REMINDER_UPDATE:     return handle_reminder_update(intent, ctx)
        case REMINDER_LIST:       return handle_reminder_list(intent, ctx)
        case SEARCH:              return handle_search_stub(intent, ctx)
        case _:                   return handle_normal(intent, ctx)
```

handler 函数落点规则：
- 有独立状态/复杂逻辑的 handler（reminder_create/cancel/update、confirmation）放在 `app/intent/` 对应子模块里
- 简单的 handler（command、search stub、normal）作为 `turn_service.py` 的私有函数即可
- `handle_normal()` 包含现有的 LLM 生成逻辑，保留在 `turn_service.py`

`recent_messages` 在阶段 2 分类前统一加载（`limit=settings.llm_context_messages`），存入 `TurnContext`，供 `classify_intent()` 和 `handle_normal()` 复用，不重复查询。

## 6. Pending Confirmation 状态机

### 存储方式

bot 回复一个"需要确认"的操作时，在写入 `messages` 表的 outbound 消息时，把待确认意图写入 `raw` 字段的 `pending_confirmation` 键：

```json
{
  "pending_confirmation": {
    "kind": "reminder_update",
    "reminder_id": "rem_xxx",
    "proposed": {
      "text": "给爸妈打电话",
      "recur_rule": "weekly:6",
      "due_at": "2026-05-31 10:00:00"
    },
    "expires_after_turns": 2
  }
}
```

### 检测逻辑（confirmation.py）

```python
def detect_pending_confirmation(recent_messages: list[dict]) -> Optional[dict]:
    """
    扫描最近 N 条 assistant 消息，返回最新一条有效的 pending_confirmation。
    超过 expires_after_turns 轮的视为失效。
    """
```

判断逻辑：
```
读最近 3 条 assistant 消息
  → 找最新一条含 pending_confirmation 的
  → 计算从该消息到现在的用户消息数（turns）
  → turns >= expires_after_turns(2) → 返回 None（失效）
  → turns < 2 → 返回 pending_confirmation payload
```

### dispatch 处理

```
confirm（中文肯定词）：
  reminder_update → update reminder 字段 → 回复"已改好"
  reminder_cancel → mark_reminder_cancelled → 回复"已取消"

reject（中文否定词）：
  → 回复"好的，保持原来的设置" / "好的，提醒保留"

都不匹配（用户在说别的事）：
  → 当 normal 处理，pending_confirmation 自然过期
```

## 7. 周期性提醒数据模型

### DB 变更

```sql
ALTER TABLE reminders ADD COLUMN recur_rule TEXT;
-- null = 一次性；有值 = 周期性

ALTER TABLE reminders ADD COLUMN sent_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE reminders ADD COLUMN last_sent_at TEXT;
```

### recur_rule 格式

| 周期 | 格式 | 示例 |
|------|------|------|
| 每天 | `daily` | `daily` |
| 每周 N | `weekly:N` | `weekly:6`（0=周一，6=周日） |
| 每月 D 日 | `monthly:D` | `monthly:15` |

时间信息存在 `due_at`，每次触发后重算下次 `due_at` 并重置 `status=pending`，原行复用。

### 触发后状态流转

```
pending → claimed → 发送
  ├── recur_rule = null（一次性）：
  │     sent_count += 1, last_sent_at = now, status = sent
  └── recur_rule 不为 null（周期性）：
        sent_count += 1, last_sent_at = now
        计算 next_due_at → 更新 due_at
        status 重置为 pending
```

### next_due_at 计算函数

```python
def compute_next_due_at(recur_rule: str, last_due_at: datetime) -> datetime:
    """
    根据 recur_rule 和上次触发时间，计算下一次触发时间。
    daily   → last_due_at + 1 天
    weekly:N → last_due_at 推到下一个星期 N 的同一时间
    monthly:D → last_due_at 推到下一个月的 D 日同一时间
    """
```

### 周期性提醒的解析（reminder.py 扩展）

规则层覆盖常见中文周期表达：

```
每天 / 每天早上 / 每天晚上     → recur_rule = "daily"
每周[一二三四五六日天] / 每周 X → recur_rule = "weekly:N"
每月 X 号 / 每个月 X 号         → recur_rule = "monthly:D"
```

模糊兜底（规则层无法确定周期）回复中文提示：
> "可以帮你设置定期提醒，请告诉我具体的周期和时间，比如：每周六上午9点提醒我…"

## 8. 取消/更新 Handler 逻辑

### 目标提醒定位

```
查询 active reminders（status=pending）：
  = 0 → 回复"你目前没有待发送的提醒"
  = 1 → 直接进入确认流程
  ≥ 2 → 尝试从消息文本匹配（时间/内容关键词）
            匹配唯一 → 进入确认流程
            仍有歧义 → 列出所有提醒，让用户指定
```

列出格式（中文纯文本）：
```
你目前有 2 个提醒，是哪一个？
1. 明天上午 10:00 — 检查事情A
2. 每周六 09:00 — 给爸妈打电话
```

用户回复"第1个"/"1"/"第一个"/"上面那个"后，再次进入确认流程。

### 取消确认回复模板（中文）

```
要取消「{recur_desc}{time_desc} — {text}」这个提醒吗？
```

### 更新确认回复模板（中文）

```
把提醒从{原描述}改成{新描述}，{内容变/内容不变}，是这样吗？
```

## 9. 频控修复

`send_proactive_text()` / `enqueue_proactive_text()` 增加 `product_category` 参数：

| product_category | quiet hours | 日上限 |
|---|---|---|
| `user_reminder` | 跳过 | 无上限 |
| `companion_followup` | 受影响 | 默认 1 条 |
| `content_push` | 受影响 | 默认 1 条 |
| `task_result` | 跳过 | 不占用上限 |

`dispatch_reminder()` 固定传 `product_category="user_reminder"`，不再需要 `bypass_quiet_hours` 参数。

## 10. 测试节点

- `明天上午10点提醒我检查事情A` → 一次性提醒创建确认
- `每周六上午9点提醒我给爸妈打电话` → 周期提醒创建确认
- `取消那个提醒` → 只有1条时直接出确认；多条时列出
- `把每周六改成周日上午10点` → 更新确认 → 用户说"好" → 执行
- `每周提醒我` → 模糊，要求补充具体时间
- `帮我查最近的AI新闻` → 识别为 search → 回复"暂不支持"
- `#重置会话` → command handler
- quiet hours 内到期的 user_reminder → 应按时发送，不受 quiet hours 影响
