# Intent Gate 重构 + 完整提醒功能设计

更新时间：2026-05-30（v2 — 改为 LLM tool use 方案）

## 1. 背景与目标

当前 `turn_service.py` 约 600 行，把意图判断、LLM 调用、状态写入全部混在一个函数里，随着提醒功能扩展和后续搜索 intent 接入，将难以维护。

本次设计做两件事：

1. **Intent Gate 重构**：放弃预-LLM 正则意图识别，改用 LLM tool use。`turn_service.py` 退化为薄编排层，意图识别由 LLM 通过工具调用自然承担。
2. **完整提醒功能**：一次性提醒 + 周期性提醒 + 自然语言取消/更新（含二次确认），同时修复现有 PRD 频控偏差。

### 为什么不用正则，也不用独立意图分类 LLM 调用

| 方案 | 问题 |
|---|---|
| 纯正则 | 中文提醒说法变体极多，正则维护成本高、miss rate 高 |
| 独立 LLM 意图分类 | 每条消息多一次完整 LLM 调用，成本和延迟翻倍 |
| **LLM tool use（本方案）** | 普通聊天零额外成本；提醒相关 turn 多一次工具执行 round-trip（约 0.5s），代价集中在需要时才支付 |

OpenClaw 的做法与此一致：explicit 提醒/调度请求由 LLM 工具直接处理，不做预-LLM 正则识别。

### 语言约束

本系统是中文对话场景，工具的 description、系统提示中的工具使用说明、以及所有回复模板必须以中文为主。

## 2. 范围

### 本次做

- `app/tools/` 模块（工具定义 + 工具执行器）
- `turn_service.py` 重构：仅保留 `#命令` 前置过滤，其余走 tool-enabled LLM 调用
- `TurnContext` dataclass 统一传参
- `llm.py` 支持 tool use 的多步调用（最多一轮工具执行）
- 一次性提醒（现有逻辑迁移）
- 周期性提醒（DB 扩展 + LLM 解析）
- 取消/更新提醒（含歧义处理，二次确认由 LLM 对话自然承担）
- 频控修复：user_reminder bypass quiet hours，日上限按 product_category 分类

### 本次不做

| 能力 | 说明 |
|---|---|
| Search worker 实现 | 无搜索工具定义，LLM 自然回复"暂不支持"；完整实现见 search_async_tasks_design.md |
| 用户级 timezone | 依赖 onboarding 收集，当前用系统时间 |
| 内容推送（type C） | 独立功能 |
| 多实例 worker lease | 单进程阶段不需要 |
| 多轮工具调用 | Phase 1 每个 turn 最多执行一次工具 |

## 3. `app/tools/` 模块结构

```
app/tools/
  __init__.py          — get_reminder_tools() → tool schema 列表
  definitions.py       — 工具 schema 定义（JSON Schema 格式）
  executor.py          — execute_tool_call(name, args, ctx) 分发器
  reminder_handlers.py — 每个工具的实际执行逻辑
```

### 工具列表

| 工具名 | 作用 |
|---|---|
| `create_reminder` | 创建一次性或周期性提醒 |
| `list_reminders` | 列出用户当前 active 提醒 |
| `cancel_reminder` | 取消指定提醒 |
| `update_reminder` | 更新提醒的时间、内容或周期 |

### 工具 schema 示例（`create_reminder`）

```python
{
    "name": "create_reminder",
    "description": (
        "创建一个提醒。用于用户明确要求在未来某个时间收到提醒的场景。"
        "due_at 格式为 YYYY-MM-DD HH:MM:SS。"
        "recur_rule 可选，格式：daily（每天）、weekly:N（每周，N 为 0=周一 … 6=周日）、"
        "monthly:D（每月第 D 日）。不填表示一次性提醒。"
        "时间不明确时不要猜测，应告知用户需要补充具体日期和时间。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text":      {"type": "string",  "description": "提醒内容"},
            "due_at":    {"type": "string",  "description": "触发时间，格式 YYYY-MM-DD HH:MM:SS"},
            "recur_rule":{"type": "string",  "description": "周期规则，可选"},
        },
        "required": ["text", "due_at"],
    },
}
```

其他工具同理，description 均使用中文说明。

### `execute_tool_call()` 分发器

```python
def execute_tool_call(
    name: str,
    args: dict,
    ctx: TurnContext,
) -> dict:
    """
    执行工具调用，返回结构化结果供 LLM 生成最终回复。
    失败时返回 {"error": "..."} 而不是抛出，让 LLM 自行解释失败。
    """
    match name:
        case "create_reminder":  return handle_create_reminder(args, ctx)
        case "list_reminders":   return handle_list_reminders(args, ctx)
        case "cancel_reminder":  return handle_cancel_reminder(args, ctx)
        case "update_reminder":  return handle_update_reminder(args, ctx)
        case _:                  return {"error": f"unknown tool: {name}"}
```

