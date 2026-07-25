# 短期上下文：Token 预算窗口 + 单消息硬上限 + 滚动摘要 设计文档

状态：已实现（单 PR，P3 默认关）  
作者：开发  
日期：2026-06-27  
关联：对标 OpenClaw agent-core 的 `packages/agent-core/.../compaction/`（token 预算触发 + 滚动摘要 + 保留最近 N 轮原文）

---

## 1. 背景与目标

当前喂给 LLM 的短期历史是**纯按条数的滑动窗口**：每轮取该账号最近 `llm_context_messages=100` 条消息，无 token 预算、无单条大小限制、无 intra-session 摘要兜底。对标 OpenClaw 的 agent 运行时，我们缺三块能力，本文档一次性设计、分期落地：

1. **Token 预算窗口**：裁剪依据从「条数」升级为「token 预算 + 条数双闸，先到先裁」。解决「100 条短句 vs 100 条长段落，送进去的 token 差几十倍」无任何防护的问题。
2. **单消息硬上限**：单条历史消息超过阈值即截断，防单条异常长消息（长文粘贴、长工具回放）顶爆上下文。
3. **Token 压力滚动摘要**：长 session 内，旧消息滑出窗口前先压成滚动摘要注入 prompt，而非静默丢失。把现有「只在 session 轮转（4 点 / 500 轮）才生成 carryover 摘要」升级为「token 压力也能触发的 intra-session 摘要」。

非目标：不改账号隔离模型；不改 OpenClaw 侧 gateway 的 `chat.history`（那是前端展示投影，与 LLM 上下文无关）；不替换计费口径（真值仍走 LLM usage）。

---

## 2. 现状代码事实（带证据）

| 事实 | 位置 |
|---|---|
| 历史查询：仅按 `account_id`，`ORDER BY id DESC LIMIT 100`，纯条数 | `app/db/accounts.py:355-390` |
| 默认条数 `llm_context_messages = 100` | `app/config.py:54` |
| 历史组装：`history = [{role, content}]`，**丢弃时间戳，无大小/预算裁剪** | `app/turn_service.py:446-453` |
| carryover 抑制：历史已覆盖上一个 session 时抑制 carryover，避免重复 | `app/turn_service.py:472-476`、`_history_covers_previous_session` `:273-293` |
| carryover 只在 session 轮转时由 dreaming 生成（4 点跨业务日 / 500 轮） | `app/session_lifecycle.py:34-160`、`app/config.py:86-87` |
| prompt_builder **已有** token 预算原语 `_estimate_tokens`(len/1.5) + `_apply_token_budget`，但**仅作用于 system prompt 的 block，且默认关**（`token_budget=None`） | `app/prompt_builder.py:62-68, 119-142` |
| prompt_builder 已有 carryover 注入 block（Block 11，截断 2000 字符，`...[已截断]` 标记） | `app/prompt_builder.py:400-403` |
| 索引缺口：`messages` 只有 `ux_messages_account_message(account_id, message_id)` 和 `ix_messages_session_created(session_id, id)`，**无 `(account_id, id)`**；热点查询 `WHERE account_id ORDER BY id DESC` 用不上有序索引 | `app/db/_core.py:622-627` |
| 会话表 `sessions` 已有 `session_summary` / `carryover_summary` 列 | `app/db/_core.py:569-582` |
| 历史后处理：`inject_tool_evidence_replay` 在历史里回灌最近 K 轮工具证据（已被 `llm_tool_evidence_max_result_chars=1500` / `llm_tool_evidence_turns=2` 约束） | `app/turn_service.py:454-463` |

复用资产：`_estimate_tokens`（token 近似）、`_truncate`（截断+标记）、dreaming 摘要原语（`rough_summary`/`carryover_summary` + 确定性兜底）。本设计尽量复用，不重造。

---

## 3. 设计总览与数据流

在 `build_turn_llm_input`（`app/turn_service.py:446` 起）插入一个**历史裁剪阶段**，新增纯函数模块 `app/context_window.py` 承载逻辑（可单测、无 I/O）。改造后的数据流：

