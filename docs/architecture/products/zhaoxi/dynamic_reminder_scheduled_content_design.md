# 动态提醒 / 例行简报（scheduled content）方案设计

> 状态：设计定稿（本轮不改代码）。
> 需求：用户在对话里说「每周一三五早上 8 点给我推 AI 热点/技术进展」，系统到点自主检索并生成一条带来源的简报推送。
> 目标账号（历史灰度起点）：`aid_554754198`（归属 aliyun2；当时仍处于 node-only 瘦节点阶段，
> 当前已升级为厚节点）。

## 0. 一句话结论

不新增独立的 `scheduled_content` 任务子系统、不新增对外工具，而是把「例行简报」建模为**提醒的一个履约分支**：
提醒分两种履约方式——`fixed`（发创建时的固定文案，现状）与 `dynamic`（到点跑一次「无用户输入的合成对话轮次」，用工具集产出内容再发）。
统一在提醒模型下的理由：**system prompt 是「每轮 × 全账号」的高频固定成本，而提醒到期是稀疏事件（比对话轮次少 1–2 个数量级）**，把复杂度从 prompt 面挪到到期履约面总成本更低；语义上「到点要做的一件事」本就是同一属概念，「直接给 / 走 LLM 产出再给」只是履约策略差异。

对外新增工具 = 0；`create_reminder` 仅 +1 可选字段。复杂度集中在低频的到期履约、recur 文法扩展、远程对账。

---

## 1. 决策记录（为什么不照 codex 的独立子系统）

| 维度 | codex 方案 | 本方案 | 取舍理由 |
|---|---|---|---|
| 对外工具 | 新增 4 个（create/list/update/cancel_scheduled_content） | 0 新增，复用 reminder 工具 | 4 个工具 schema 进 system prompt = 每轮 × 全账号的固定开销；提醒到期稀疏，成本应落在低频面 |
| 任务存储 | 新建 `scheduled_content_tasks` | 复用 `reminders` 行 + 2 列 + metadata | 语义同属「到点做的事」；避免两套调度/认领/隔离逻辑并行 |
| run 记录 | 新建 `scheduled_content_runs` | 新建 `reminder_content_runs`（仅 dynamic 用，可观测性） | 生成正文/来源/搜索 trace 需要留痕，这张表保留 |
| OutboundCategory | 新增 `SCHEDULED_CONTENT` | 复用 `USER_REMINDER`（已 exempt） | 语义=用户明确要求、豁免普通主动配额；子类型打 metadata 供分析即可 |
| 周期表达 | 新建 schedule_json | 扩展 `recur_rule` 文法为多星期几 | 留在提醒模型内，顺带惠及普通提醒 |
| 履约工具集 | 强制只 web_search | 通用「合成轮次」，工具集可配；v1 默认 web_search + 首轮强制 | 长期动态提醒应能用全工具（同普通轮次），接口先按通用形状定，实现先窄 |

**codex 说对、本方案保留的点**：结构化周期、`next_run_at` 由后端算（绝不信 LLM）、强制搜索、搜索失败不发编造、限一条微信消息、幂等键含周期时间戳、**远程账号 pending→enqueued→对账**（这条最关键，见 §6）、灰度双机配置。

---

## 2. 数据模型

### 2.1 `reminders` 扩展（新增迁移，追加到 `_MIGRATIONS`）

> 遵循项目约定：**新增迁移函数**，不用启动期 `_ensure_column` 补丁。

新增两列：

| 列 | 类型 | 说明 |
|---|---|---|
| `fulfillment` | TEXT NOT NULL DEFAULT `'fixed'` | `'fixed'` \| `'dynamic'`。`fixed`=发 `text` 固定文案（现状零回归）；`dynamic`=到点跑履约轮次 |
| `content_meta_json` | TEXT | 仅 dynamic 用：topic、instructions、max_items、tool_policy、last_success_run_at 等（见下） |

