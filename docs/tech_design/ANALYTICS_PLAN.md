# Nearline Analytics 建设规划（工程实施方案）

> 状态：规划草稿 | 日期：2026-06-08

> **文档分工**：本文是**工程实施方案**——物理存储（facts/marts 双库）、目录结构、ETL 机制、基础表 schema、实施 phases。
> **指标口径、数据不变量、数据质量检查**以 [`analytics_foundation_design.md`](./analytics_foundation_design.md) 为唯一事实源。两者冲突时以 foundation 为准。

---

## 一、背景与目标

本文档规划 `nearline/` 目录下数据分析基建的建设方式，覆盖四个核心分析域：

1. 用户增长与留存（DAU / 注册 / 留存率 / 贝壳消耗）
2. 主动消息发送情况（分类别 / 覆盖率 / 回复率 / 时间分布）
3. Dreaming 机制每日运行情况
4. 新用户 Onboarding 漏斗 + 人设选择分析

目标是：**可重复运行、产出可审阅的日报**，逐步积累历史快照，支撑后续趋势分析。

---

## 二、数据源现状

主库：`data/ai4all.sqlite3`（只读访问）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `accounts` | 注册用户，onboarding 状态 | `created_at`, `onboarding_state` |
| `messages` | 收发消息记录 | `direction`, `account_id`, `created_at`, `session_id` |
| `sessions` | 对话 session，按 business_day 归日 | `business_day`, `turn_count`, `account_id` |
| `outbound_messages` | 所有主动外发消息 | `source`, `product_category`, `status`, `sent_at`, `scheduled_at` |
| `dreaming_runs` | Dreaming 每次运行记录 | `status`, `source_type`, `token_input/output`, `started_at`, `completed_at` |
| `dreaming_memory_items` | Dreaming 产出的记忆条目 | `operation`, `category`, `apply_status` |
| `cost_events` | LLM token 计费事件 | `cost_type`, `computed_shell_micros`, `created_at` |
| `entitlement_ledger` | 贝壳流水 | `entry_type`, `amount_shell_micros`, `created_at` |
| `proactive_commitments` | 承诺类主动消息 | `status`, `sent_at`, `due_at` |
| `reminders` | 提醒类主动消息 | `status`, `sent_at` |
| `content_invitations` | 话题邀约类主动消息（当前无数据） | `status`, `invited_at`, `responded_at` |
| `profiles` | 人设配置（style 当前全为 NULL） | `style`, `account_id` |

**主动消息分类（`outbound_messages.product_category`）：**

| category 值 | 含义 | source 来源 |
|---|---|---|
| `user_reminder` | 用户自设提醒 | reminder |
| `companion_followup` | 陪伴式主动关心 | account_check, heartbeat, commitment |
| `content_invitation` | 话题邀约 | content_invitation |
| `content_invitation_response` | 话题邀约响应 | content_invitation_titles/feedback |
| `task_result` | 异步任务结果 | async_task_result |
| `legacy_proactive` | 历史兜底类 | 其他 |

**Onboarding 状态机：**
`pending → step1_sent → step2_sent → step3_sent → complete / timed_out`

---

## 三、基建设计

### 3.1 目录结构