```
list_recent_messages_for_account(LIMIT = 条数硬顶)        # DB 取数（保留 100 作安全顶）
        │
        ▼
trim_history_rows(rows, token_budget, per_message_max_chars)   # ← 新增（Part 1 + 2）
   1) 逐条：单条超 per_message_max_chars → 截断 + 标记         # Part 2
   2) 从最旧端丢弃，直到累计 est_token ≤ token_budget          # Part 1（保留尾部最近）
   → kept_rows + dropped_rows + metrics
        │
        ▼
history = [{role, content} for kept_rows]
inject_tool_evidence_replay(history, ...)                  # 原有，不变
        │
        ▼
suppress_carryover = _history_covers_previous_session(kept_rows)   # ← 改：基于"裁剪后"的行，保证一致性
rolling_summary = session.rolling_summary if 本会话头部被丢弃 else None   # Part 3（灰度）
        │
        ▼
PromptBuilder.assemble(carryover_summary=..., rolling_summary=...)      # Part 3 新增 block
```

**关键正确性点**：`_history_covers_previous_session` 必须改为基于**裁剪后**的 `kept_rows` 计算。否则会出现「token 预算把上一个 session 的旧消息丢了，但抑制逻辑仍以为历史覆盖了它 → 既丢原文又抑制摘要 → 上下文丢失」。这是本设计第一优先级的回归点。

---

## 4. Part 1：Token 预算窗口

### 行为
- DB 取数仍 `LIMIT llm_context_messages`（100，作条数安全顶，避免无界扫描）。
- 取回后按 token 预算 `llm_context_token_budget` 从**最旧端**丢弃，保留尾部最近消息（与 OpenClaw `capArrayByJsonBytes` 的尾部保留一致）。
- token 估算复用 `_estimate_tokens`（`len/1.5` 的 ceil 近似），不引入 tokenizer 依赖。
- `llm_context_token_budget = 0` → 关闭，退回纯条数（**零行为变更**，便于灰度）。

### 预算范围（已定）
**该预算只作用于对话历史（history 消息数组），不含 system prompt。** system prompt 由
prompt_builder 自带的、独立的 `_apply_token_budget`（`app/prompt_builder.py:119`）+ 各 block
字符截断管理（默认关）。理由：system prompt 是稳定、缓存友好的前缀，history 是易变尾部，混在
一个预算里裁剪会让「裁人设还是裁历史」不可控。如未来需要「整体 prompt 上限」，另起设计，不在本文档范围。

### 取舍
- 预算只覆盖 base history；`tool_evidence_replay` 注入的工具证据本身已被字符/轮数上限约束，v1 不纳入同一预算（标注为已知近似，metadata 里单独记 tool_evidence 估算 token 供观测）。
- 预算建议初值：评审时定。倾向 `6000~8000`（中文 ≈ 1 token/字，留足 system+输出余量）。先 `0` 上线、灰度账号验证后再设非零。

---

## 5. Part 2：单消息硬上限

### 行为
- 单条 `content` 字符数超过 `llm_context_message_max_chars` → 截断到该长度并追加 `...[已截断]`（复用 prompt_builder 的标记风格）。
- 在 token 预算丢弃**之前**执行（先把超大单条压小，再算总预算）。
- `llm_context_message_max_chars = 0` → 关闭。建议初值 **`2000`** 字符。

### 取舍
- 用字符数而非字节，与现有 `_truncate`/`char_limit` 全代码风格一致。
- 截断只作用于「喂给 LLM 的副本」，**不改落库内容**（与 `_wrap_current_message_envelope` 同原则，`app/turn_service.py:400-414`）。

---

## 6. Part 3：Token 压力滚动摘要（灰度，默认关）

最重、风险最高的一块。核心约束：**不在用户同步链路新增 LLM 调用**（保延迟），摘要生成放后台，同步轮只消费已有摘要——与现有 carryover/memory 后台更新架构一致。

### 数据模型
`sessions` 表新增两列（新迁移函数，追加到 `_MIGRATIONS`）：
- `rolling_summary TEXT`：本会话「已滑出窗口的头部消息」的滚动摘要。
- `rolling_summary_upto_id INTEGER`：水位线，标记摘要已覆盖到哪条 message.id（避免重复摘要、avoid 与 kept window 重叠）。