## 4. `llm.py` 工具调用支持

新增 `generate_reply_with_tools()`，支持最多一轮工具执行：

```python
def generate_reply_with_tools(
    *,
    user_text: str,
    history: list[dict],
    system_prompt: str,
    tools: list[dict],
    ctx: TurnContext,
) -> tuple[str, Optional[str]]:
    """
    返回 (reply_text, error_str)。
    error_str 非 None 表示工具执行或 LLM 调用失败。
    """
    # 1. 第一次 LLM 调用（带工具定义）
    response = client.messages.create(
        model=settings.llm_model,
        system=system_prompt,
        messages=build_messages(history, user_text),
        tools=tools,
        max_tokens=...,
    )

    # 2. 如果 LLM 直接返回文本 → done
    if response.stop_reason == "end_turn":
        return extract_text(response), None

    # 3. 如果 LLM 调用工具
    if response.stop_reason == "tool_use":
        tool_call = extract_tool_use_block(response)
        tool_result = execute_tool_call(tool_call.name, tool_call.input, ctx)

        # 4. 第二次 LLM 调用（带工具结果，生成最终回复文本）
        response2 = client.messages.create(
            model=settings.llm_model,
            system=system_prompt,
            messages=build_messages_with_tool_result(
                history, user_text, response, tool_call, tool_result
            ),
            max_tokens=...,
        )
        return extract_text(response2), None

    return "", "unexpected_stop_reason"
```

`generate_reply()`（无工具版本）保留，供 onboarding 等不需要工具的路径使用。

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
    recent_messages: list[dict]   # 已加载，供 LLM history 和工具执行复用
    background_loop: Optional[asyncio.AbstractEventLoop]
```

### handle_openclaw_turn 结构

```python
def handle_openclaw_turn(payload, *, background_loop=None):

    # ── 阶段 1：identity / session / onboarding / rate limit / dedup ──
    # 现有逻辑不变（约 150 行），onboarding 特殊路径保持原位

    # ── 阶段 2：#命令 前置过滤 ──
    if text in _SPECIAL_COMMANDS:
        reply, generation_error = handle_special_command(text, ctx)

    # ── 阶段 3：LLM + 工具调用 ──
    else:
        recent_messages = list_recent_messages(
            session_id=session["id"],
            limit=settings.llm_context_messages,
        )
        ctx = TurnContext(...)
        system_prompt = build_system_prompt(ctx)

        if onboarding_active:
            # onboarding 路径不带工具（避免用户在入门时误触提醒工具）
            reply, generation_error = generate_reply(
                user_text=text, history=recent_messages, system_prompt=system_prompt
            )
        else:
            tools = get_reminder_tools()
            reply, generation_error = generate_reply_with_tools(
                user_text=text,
                history=recent_messages,
                system_prompt=system_prompt,
                tools=tools,
                ctx=ctx,
            )

    # ── 阶段 4：写回复消息 / debug trace / 后台任务 ──
    # 现有逻辑不变
```

### 消息历史中的工具调用记录

工具调用是 turn 内部实现细节，不写入 `messages` 表。写入的只有最终 assistant 文本回复，和现在一致。这样历史窗口格式不变，无需改造 `list_recent_messages()`。

## 6. 取消/更新的二次确认：由 LLM 对话自然承担

**不再需要 `pending_confirmation` 状态机。**

LLM 在同一 turn 内执行工具前，如果有歧义，会先调用 `list_reminders()` 获取列表，然后在文本回复中询问用户。下一 turn 用户的回答 + 上一 turn 的对话历史，足够让 LLM 直接调用 `cancel_reminder` 或 `update_reminder`。

示例流：
```
用户：取消那个提醒
  LLM call 1 → tool: list_reminders()
  tool result → [{id: "rem_1", ...}, {id: "rem_2", ...}]
  LLM call 2 → 文本："你有 2 个提醒，是哪个？\n1. 明天上午10点 — 检查事情A\n2. 每周六 — 给爸妈打电话"

用户：第一个
  LLM call 1 → 看到历史中的 rem_1 信息 → tool: cancel_reminder(id="rem_1")
  tool result → {status: "cancelled"}
  LLM call 2 → 文本："已取消「明天上午10点 — 检查事情A」这个提醒。"