```
nearline/
├── analytics/
│   ├── __init__.py
│   ├── source_db.py       # 只读连接主库（operational source）
│   ├── facts_db.py        # 读写 facts.sqlite3（dim_/fct_，只追加）
│   ├── marts_db.py        # 读写 marts.sqlite3（agg_，可删重建）
│   ├── models.py          # dim/fct/agg 与分析域输出 dataclass
│   ├── warehouse/         # 【新增】基础数据表层（dim_/fct_）
│   │   ├── __init__.py
│   │   ├── facts_schema.sql  # dim_/fct_ 建表（facts.sqlite3）
│   │   ├── marts_schema.sql  # agg_ 建表（marts.sqlite3）
│   │   ├── etl.py            # 增量抽取 + watermark 管理调度
│   │   ├── dim_account.py    # 账号维度装载
│   │   ├── dim_date.py       # 日历维度生成
│   │   ├── fct_message.py    # 会话消息事实（增量）
│   │   ├── fct_proactive.py  # 主动消息事实 + 回复归因
│   │   ├── fct_dreaming.py   # Dreaming 运行 / 记忆条目事实
│   │   └── fct_onboarding.py # Onboarding 累积快照
│   └── metrics/           # 日聚合层（agg_），消费 dim_/fct_
│       ├── __init__.py
│       ├── daily_users.py     # 分析域1：增长与留存
│       ├── proactive.py       # 分析域2：主动消息
│       ├── dreaming.py        # 分析域3：Dreaming 运行
│       └── onboarding.py      # 分析域4：Onboarding 漏斗 + 人设
├── reporting/
│   ├── __init__.py
│   ├── formatter.py       # Markdown 报告模板渲染
│   └── writer.py          # 输出到文件 / 推送飞书（可选）
├── data/
│   ├── facts.sqlite3      # dim_/fct_（durable 基线，只追加，纳入备份）
│   ├── marts.sqlite3      # agg_（可删重建，gitignore，不依赖备份）
│   └── reports/           # 生成的 Markdown 日报（可归档）
├── notebooks/             # 探索性 Jupyter notebook
├── run_etl.py             # 入口：增量刷新 dim_/fct_ 基础表
├── run_daily.py           # 入口：基于基础表计算 agg_ 日指标 + 出报告
├── run_backfill.py        # 入口：补跑历史日期（ETL + 聚合）
└── requirements.txt       # nearline 专属依赖
```

### 3.2 分层原则

```
操作库（data/ai4all.sqlite3，只读 source）
  └→ 基础表层 dim_/fct_（facts.sqlite3，只追加，durable 基线）
       └→ 聚合层 agg_（marts.sqlite3，可删重建）
            └→ 报告层（reporting/）—— 消费 agg_，渲染文档
```

- **基础表层（新增核心）**：把操作库的原始事件清洗、归一为稳定的维度/事实表（`dim_`/`fct_`），落在 `facts.sqlite3`。事件粒度、增量装载、只追加。这是整个分析体系的**长期基线**，详见第 3.6 节。
- **聚合层**：每个分析域模块接收 `date: str`（YYYY-MM-DD），**只读 `facts.sqlite3` 的 `dim_`/`fct_`**（不再直接碰操作库），输出日粒度指标写入 `marts.sqlite3` 的 `agg_` 表。纯函数、无副作用、可单测。
- **报告层**：从 `marts.sqlite3` 的 `agg_` 读，支持离线渲染。

**为什么物理隔离成两库**：
- `facts.sqlite3`（`dim_`/`fct_`）是不可重建的事实基线——操作库一旦裁剪/轮转就丢失，必须纳入现有自动备份；只追加写入，无 DROP/DELETE。
- `marts.sqlite3`（`agg_`）任何时候都能从 `facts.sqlite3` 重算，视为缓存，`gitignore` 且**不依赖备份**；重建口径只需 `DROP` 后重跑聚合，不会污染事实基线。
- 物理隔离让备份边界、写入权限、重建动作三者互不交叉：备份只盯一个文件，重算只动另一个文件。

### 3.3 依赖选型

| 包 | 用途 | 理由 |
|---|---|---|
| `pandas` | 数据处理、留存计算 | 标准分析工具，SQL 结果直接转 DataFrame |
| `jinja2` | 报告模板渲染 | 已在 Python 生态内，不引入 headless browser |
| `pyarrow`（可选） | 导出 Parquet 快照 | 数据量大时备用，初期不启用 |

nearline 的依赖与主 app 完全隔离，放在 `nearline/requirements.txt`，不修改项目根 `requirements.txt`。

### 3.4 调度与运行方式

**初期**：手动运行脚本（`python nearline/run_daily.py --date 2026-06-07`）。

**中期**：系统 cron，独立于 app 进程：
```cron
# 每天 02:00 跑前一天的指标
0 2 * * * cd /path/to/weixin_bot && .venv/bin/python nearline/run_daily.py --yesterday
```