### 触发与生成（后台）
- **溢出分界与同步链路同口径（关键一致性）**：后台摘要不用「条数」估窗口，而是用与
  `build_turn_llm_input` **完全相同**的 `trim_history_rows`（account-scoped 取数 + token 预算 +
  单条上限）重算 live window，取 kept 中最小 id 作分界；本会话中 `id < 分界` 即真·溢出。否则会
  出现「token 预算把消息丢出 prompt，但条数未超 → 摘要判 below_window 跳过 → 既丢原文又不摘要」。
- **候选按水位线分页**：候选查询以 `after_id=rolling_summary_upto_id` 起（非固定 `after_id=0`），
  避免超长 session（>单次取数上限 1000）时水位线卡在头部、后续真实溢出永不被摘要。
- 同步轮在裁剪后记录：本会话是否有「早于 kept window 最旧 id」的消息（即发生了 intra-session 溢出）。
- **后台挂点（已定）：复用现有 turn 后的 memory 更新链路**（每轮回复后已在跑 `memory_writer` 的那段后台逻辑），在其中追加一步「需要时生成滚动摘要」。同步轮零新增 LLM 调用，保延迟；不新起独立任务/进程。摘要生成逻辑独立成可单测函数（可放 `app/context_summarizer.py` 仅作纯逻辑承载，由 memory 后台链路调用）。
- 触发条件：当 `本会话未被摘要覆盖的溢出消息数 ≥ llm_rolling_summary_trigger_messages`（或其 token 估算超阈值）时，对 `(rolling_summary_upto_id, kept_window 最旧 id)` 之间的消息调用 dreaming 摘要原语，与旧 `rolling_summary` 合并蒸馏（参考 OpenClaw `previousSummary` 复用），写回 `rolling_summary` + 推进水位线。
- 失败/未达阈值：保持旧摘要，绝不阻塞用户回复。

### 同步消费
- `build_turn_llm_input` 读取 `session.rolling_summary`，仅当 kept window 未回溯到 session 起点时注入。
- prompt_builder 新增 Block（紧邻 carryover，截断阈值复用 2000、`trim_priority` 介于 carryover(20) 与 long_term_memory(30) 之间）。carryover（跨 session）与 rolling（session 内）语义不同，**并存不互斥**。

### 取舍
- 默认 `llm_rolling_summary_enabled=False`。聊天陪伴单条消息短、无巨型工具输出，溢出频率远低于编程 agent；Part 3 仅在确有长 session 丢历史痛点时开。先上 Part 1+2，Part 3 灰度。

---

## 7. 新增配置项（`app/config.py` + `.env.example` 行内注释）

```python
# 历史 token 预算：>0 时在条数窗口基础上再按 token 从最旧端裁剪；0=关闭（退回纯条数）。
llm_context_token_budget: int = 8000
# 单条历史消息字符硬上限：>0 时超长单条截断加 ...[已截断]；0=关闭。
llm_context_message_max_chars: int = 2000
# Part 3 滚动摘要总开关（涉及后台 LLM 成本，默认关，确认后开）。
llm_rolling_summary_enabled: bool = False
# 本会话未摘要的溢出消息达到该条数即触发后台滚动摘要。
llm_rolling_summary_trigger_messages: int = 20
```

> 默认值（已定）：**P1/P2 直接非零默认上线**——`token_budget=8000`、`message_max_chars=2000`。
> 8000 token 对正常陪伴对话（百条短消息约 3000~4000 token）基本不触发裁剪，主要作长消息/异常膨胀的安全上限；2000 字符单条上限同理。P3（`rolling_summary_enabled`）因涉及后台 LLM 调用成本，默认关、待确认。

`llm_context_messages=100` 保留为 DB 取数条数硬顶。

---

## 8. DB 迁移

新增**两个**迁移函数（追加到 `app/db/_core.py` 的 `_MIGRATIONS`，遵循 `PRAGMA user_version` 框架，禁用启动期 `_ensure_column` 补丁）：

1. `mNNNN_messages_account_id_index`：
   `CREATE INDEX IF NOT EXISTS ix_messages_account_id ON messages(account_id, id);`
   修复热点查询 `WHERE account_id ORDER BY id DESC LIMIT N` 的全账号扫描+排序退化。SQLite/PG 双后端均需生效（PG-parity gate 已在 CI）。
2. `mNNNN_sessions_rolling_summary`（Part 3）：
   给 `sessions` 加 `rolling_summary TEXT`、`rolling_summary_upto_id INTEGER`。