```

当只有一条 active 提醒时，LLM 直接调用 `cancel_reminder`，同 turn 完成，无需确认。

更新流类似：LLM 先 `list_reminders` 定位，再 `update_reminder`，或在有歧义时先问再操作。

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
    daily   → last_due_at + 1 天
    weekly:N → last_due_at 推到下一个星期 N 的同一时间
    monthly:D → last_due_at 推到下一个月的 D 日同一时间
    """
```

### `reminder_parser.py` 的去留

原来的意图识别逻辑（正则匹配提醒触发词）不再需要。保留的部分：

- `compute_next_due_at()`：周期提醒触发后计算下次时间
- `validate_due_at(text) -> datetime`：对 LLM 传入的时间字符串做服务端校验（防止 LLM 幻觉出无效日期）
- `validate_recur_rule(text) -> str`：对 LLM 传入的 recur_rule 做格式校验

文件可改名为 `app/reminder_utils.py` 反映新定位。

## 8. 取消/更新 Handler 逻辑

### `handle_cancel_reminder(args, ctx)`

```python
def handle_cancel_reminder(args: dict, ctx: TurnContext) -> dict:
    reminder_id = args.get("reminder_id")
    reminder = get_reminder(reminder_id=reminder_id)
    if not reminder or reminder["account_id"] != ctx.account_id:
        return {"error": "提醒不存在或无权操作"}
    if reminder["status"] != "pending":
        return {"error": f"该提醒已是 {reminder['status']} 状态，无法取消"}
    cancel_reminder(reminder_id=reminder_id)
    return {"status": "cancelled", "reminder": reminder}
```

### `handle_update_reminder(args, ctx)`

```python
def handle_update_reminder(args: dict, ctx: TurnContext) -> dict:
    reminder_id = args.get("reminder_id")
    # 校验归属
    # 更新 due_at / recur_rule / text（只更新 args 中有的字段）
    # 如果 recur_rule 改变，重新计算 next_due_at
    # 返回更新后的 reminder
```

### `handle_list_reminders(args, ctx)`

返回该账号所有 `status=pending` 的提醒，格式：

```json
{
  "reminders": [
    {"id": "rem_xxx", "text": "检查事情A", "due_at": "2026-05-31 10:00:00", "recur_rule": null},
    {"id": "rem_yyy", "text": "给爸妈打电话", "due_at": "2026-05-31 09:00:00", "recur_rule": "weekly:6"}
  ]
}
```

`id` 字段是后续 `cancel_reminder` / `update_reminder` 的必填参数，description 里需明确说明。

## 9. 频控修复

`send_proactive_text()` / `enqueue_proactive_text()` 增加 `product_category` 参数：

| product_category | quiet hours | 日上限 |
|---|---|---|
| `user_reminder` | 跳过 | 无上限 |
| `companion_followup` | 受影响 | 默认 1 条 |
| `content_push` | 受影响 | 默认 1 条 |
| `task_result` | 跳过 | 不占用上限 |

`dispatch_reminder()` 固定传 `product_category="user_reminder"`，不再需要 `bypass_quiet_hours` 参数。

## 10. 系统提示中的工具使用指引

在 `prompt_builder.py` 的系统提示中增加工具使用说明段落（中文）：

```
## 提醒工具使用规则

- 用户明确要求在某个时间收到提醒时，调用 create_reminder。
- 时间不明确时，不要猜测或假设，告知用户需要补充具体日期和时间。
- 需要取消或修改提醒时，先调用 list_reminders 确认提醒存在，再调用 cancel_reminder 或 update_reminder。
- 有多个 active 提醒且用户描述不够精确时，列出提醒让用户选择，不要盲目操作。
- 不要承诺任何工具之外的动作（如搜索、发图片等）。
```

## 11. 测试节点

- `明天上午10点提醒我检查事情A` → LLM 调用 create_reminder → 确认文本
- `每周六上午9点提醒我给爸妈打电话` → LLM 调用 create_reminder（recur_rule=weekly:6）→ 确认
- `下周提醒我` → LLM 不调用工具，回复"请告诉我具体日期和时间"
- `取消那个提醒`（1条 active）→ LLM list_reminders → cancel_reminder → 确认
- `取消那个提醒`（2条 active）→ LLM list_reminders → 列出让用户选 → 用户回"第1个" → cancel_reminder
- `把每周六改成周日上午10点` → LLM list_reminders → update_reminder → 确认
- `帮我查最近的AI新闻` → LLM 无搜索工具，自然回复"搜索功能暂时还没有"
- `#重置会话` → 前置过滤，不进 LLM
- quiet hours 内到期的 user_reminder → 按时发送，不受 quiet hours 影响