`content_meta_json` 结构（示例）：
```json
{
  "topic": "AI 热点新闻、模型和开发工具技术进展",
  "instructions": "偏工程实践，少纯融资新闻",
  "max_items": 5,
  "tool_policy": {"allowlist": ["web_search"], "force_first": ["web_search"]},
  "last_success_run_at": "2026-07-17 08:00:03"
}
```
> 说明：`fixed` 提醒这两列为默认值/NULL，`_decode_reminder` 兼容旧行。`text` 对 dynamic 提醒承载「用户原话/topic 摘要」，仅用于展示与创建确认，不作为发送正文。

### 2.2 `reminder_content_runs`（新表，仅 dynamic 履约留痕）

```sql
CREATE TABLE IF NOT EXISTS reminder_content_runs (
    id                  TEXT PRIMARY KEY,
    reminder_id         TEXT NOT NULL,
    account_id          TEXT NOT NULL,             -- 冗余，便于按账号隔离查询
    scheduled_for       TEXT NOT NULL,             -- 本次到期的 due_at（周期时间戳）
    status              TEXT NOT NULL,             -- pending|running|enqueued|sent|skipped|failed
    attempts            INTEGER NOT NULL DEFAULT 0,
    generated_text      TEXT,
    outbound_message_id INTEGER,
    search_ok           INTEGER NOT NULL DEFAULT 0,-- 至少一次搜索成功
    search_trace_json   TEXT,                      -- 命中来源 URL/标题/时间
    error               TEXT,
    metadata_json       TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','+8 hours')),
    UNIQUE(reminder_id, scheduled_for)             -- 防重复搜索/重复发送的第一道闸
);
CREATE INDEX IF NOT EXISTS ix_rcr_status ON reminder_content_runs(status);
CREATE INDEX IF NOT EXISTS ix_rcr_account ON reminder_content_runs(account_id, created_at);
```

**账号隔离**：`reminder_content_runs` 所有查询强制带 `account_id`（核心不变量）。

---

## 3. recur_rule 文法扩展：多星期几

现状 `^(daily|weekly:[0-6]|monthly:...)$`，`weekly:N` 只能表一个星期几，无法表「一三五」。

扩展为 `weekly:` 后接**逗号分隔的多个星期几**（0=周一…6=周日）：

- 正则：`weekly:[0-6](,[0-6])*`（单个 `weekly:3` 仍合法，向后兼容）。
- `compute_next_due_at`：`weekly` 分支解析出星期几集合，取「严格晚于 last_due 的最近命中日」，命中时刻用 last_due 的 HH:MM:SS。

「一三五 08:00」= `recur_rule="weekly:0,2,4"`。**这一改动同时惠及普通固定提醒**（「工作日提醒」也能一条搞定）。

> 创建时 `next_run_at`（首次 due_at）由后端按 `weekly:0,2,4` + 时刻 + 当前时间算：今天（周三 2026-07-15）若已过 08:00 → 首次 = 周五 2026-07-17 08:00。**与验收标准一致，且绝不采用 LLM 提供的日期。**

---

## 4. 工具层：0 新增，`create_reminder` +1 可选字段

`create_reminder`（`definitions.py`）新增可选参数：

- `fulfillment`（可选，默认 `fixed`）：`dynamic` 表示「定期给新内容」而非固定文案。
- 当 `fulfillment=dynamic`：`text` 存 topic/用户原话，可选带 `topic` / `max_items`（也可全部塞进 `text`，handler 解析）。

工具描述改写（并**回收当前未提交 diff 的临时护栏**）：

- 现 diff 里给 reminder/proactive_settings/allowed_windows 加的「禁止冒充定时内容订阅」提示 → 改为**引导**：「用户要定期推送某类**新**内容时，用 `create_reminder` 并设 `fulfillment=dynamic`」。
- 保留的边界描述：`fixed` 提醒仍只发固定文案；`allowed_windows` 仍只是发送过滤器，不创建内容。

`list_reminders` / `cancel_reminder` / `update_reminder`：**无需改工具面**，天然覆盖用户「看/关/改」诉求（dynamic 提醒也是 reminder 行）。序列化时把 `fulfillment` 带出来即可。