> 迁移 1 与 Part 1 强相关（放大窗口/预算扫描成本敏感），**应随 Part 1 一起上**。

---

## 9. 改动文件清单（diff 级）

| 文件 | 改动 | 所属 |
|---|---|---|
| `app/context_window.py` | **新增**：`estimate_tokens`、`cap_message_chars`、`trim_history_rows(rows, *, token_budget, per_message_max_chars) -> (kept, dropped, metrics)`。纯函数，无 I/O | P1+P2 |
| `app/turn_service.py` | `build_turn_llm_input`：取数后调用 `trim_history_rows`；`history` 由 kept 构建；`_history_covers_previous_session` 改传 kept_rows；metadata 增 `history_dropped_count`/`history_est_tokens`/`message_truncated_count` | P1+P2(+P3 消费) |
| `app/config.py` | 新增 4 个配置字段 | P1+P2+P3 |
| `.env.example` | 4 个字段行内注释 | P1+P2+P3 |
| `app/db/_core.py` | 迁移 1（索引）；迁移 2（sessions 两列，P3） | P1 / P3 |
| `app/prompt_builder.py` | `assemble/build` 增 `rolling_summary` 参数 + 新 Block | P3 |
| `app/context_summarizer.py` | **新增**：后台滚动摘要生成（复用 dreaming 原语 + 水位线） | P3 |
| `app/db/sessions*.py` | `rolling_summary`/水位线读写 helper | P3 |

> 若 P3 的 prompt_builder/turn_service 引入新 `from app.config import settings` 的 router 依赖，按约定在 `tests/conftest.py` 的 `fresh_db`/`client` 两个 fixture 各补 per-module patch（本设计预计不新增 router，故大概率不涉及）。

---

## 10. 测试方案

聚焦优先（改 `context_window` / `turn_service` / 迁移）：

- `tests/test_context_window.py`（新）：
  - 预算裁剪保留尾部最近、丢弃最旧；`budget=0` 原样返回（零行为变更）。
  - 单条超限截断 + 标记；落库副本不受影响。
  - 边界：空历史、单条即超预算（至少留 1 条）、全部在预算内。
- `tests/test_turn_service.py`（扩展）：
  - 裁剪丢掉上一个 session 的旧消息时，`_history_covers_previous_session` 返回 False、carryover **不被抑制**（核心回归）。
  - metadata 字段正确（dropped_count、est_tokens）。
- 迁移测试：索引存在；SQLite + PG-parity gate 通过（`docs` 现有 PG gate）。
- P3：`llm_rolling_summary_enabled=False` 时全链路逐字不变；开时后台生成 + 同步注入；水位线不回退、不重叠 kept window。

全量回归：触及持久化/schema 与跨模块契约，提交前跑 `pytest tests/ -v`。

---

## 11. 风险与回滚

- **抑制不一致（最高优先级）**：必须基于 kept_rows 算抑制，单测覆盖。
- **token 估算偏差**：`len/1.5` 偏粗；预算留足余量即可，真值走 usage，不用于计费。
- **延迟**：P3 摘要仅后台，同步轮零新增 LLM 调用。
- **账号隔离**：裁剪输入已是 account-scoped 行，纯函数不跨账号，不变量不受影响。
- **回滚**：三项均有开关（`*_token_budget=0`、`*_message_max_chars=0`、`rolling_summary_enabled=False`）→ 逐字回到当前行为。索引迁移幂等、纯增益，无需回滚。

---

## 12. 实施顺序（单一 PR）

三项 + 索引在**同一个 PR** 内交付（已定）。编码按下述步骤推进，便于自审与分段测试，但合并为一个 PR：

1. **迁移 1**：`(account_id,id)` 索引——纯增益。
2. **P1+P2**：`context_window.py` + turn_service 接线 + `llm_context_token_budget=8000` / `llm_context_message_max_chars=2000`（**非零默认直接写进代码**，合并即生效）。
3. **P3**：迁移 2（sessions 两列）+ 滚动摘要纯逻辑函数 + 复用 memory 后台链路触发 + prompt block，`llm_rolling_summary_enabled` 默认关（涉及后台 LLM 成本，单独确认后开）。

> 单 PR 落地。P1/P2 合并即生效（默认值对正常对话基本不裁剪，主要作安全上限）；P3 代码就位但默认关，确认后再开。