不依赖 dreaming_scheduler，不集成进 FastAPI app，保持完全隔离。

### 3.5 幂等性与回溯

- **基础表增量**：`fct_` 用 watermark（如 `messages.id`）只追加新事件；`dim_` 用主键 UPSERT 刷新当前态。维护 `etl_watermark` 表记录各表已装载位点，重跑只补增量。
- **聚合幂等**：`agg_` 以 `date` 为边界（`sessions.business_day` 或 `DATE(created_at)`），写入用 `INSERT OR REPLACE`，重算同一天不产生重复。
- **回溯**：`run_backfill.py --from 2026-05-30 --to 2026-06-07` 先重建 `fct_`/`dim_`，再逐日重算 `agg_`。

### 3.6 基础数据表目录（数仓 dim_/fct_ 分层）

> 这一层是本次新增的核心。命名约定：`dim_` 维度、`fct_` 事件粒度事实（durable 基线）、`agg_` 日聚合 mart（可重建）。下表为最小但完整的星型模型，覆盖四个分析域，并为后续周留存、窗口敏感性、话题挖掘等长期需求保留事件粒度。

**维度表（dim_）**

| 表 | 粒度 | 关键列 | 装载 |
|---|---|---|---|
| `dim_date` | 一行一日 | `date` PK, year, month, iso_week, day_of_week, is_weekend | 生成器按区间填充，不读操作库；用于补齐零活跃日、周/月聚合 |
| `dim_account` | 一行一账号 | `account_id` PK, channel, is_debug, registered_at, registered_date, registered_week（cohort 键）, first_inbound_at, first_active_date, last_active_date, current_onboarding_state | 主键 UPSERT（SCD-1 当前态）。把 `contacts`/`sender_id` 历史命名归一为 `account_id`，对分析侧隐藏技术债 |

**事实表（fct_，事件粒度，只追加 / 纳入备份）**

| 表 | 粒度 | 关键列 | 来源 / 装载 |
|---|---|---|---|
| `fct_message` | 一行一条会话消息 | `message_pk`（=messages.id）, account_id, session_id, direction, message_type, created_at, event_date, event_hour, business_day, is_account_first_ever, is_account_first_of_day | `messages` 表，watermark 增量。支撑 DAU、消息量、留存 |
| `fct_proactive_message` | 一行一条主动外发 | `id`（=outbound_messages.id）, account_id, category, source, status, scheduled_at, sent_at, sent_date, sent_hour, **replied, reply_message_id, reply_latency_sec, reply_window_hours, resolution_status** | `outbound_messages` 表。回复归因一次算清（见下"晚到归因"），下游直接读列 |
| `fct_dreaming_run` | 一行一次 dreaming run | `id`（=dreaming_runs.id）, account_id, source_type, status, started_at, completed_at, run_date, duration_sec, token_input, token_output | `dreaming_runs` 表，watermark 增量 |
| `fct_dreaming_memory_item` | 一行一条记忆产出 | `id`, dreaming_run_id, account_id, operation, category, apply_status, created_at, event_date | `dreaming_memory_items` 表。供域3 的 operation/category/apply_status 拆解 |
| `fct_onboarding_journey` | 一行一账号的 onboarding 旅程 | `account_id` PK, registered_at, step1_sent_at, step2_sent_at, step3_sent_at, completed_at, timed_out_at, current_state, time_to_complete_sec, is_complete, is_timed_out | **累积快照（accumulating snapshot）**，随状态推进回填里程碑时间戳 |
| `fct_cost_event`（可选/Phase 2） | 一行一条计费事件 | `id`, account_id, cost_type, shell_micros, model, created_at, event_date | `cost_events` 表，支撑贝壳消耗趋势 |

**ETL / 装载机制**