> 净 system prompt 增量 = 一个可选字段 + 一行描述，全账号可用；对比 codex 4 个新工具，成本低 1–2 个数量级。

---

## 5. 履约层：dynamic = 无用户输入的「合成对话轮次」，工具集可配

**核心抽象**：到期履约不是「专用 web_search 脚本」，而是**一次没有用户输入的合成轮次**——给定专用 system prompt + 一个工具集，跑与普通对话**同一套 tool loop / executor**，产出一条消息。工具集是**策略参数**，不是写死单工具。

- 构造合成 `TurnContext`：`account_id` 取自提醒行、无用户消息、system prompt = 专用内容 prompt（**不复用聊天 prompt**）。
- **工具集 = registry × ctx 运行时开关，再叠履约级 `tool_policy` 过滤**：
  - **v1**：`allowlist=["web_search"]` + `force_first=["web_search"]`（配置项，非硬编码 if-else）。
  - **后续**：放开 allowlist（或置空=继承普通轮次全集），**不改履约主干，只改配置/行级 tool_policy**。
- **「强制搜索」的实现取舍（thinking 模型约束）**：原设计想用 `tool_choice` 在 API 层强制首轮命中 web_search。但当前 PRO 档 `deepseek-v4-pro` 是 thinking 模型，其 API **只接受 `tool_choice="auto"`**，下发指定函数或 `"required"` 一律 400（`Thinking mode does not support this tool_choice`）。因此改为：**首轮 `tool_choice=auto` + 履约提示词硬性要求先搜索 + `search_ok` 后置校验**（搜不到就判失败、不发编造内容）。硬保证由「校验」而非「API 强制」提供；`DYNAMIC_REMINDER_FORCE_FIRST_TOOL` 默认置空，仅当将来换用非 thinking provider 需要 API 层强制时才设值。
- **护栏复用现成的**：每轮工具调用硬上限（参照 TDAI 主动检索的 `TurnContext` 计数器）、account 强隔离、超时降级 never-raise。通用化到全工具时这些闸门正好复用。

专用 prompt 要求：强制搜索、`date_after` 优先取 `last_success_run_at`（首次取最近一周）、只引用搜索结果的 URL/标题/时间、输出限一条微信消息、**搜索失败不发编造内容**、生成 3–5 条。

配置项：
- `DYNAMIC_REMINDER_FULFILLMENT_TOOLS`（默认 `web_search`）
- `DYNAMIC_REMINDER_FORCE_FIRST_TOOL`（**默认空=auto**；thinking 模型只接受 auto，见上）

---

## 6. 运行链路（复用 reminder 骨架 + dynamic 分支 + 远程对账）

在 `dispatch_reminder`（`obligations/reminders.py`）里按 `fulfillment` 分叉；scheduler（`orchestration/scheduler.py`）的 reminders step 不变（dynamic 提醒也在 `list_due_reminders` 里）。

### 6.1 fixed 分支
现状不变，零回归。

### 6.2 dynamic 分支（单次到期）

1. `claim_due_reminder`（原子，现有）→ claim 成功。
2. `INSERT reminder_content_runs ... UNIQUE(reminder_id, scheduled_for)`：重复扫描命中 UNIQUE → 直接 skip（防重复搜索/发送，验收标准）。
3. **可触达检查**：`get_account_touch_state == STALE`（复用 `touch_state.py`）：
   - 不可触达 → run=`skipped`（reason=`touch_stale`）、**先不搜索**（省成本）；提醒按 recur 推进到下周期（复用 `reschedule_reminder_stale_touch` + 扩展后的 `compute_next_due_at`）。