- `etl_watermark(table_name, last_id, last_run_at)`：记录各 `fct_` 已装载的最大 id，增量只取 `id > last_id`。
- **回复晚到归因（fct_proactive_message）**：回复判定依赖"发送后 N 小时（默认 **24h**，foundation §4.2）内是否有 `role='user'` 入站消息"，且窗口被同账号下一条主动消息的 `sent_at` 截断；因此晚间发出的消息要等次日才能定论。装载分两段：① 新外发先以 `resolution_status='pending'` 落库；② 每次 ETL 对窗口已闭合的 pending 行回填 `replied/reply_latency_sec`，置 `resolved`。把"窗口假设"显式存为 `reply_window_hours` 列，便于日后做窗口敏感性分析。
- **Onboarding 历史局限**：当前 schema 只有 `onboarding_state` + `onboarding_updated_at`，无逐步时间戳。`fct_onboarding_journey` 的中间里程碑（step1/2/3）历史值只能为空；上线后由 ETL 每日捕捉状态变化回填，或推动 app 侧在 onboarding 事件落库（见第五节缺口）。

> **实现提示**：第四节各域的 SQL 是"指标逻辑"示意，落地时一律改为读 `dim_`/`fct_` 基础表，而非直接查操作库原始表。两者列名已对齐。

---

## 四、各分析域详细设计

> **口径以 [`analytics_foundation_design.md`](./analytics_foundation_design.md) 为唯一事实源**（指标定义、数据不变量、数据质量检查）。本节只描述**工程落地**：每个指标读哪张 `fct_`/`dim_` 表、写哪张 `agg_` 表。两文档口径冲突时，一律以 foundation 为准。
>
> 全节默认遵守 foundation 的不变量：① 活跃/消息口径用 `direction='inbound' AND role='user'`；② 默认排除 `is_debug=1`；③ 贝壳消耗看 `entitlement_ledger` debit，资源成本看 `cost_events`，二者不混；④ 不读取任何正文（content/prompt/memory）。这些约束在 ETL 阶段固化进 `fct_` 列，`agg_` 计算时不再重复判断。

### 域1：用户增长与留存（`metrics/daily_users.py`）

**读取**：`fct_message`（已带 `is_account_first_ever`、`role`、`event_date`）、`dim_account`（cohort 键、`is_debug`）、`fct_cost_event` / 贝壳流水。**写入**：`agg_daily_users`。

**输出指标（口径详见 foundation §4.1）：**

| 指标 | 工程口径（读基础表） |
|---|---|
| `new_users` | `dim_account` 当日 `registered_date`（同时可出 `platform_users` 口径，headline 用哪个见 foundation §9） |
| `dau` | `fct_message` 当日 distinct `account_id`（已过滤 `role='user'` + 非 debug） |
| `inbound_messages` | `fct_message` 当日用户入站条数 |
| `d1_retention_rate` | **首聊 cohort**（foundation 推荐）：以 `fct_message.is_account_first_ever` 定 cohort，D+1 活跃比例 |
| `d7_retention_rate` | 同上，D+7（数据积累后启用） |
| `shell_consumed_micros` | 贝壳流水 `entitlement_ledger` 当日 debit 绝对值汇总（**非** cost_events） |

**注意：** 留存依赖历史，cohort 样本数 < 10 时报告标注"仅观察"，不展示比率（foundation §8）。

---

### 域2：主动消息（`metrics/proactive.py`）

**读取**：`fct_proactive_message`——回复归因已在 ETL 阶段算清并落列（`replied`、`reply_latency_sec`、`reply_window_hours`、`resolution_status`），`agg_` 层**不再现算窗口**，直接读列聚合。**写入**：`agg_daily_proactive`、`agg_hourly_proactive`。

**回复归因口径（foundation §4.2，已固化进 fct_）：**
- 窗口默认 **24h**（非 4h），并被同账号下一条主动消息的 `sent_at` 截断。
- 归因目标为 `role='user'` 的首条入站消息。
- 分类别（`product_category`）分别计算，不同类别不混用同一个回复率结论。

**输出指标：**

| 指标 | 工程口径（读 `fct_proactive_message`） |
|---|---|
| `total_sent` | 当日 `status='sent'` 条数 |
| `sent_by_category` | 按 `product_category` / `source` 分组发送量 |
| `blocked_by_policy` | 非成功且有 `policy_reason` 的拦截数（quiet hours / 日上限 / 冷却） |
| `coverage_rate` | 收到主动消息的 **eligible 账号** / 当日 eligible 账号（eligible 定义见 foundation §4.2，非简单 DAU） |
| `reply_rate_by_category` | 各类别 `replied=1` 占 sent 比例；同时可出 1h/6h/24h 多窗口 |
| `first_reply_latency_p50` | `reply_latency_sec` 中位数（用 P50/P75，不用均值） |
| `hourly_distribution` | 按 `sent_hour` 的发送量 / 回复率（低样本不下策略结论） |

---

### 域3：Dreaming 运行情况（`metrics/dreaming.py`）

**读取**：`fct_dreaming_run`、`fct_dreaming_memory_item`，外加 `scheduler_heartbeats`（调度健康）。**写入**：`agg_daily_dreaming`。口径详见 foundation §4.3。

**关键口径约束（foundation §4.3）：**
- `status` 区分 `succeeded` / `partial` / `failed`——**`partial` 单独展示**，不能并入成功或失败。
- `skipped` 是 memory item 的 `apply_status`，**不是** run 状态；自动跳过敏感/低置信条目是预期安全行为，应用率不是越高越好。
- `token_input/output` 可能为空，聚合时按 NULL 安全处理。

**输出指标：**

| 指标 | 工程口径 |
|---|---|
| `runs_by_status` | `fct_dreaming_run` 按 `status`（含 partial 独立列） |
| `accounts_covered` | distinct `account_id` |
| `avg_duration_sec` | `fct_dreaming_run.duration_sec`（ETL 已算好） |
| `tokens_input` / `tokens_output` | NULL 安全汇总 |
| `items_applied_rate` | `apply_status='applied'` / 生成条目数 |
| `items_by_skip_reason` | 按 `skip_reason` 汇总 |
| `scheduler_status` | heartbeat 是否在阈值内、最近错误 |

---

### 域4：Onboarding 漏斗 + 人设分析（`metrics/onboarding.py`）

**漏斗定义：**

```
注册（pending）
  → step1_sent（发送欢迎语，询问称呼）
  → step2_sent（确认称呼，询问人设偏好）
  → step3_sent（确认人设，发送完成语）
  → complete
  ⊥ timed_out（任意步骤超时）
```

**读取**：`fct_onboarding_journey`（累积快照，里程碑时间戳）、`dim_account`。**写入**：`agg_onboarding_funnel_daily`。口径详见 foundation §4.4。

注意 foundation 区分**两段漏斗**：① Web 注册/扫码绑定（`phone_verifications` → `binding_intents`）；② 微信首次聊天 onboarding（`pending → step1/2/3_sent → complete/timed_out`）。本模块先做第②段，第①段待事件补齐。

**人设分析（当前缺口，foundation §4.4）：**

⚠️ 人设选择写入 `SOUL.md/IDENTITY.md/USER.md`，**不适合**作长期结构化分析源；`profiles.style` 也全为 NULL。foundation 建议补 `onboarding_events` / 通用 `analytics_events` 结构化事件后再统计"预设/改名预设/自定义/跳过"分布。本模块当前只输出占位，明确标注"数据待积累"。

**输出指标：**

| 指标 | 工程口径 |
|---|---|
| `funnel_snapshot` | `fct_onboarding_journey` 各里程碑到达数（存量） |
| `daily_cohort_funnel` | 当日注册 cohort 的漏斗推进 |
| `completion_rate` | `is_complete` / (complete + timed_out) |
| `median_step_latency` | 里程碑间耗时中位数（历史值受 schema 局限，见第五节） |
| `style_distribution` | 待 `onboarding_events` 落库后启用 |

---

## 五、已知数据缺口与后续建议

> 缺口与埋点补齐的完整论述见 foundation §3（数据基线缺口）与 §4.4（onboarding 事件）。下表只列**影响本期基础表实现**的关键项。