4. **履约合成轮次**（§5）：跑通用 tool loop；校验 `search_ok`；生成正文。失败 → run=`failed`，进 30 分钟内重试（≤ `DYNAMIC_REMINDER_MAX_RETRIES`），**不发编造/过期占位**。
5. `dispatch_proactive_text(source="reminder", product_category="user_reminder", bypass_quiet_hours=True, idempotency_key=f"reminder-{id}-{compact(scheduled_for)}", metadata={"fulfillment":"dynamic",...})`。
6. **run 状态机（关键修正，修 reminder 现有远程 bug）**：现 `reminders.py:116-149` 把非 `sent` 一律判 `failed`；dynamic 分支改为：
   - outbound `sent` → run=`sent` + `mark_reminder_sent(next_due_at=...)`；
   - outbound `pending`（**远程账号 enqueue**）→ run=**`enqueued`**（未终结）；**提醒推进到下一周期**。
     — 实现取舍：入队即视为「本期已交付/在途」并推进周期，而非把提醒停在 `sending` 等对账。
     停在 `sending` 会在对账迟迟不来/outbound 永久 pending 时让整条序列卡死（比丢一期更糟）；
     推进周期保证序列永远前进，代价仅是「远程发送最终失败时不重发这一期」，可接受。
   - outbound `cancelled`（政策/moderation）→ run=`skipped`；提醒推进周期。
   - 其它 outbound 失败 → run=`failed`；按重试策略（同周期重试至上限，再推进）。
7. **对账（reconciliation）**：dynamic obligation 增一步扫 `status='enqueued'` 的 run，按 `outbound_message_id` 查 outbound 终态 → 把 **run** 翻成 `sent`/`failed`（提醒已在入队时推进，故此处只落 run 终态，供 run 历史/观测准确）。**这一步是 reminder 现有代码没有、远程账号（aliyun2）必需的。**

> 为什么远程 bug 会真实触发：目标账号在 aliyun2，对中心调度器是「远程」，`dispatch_proactive_text` → `enqueue_proactive_text` 返回 `status="pending"`，照现状会被判 failed。dynamic 分支必须新增 enqueued 态 + 对账。

### 6.3 调度归属（已定：aliyun1 统一调度）
**aliyun1 统一调度所有账号（含 aliyun2 归属账号）**；aliyun2 当前是厚 node，会本地处理 turn
并直连中心 PG，但不运行 central-only scheduler。由此：

- dynamic 提醒的到期扫描**必须跨节点覆盖目标账号**：`list_due_reminders` 传 `node_id=None`（不按 `assigned_node_id` 分片到 aliyun1），否则 aliyun2 归属账号（如 `aid_554754198`）永远扫不到。
- 出站侧由 `dispatch_proactive_text` 按账号归属自动分流：aliyun1 账号 inline 直发；aliyun2 账号 `enqueue`（返回 pending）→ 走 §6.2-6/7 的 enqueued→对账。
- 因此「远程对账」不是可选项，而是本部署形态下 aliyun2 账号简报能落地的**必要条件**。

---

## 7. 策略与安全边界

- **分类**：复用 `USER_REMINDER`（exempt 豁免总开关/allowed_windows/quiet hours/日上限），符合「用户明确要求」；dynamic 子类型打 `metadata.fulfillment` 供分析。**不新增 OutboundCategory。**
- **仍全程生效**（在 outbound 层，不受 exempt 影响）：账号状态、route 能力过滤、**同步敏感词 `check_sync_guard`（对生成正文）**、moderation、网关限速、`idempotency_key` 去重、微信 24 小时送达窗口（不可绕过，到点不可达即 §6.2-3 跳过）。
- **成本控制**：`touch_stale` 前置跳过（不搜索）+ 首轮才搜 + `max_items` 上限 + 每轮工具调用硬上限。

---

## 8. 灰度配置（`app/config.py` + `.env.example`）