| 缺口 | 影响 | 建议（与 foundation 对齐） |
|---|---|---|
| Onboarding 无状态流转历史 | `fct_onboarding_journey` 中间里程碑历史值只能为空 | 补 `onboarding_events` / `analytics_events` 结构化事件（foundation §4.4） |
| 人设选择只在上下文文件 | 无结构化人设分布 | 同上，事件含 `persona/persona_source` |
| 主动消息无直接回复外键 | `fct_proactive_message` 回复靠时间窗归因 | 窗口假设显式存 `reply_window_hours` 列，便于后续敏感性分析；默认 24h（foundation §4.2） |
| `content_invitations` 无数据 | 话题邀约分析待验证 | 功能上线后自动有数据 |
| 留存率分母不稳定 | 数据早期留存不可信 | cohort 样本 < 10 标注"仅观察"，不展示比率（foundation §8） |
| 首条 inbound 触发欢迎可能不写 `messages` | 低估 onboarding 第一步触发量 | 由 `welcome_sent` 事件补齐（foundation §4.4） |

---

## 六、实施路径

### Phase 0：基建 + 基础表层（当前）

- [ ] 搭建 `nearline/analytics/` 目录结构
- [ ] 实现 `source_db.py`（只读操作库）、`facts_db.py`、`marts_db.py`
- [ ] 编写 `facts_schema.sql`（dim_/fct_ + etl_watermark）、`marts_schema.sql`（agg_）
- [ ] 实现 ETL 装载器：`dim_date`、`dim_account`、`fct_message`（watermark 增量先跑通）
- [ ] 把 `facts.sqlite3` 纳入现有自动备份任务；`marts.sqlite3` 加入 `.gitignore`
- [ ] 添加 `nearline/requirements.txt`（pandas, jinja2）

### Phase 1：补齐事实表 + 四个指标模块

- [ ] 事实表：`fct_dreaming_run` / `fct_dreaming_memory_item` / `fct_proactive_message`（含回复晚到归因）/ `fct_onboarding_journey`
- [ ] `daily_users.py`（读 `fct_message`/`dim_account`）+ 基础报告渲染
- [ ] `dreaming.py`（数据最完整，先跑通流程）
- [ ] `onboarding.py`（漏斗框架，人设部分留占位）
- [ ] `proactive.py`（直接读 `fct_proactive_message` 已归因的回复列）

### Phase 2：报告与调度

- [ ] `run_etl.py` 入口（增量刷新基础表）+ `run_daily.py`（基于基础表算 agg_ + 出报告，支持 `--date`/`--yesterday`）
- [ ] `run_backfill.py` 补跑历史（先 ETL 重建 fct_/dim_，再逐日重算 agg_）
- [ ] Markdown 日报模板（`reporting/formatter.py`）
- [ ] 配置 cron：先 `run_etl.py` 再 `run_daily.py`，自动产出每日报告

### Phase 3：扩展（视需求）

- [ ] 周活跃（WAU）、7日留存
- [ ] 飞书文档推送（对接 lark-doc）
- [ ] Jupyter notebook 探索性分析
- [ ] 话题挖掘（热门候选话题，单独模块）

---

## 七、报告示例（目标输出格式）

```
# AI4ALL 每日运营报告 — 2026-06-07

## 用户增长
- 新注册：3 | DAU：7 | 消息量：42
- D1 留存（首聊 cohort）：2/3 = 66.7% | 贝壳消耗（ledger debit）：12,340 micros

## 主动消息
- 总发送：5 条（companion_followup: 3 / user_reminder: 2）
- 覆盖账号：4 / DAU 7 = 57.1%
- 回复率（24h，分类别）：companion_followup 2/3、user_reminder 1/2
- 发送高峰：20-22 时

## Dreaming
- 运行：11 次 / 成功：11 / 失败：0
- 覆盖账号：4 | 平均时长：8.3s
- 记忆产出：23 条（insert: 15 / update: 8）| Token: 48,200 in / 12,100 out

## Onboarding
- 存量状态：complete 5 / pending 11 / step1_sent 2 / step2_sent 1
- 完成率：5 / (5+0) = 100% | 超时率：0%
- 人设分布：数据待积累

---
*由 nearline/run_daily.py 自动生成*
```