```
DYNAMIC_REMINDER_ENABLED=true                 # 单一总开关：开=对所有账号放开（全量），关=整体禁用
DYNAMIC_REMINDER_MAX_ACTIVE_PER_ACCOUNT=5     # 每账号活跃 dynamic 提醒上限
DYNAMIC_REMINDER_MAX_RETRIES=2                # 30 分钟内重试次数
DYNAMIC_REMINDER_FULFILLMENT_TOOLS=web_search # 履约工具集（逗号分隔，后续放开）
DYNAMIC_REMINDER_FORCE_FIRST_TOOL=web_search  # 首轮强制工具
```
- **默认全量、无账号灰度**：`dynamic_reminder_enabled=true` 后对**所有账号**放开创建/履约；功能启停只靠这一个总开关（不再有 account allowlist 门控）。
- **两机都配**：aliyun2 处理入站→创建 dynamic 提醒需 enabled；调度机执行到期履约+出站分发。
- `create_reminder` 的 `fulfillment=dynamic` 受 `dynamic_reminder_enabled` 门控（关闭时忽略该字段，提示不支持）。
- **无需**改 `TDAI_RECALL_ACCOUNT_ALLOWLIST`（web_search 已可用）。

---

## 9. 主要文件

| 文件 | 改动 |
|---|---|
| `app/db/_core.py` | 新增迁移：reminders +2 列、建 `reminder_content_runs` |
| `app/products/zhaoxi/infrastructure/persistence/proactive.py` | dynamic 提醒读写、run CRUD/claim/状态翻转/对账查询（均带 account_id） |
| `app/reminder_utils.py` | `recur_rule` 文法扩展多星期几 + `compute_next_due_at` 多目标日 |
| `app/tools/definitions.py` | `create_reminder` +`fulfillment` 字段；护栏提示改引导；序列化带出 fulfillment |
| `app/products/zhaoxi/tools/reminder_handlers.py` | 创建时解析 topic/max_items → `content_meta_json`；确认回复给精确下次时间 |
| `app/products/zhaoxi/proactive/obligations/reminders.py` | dispatch 按 fulfillment 分叉；dynamic 履约 + run 状态机 + 对账步骤 |
| `app/products/zhaoxi/proactive/fulfillment/*`（新增） | 合成轮次履约器（专用 prompt + 可配工具集 tool loop + 护栏） |
| `app/config.py` / `.env.example` | 6 个灰度项 |
| `app/products/zhaoxi/api/debug.py` / `admin_proactive.py` | dynamic 提醒 run-once、run 记录查询 |
| 相关产品/技术文档 | 解除「禁止订阅式日报」旧约束描述 |

---

## 10. 测试方案

- **unit**（`make test-unit`）：
  - `compute_next_due_at("weekly:0,2,4", 周三)` → 周五同刻；跨周边界；单 `weekly:3` 向后兼容；今天已过点取次日命中。
  - `validate_recur_rule` 接受 `weekly:0,2,4`、拒 `weekly:7`/空段。
- **db**：`claim` 原子性；`reminder_content_runs` `UNIQUE(reminder_id, scheduled_for)` 去重；account 隔离（他账号查/改/取消该 dynamic 提醒全拒）。
- **integration**：
  - 到期 → 履约合成轮次（mock 搜索成功/失败两路）→ 远程账号 dispatch 返回 pending → run=`enqueued` → 对账翻 `sent` → 提醒推进下周一；
  - 重复扫描不重复搜索/发送；`touch_stale` 跳过且不搜索、提醒仍推进；
  - 搜索失败不发、进重试；
  - `fixed` 提醒回归不变。
- 触及共享 schema/调度/工具/出站策略：先跑聚焦测试，再 `make test`（PG 侧 `make test-pg`）。

---

## 11. 已定决策（原待确认项，已闭环）

1. **调度归属**：aliyun1 统一调度所有账号（含 aliyun2 归属）；dynamic 到期扫描用 `node_id=None` 跨节点覆盖，出站按账号归属 inline/enqueue 分流。远程对账为必要路径。详见 §6.3。
2. **update 语义**：改周期**只影响下次 `next_run_at` 计算**；已 `claim` 的 run 不作废、继续按其 `scheduled_for` 跑完。即「改动对进行中的一次履约无副作用」。
3. **履约工具集 v1**：确认**只带 `web_search`** + 首轮强制；接口/数据结构按「任意工具集」形状定（§5），后续放开只改配置，不返工。
