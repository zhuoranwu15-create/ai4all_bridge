# Nearline Analytics 建设规划（工程实施方案）

> 状态：规划草稿 | 日期：2026-06-08

> **文档分工**：本文是**工程实施方案**——物理存储（facts/marts 双库）、目录结构、ETL 机制、基础表 schema、实施 phases。
> **指标口径、数据不变量、数据质量检查**以 [`analytics_foundation_design.md`](../../architecture/shared/platform/analytics_foundation_design.md) 为唯一事实源。两者冲突时以 foundation 为准。

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

> 事实源为代码 `app/products/zhaoxi/proactive/policy.py:OutboundCategory` + `SOURCE_CATEGORY_MAP`（共 8 个枚举值）。归一逻辑：`product_category` 已显式写入则直接用，否则按 `source` 查表，未命中落 `legacy_proactive`。`fct_proactive_message.category` 直接沿用该列，不再二次归一。

| category 值 | 含义 | source 来源 | 当前数据 |
|---|---|---|---|
| `user_reminder` | 用户自设提醒 | reminder, reminder_change_confirmation | 有 |
| `companion_followup` | 陪伴式主动关心 | commitment, account_check, heartbeat | 有 |
| `reactivation_topic_followup` | 召回·话题跟进（**当前主力**） | reactivation 路径直写 | 有（最多） |
| `reactivation_content_invitation` | 召回·话题邀约 | reactivation 路径直写 | 有 |
| `content_invitation` | 话题邀约（旧路径，已并入 reactivation） | content_invitation | 有 |
| `content_invitation_response` | 话题邀约响应 | content_invitation_titles/feedback | 暂无 |
| `task_result` | 异步任务结果 | async_task_result | 暂无 |
| `legacy_proactive` | 历史兜底类 | 未命中来源 | 暂无 |

> ⚠️ 截至 2026-06-08，线上 44 条 outbound 中 `reactivation_*` 占 35 条——**召回类是当前主力**，域2 报告必须独立展示这两类，不能并入旧 `content_invitation`。

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

> **口径以 [`analytics_foundation_design.md`](../../architecture/shared/platform/analytics_foundation_design.md) 为唯一事实源**（指标定义、数据不变量、数据质量检查）。本节只描述**工程落地**：每个指标读哪张 `fct_`/`dim_` 表、写哪张 `agg_` 表。两文档口径冲突时，一律以 foundation 为准。
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

> **已校验现状（2026-06-08，对照 `data/ai4all.sqlite3` + 代码）**：
> - 操作库列名与本文 `fct_`/`dim_` 假设一致（`messages.role/direction`、`outbound_messages.product_category/sent_at/policy_reason`、`dreaming_runs.token_input/output/status`、`entitlement_ledger.entry_type`）。
> - `created_at` 为无时区的北京本地时间 → `DATE(created_at)` 即北京自然日，无需转换。
> - 数据量极小：50 账号（0 debug）、597 条用户入站、44 条 outbound、71 次 dreaming（全 succeeded）。→ 留存/分时段初期普遍 < 阈值，按 foundation §8 标注"仅观察"，**先跑通管线而非追指标**。
> - 备份接入点 = `scripts/backup_data.py`（当前单库 `_backup_sqlite` + `_COUNTED_TABLES` + manifest artifacts）。
> - `.gitignore` 当前仅含 `ai4all.db`，需补 nearline 产物。
>
> 执行顺序原则：**先打通 source→fct→agg→report 端到端最薄一条线（dim_date + dim_account + fct_message → daily_users → Markdown），再横向补齐其余事实表/域**。

### Phase 0：基建骨架 + 端到端最薄一条线 ✅ 已完成（2026-06-08）

- [x] 搭 `nearline/` 目录骨架（按 §3.1），含 `nearline/requirements.txt`（pandas、jinja2；Phase 0 仅用标准库）
- [x] `source_db.py`：以 `mode=ro` URI 只读连接 `data/ai4all.sqlite3`（默认仓库根路径，可经 `--source-db`/`NEARLINE_SOURCE_DB` 覆盖，禁止写）
- [x] `facts_db.py` / `marts_db.py`：连接 + 建表（执行 `facts_schema.sql` / `marts_schema.sql`，幂等 `CREATE TABLE IF NOT EXISTS`）
- [x] `facts_schema.sql`：`dim_date`、`dim_account`、`fct_message`、`etl_watermark`（其余 fct_ 放 Phase 1）
- [x] `marts_schema.sql`：`agg_daily_users`（其余 agg_ 放 Phase 1）
- [x] ETL：`dim_date`（区间生成）、`dim_account`（UPSERT，含 `is_debug`/`registered_date`/首聊日）、`fct_message`（watermark 按 `messages.id` 增量；首条标记对源自相关，重跑稳定）
- [x] `metrics/daily_users.py`：读 `fct_message`/`dim_account` → 写 `agg_daily_users`（DAU、入站数、新增；留存样本不足标"仅观察"，次日未到标"窗口未闭合"）
- [x] `reporting/formatter.py` + `run_daily.py --date/--yesterday` + `run_etl.py`：渲染 §七 格式 Markdown"用户增长"段，端到端 source→report 跑通
- [x] 工程接入：`.gitignore` 补 `nearline/data/*.sqlite3` + `reports/`；`facts.sqlite3` 纳入 `scripts/backup_data.py`（双库快照 + 完整性校验 + manifest），**`marts.sqlite3` 不入备份**
- 校验：facts 行数=源 1237；first_ever=40=有入站账号数；逐日 DAU 与源一致；06-04 留存 8/12 与源交叉一致；ETL 重跑增量 0；备份 dry-run 通过

### Phase 1：补齐事实表 + 其余三个指标域 ✅ 已完成（2026-06-08）

- [x] 事实表（watermark 增量）：`fct_dreaming_run`、`fct_dreaming_memory_item`、`fct_proactive_message`、`fct_onboarding_journey`（累积快照，COALESCE 保留首次捕捉里程碑）
- [x] `fct_proactive_message` **回复晚到归因**（§3.6）：新行 sent 落 `pending`/非 sent 落 `not_applicable`；每次 ETL 对窗口已闭合行回填 `replied/reply_message_id/reply_latency_sec` 置 `resolved`；窗口默认 24h 存入 `reply_window_hours`，被同账号下一条 sent 截断；`category` 直接沿用 8 类枚举（§二），`reactivation_*` 独立保留。新增 `created_date` 列支撑 blocked 按日归属
- [x] `metrics/dreaming.py`：`partial` 独立列、token NULL 安全、`skip_reason` 拆解、heartbeat 健康快照
- [x] `metrics/proactive.py`：读已归因列，分类别（含 `reactivation_*`）出回复率（分母仅 `resolved`）/覆盖账号/P50 延迟/分时段
- [x] `metrics/onboarding.py`：第②段微信首聊存量分布 + 注册 cohort 完成；人设占位"数据待积累"
- [x] `marts_schema.sql` 补 `agg_daily_dreaming`/`agg_daily_proactive`/`agg_hourly_proactive`/`agg_onboarding_funnel_daily`；`formatter.py`/`run_daily.py` 渲染四域完整日报
- 校验（2026-06-05）：proactive 分类 ci4/rci1/rtf4 与源一致；回复归因 sent 10:01:32→msg#581(18:18:39)=29827s 与独立查询一致；dreaming 14 runs、记忆 24/2 与源一致；归因状态 39 resolved/4 pending/1 n/a；ETL 重跑全增量 0；备份含 facts.sqlite3（integrity ok）
- 已知限制：coverage 仅出覆盖账号分子（eligible 分母待 `proactive_account_state`/`channel_bindings` 接入）；onboarding 中间里程碑历史值空（待 `onboarding_events`）；dreaming token 当前源全 NULL

### Phase 2：编排、回溯与质量门禁 ✅ 已完成（2026-06-08；2026-06-16 接 cron + 日报飞书群推送）

- [x] `run_etl.py`：一次性增量刷新全部 `dim_`/`fct_`（含归因回填）
- [x] `run_daily.py` 支持 `--yesterday`/`--date`/`--no-write`/`--skip-quality`；ETL → 质量门禁 → 四域 agg → 报告 → `run_state.json`（供监控检测陈旧）
- [x] `run_backfill.py --from --to [--write-reports]`：先重建 fct_/dim_，再逐日重算 agg_（软质量只打印不阻断；明确回溯填不回历史里程碑/未捕捉归因）
- [x] **数据质量门禁**（foundation §7）`analytics/quality.py`：5 项硬检查（账号外键 / direction-role / sent 有 sent_at / 回复外键 / memory-run 外键）+ 2 项软检查（DAU 跨层对账 / daily_usage 差异）。硬失败→飞书告警（复用 `app.alerting`）+ 非零退出 + 不出报告；软失败→报告"数据质量提示"段
  - H4「回复外键」（2026-06-16 增量）：facts 为 append-only 基线，账号被解绑清空（`wipe_account_data`）后历史归因到的 `reply_message_id` 会从源 messages 消失，属预期。改为只对"账号源库仍有存活消息"的孤儿硬失败，已清空账号豁免并在 detail 注明，避免误阻断 `run_daily`。
- [x] 调度模板：`nearline/deploy/ai4all-nearline.{service,timer}`（systemd，推荐）+ `crontab.example`；独立进程不挂 FastAPI/dreaming_scheduler
- [x] **每日日报飞书群推送**（2026-06-16 增量）：`run_daily` 成功出报告后，把四域核心数字的纯文本摘要（`formatter.render_feishu_summary`）推送到运营飞书群（`FEISHU_WEBSITE_WEBHOOK_URL`，经 `alerting.send_report`，区别于运维群告警 `FEISHU_ALERT_WEBHOOK_URL`）；`--no-feishu` 可关闭，`run_state.json` 记录 `feishu_pushed`，best-effort 失败不影响日报产出
- [x] **产品×渠道日报作用域**（2026-07-28 增量）：服务端通过
  `nearline/reporting/scope.py` 注册可信 `app_id + channel` 组合；当前定时任务生成
  `zhaoxi + openclaw-weixin`（朝夕相伴微信渠道），`zhaoxi + native` 已注册但未进入定时推送。
  增长指标按真人 owner 去重（产品 membership 加入日、真人 DAU、真人首聊 D1），同时保留
  活跃 AI 账号数；消息/主动消息使用事件级 channel，Dreaming 因缺少事件渠道暂按账号归属渠道。
  scoped marts 以 `(date, app_id, channel)` 为主键，避免后续 App/第二产品报告互相覆盖。
- [x] 已接 cron：每日定时跑 `--yesterday`（`crontab.example` 提供模板；cron 不读 `.env`，需在 crontab 内显式提供 `FEISHU_WEBSITE_WEBHOOK_URL`）
- 校验：7 检查全过（DAU 对账 facts=源=14）；注入孤儿行→硬检查 FAIL 且软对账独立命中→报告阻断 exit 1；清理后恢复 exit 0；backfill 06-04..06-06 逐日重算正常；`run_state.json` 落盘

### Phase 3：扩展（视需求）

- [ ] 周活跃（WAU）、7日留存（数据积累后启用）
- [ ] `fct_cost_event` + 贝壳消耗趋势
- [ ] 飞书**文档**推送（对接 lark-doc，富文本归档；区别于 Phase 2 已上线的飞书群纯文本摘要推送）
- [ ] Jupyter notebook 探索性分析
- [ ] 话题挖掘（热门候选话题，单独模块）
- [ ] 推动 app 侧补 `onboarding_events`/`analytics_events` 结构化埋点（foundation §4.4），解锁人设分布与精确漏斗耗时

---

## 七、当前状态与验收建议（截至 2026-06-08）

### 7.1 整体完成状态

| 模块 | 状态 | 备注 |
|---|---|---|
| Phase 0：基建骨架 + 用户增长域 | ✅ 完成 | 端到端 source→fct→agg→report 跑通 |
| Phase 1：四域事实表 + 三个指标域 | ✅ 完成 | dreaming / proactive / onboarding 报告可出 |
| Phase 2：run_daily / backfill / 质量门禁 | ✅ 完成 | 已接 cron（每日 `--yesterday`）；报告摘要推送运营飞书群；H4 豁免已清空账号 |
| A1：dreaming token 写库 | ✅ 完成 | `app/llm.py` + `app/dreaming.py`；新 run 起才有数据 |
| A2：onboarding 状态变更埋点 | ✅ 完成（半闭合） | `analytics_events` 表已建；nearline 消费侧未接 |

**A2 半闭合说明**：`analytics_events` 已落库（`onboarding_state_changed` + `persona_selected`），但 nearline 尚无消费者（无 `fct_onboarding_event`，无人设分布指标）。现有 `fct_onboarding_journey` 仍靠状态快照，不靠事件流。

### 7.2 重启方式

服务由 systemd 管理。代码更新后执行：

```bash
# 需要 sudo；--skip-nginx 适用于本地不走反代的环境
sudo bash scripts/restart_runtime.sh --skip-nginx

# 或 Claude Code 内联执行：
! sudo bash scripts/restart_runtime.sh --skip-nginx
```

脚本自动完成：`systemctl restart backend + scheduler` → 等 3s → `GET /health/ready` → `systemctl is-active` × 4 → `monitor_health.py --dry-run`。

**当前状态（2026-06-08 17:33）**：`/health/ready` → ok，`analytics_events` 表已存在，`proactive_scheduler` 心跳正常（17:32），`dreaming_scheduler` 心跳 idle 正常（今日 04:01 成功）。

### 7.3 验收清单

#### 域1：用户增长

| 检查点 | 方法 |
|---|---|
| ETL 跑通，facts 行数与源一致 | `python nearline/run_etl.py`（无错退出） |
| 日报生成 | `python nearline/run_daily.py --date 2026-06-07`，看 `nearline/data/reports/` |
| DAU / 新增 / 入站量有值 | 核对报告与 `SELECT COUNT(*) FROM messages WHERE direction='inbound'` |
| D1/D7 留存标注"仅观察"（样本 < 10） | 报告中出现 `observe_only` 或 `N/A` 字样 |

#### 域2：主动消息

| 检查点 | 方法 |
|---|---|
| `fct_proactive_message` 行数与 `outbound_messages` 一致 | `SELECT COUNT(*) FROM fct_proactive_message` vs 源 |
| resolved/pending/n_a 分布合理 | `SELECT resolution_status, COUNT(*) FROM fct_proactive_message GROUP BY 1` |
| `reactivation_*` 类别独立展示 | 报告含 `reactivation_topic_followup` / `reactivation_content_invitation` 分行 |
| 回复归因回填在窗口闭合后有效 | 查 `replied=1` 的行，核对 `reply_latency_sec` 值 |

#### 域3：Dreaming

| 检查点 | 方法 |
|---|---|
| 历史 runs 全 NULL token | `SELECT token_input, token_output FROM dreaming_runs LIMIT 5` — 历史行仍 NULL，正常 |
| **新 run（重启后触发）有 token** | 等待当晚 dreaming 跑后检查：`SELECT token_input, token_output FROM dreaming_runs ORDER BY id DESC LIMIT 3` |
| run 状态 / 记忆条目与源一致 | 报告中 `succeeded` 数 = `SELECT COUNT(*) FROM dreaming_runs WHERE status='succeeded'` |

> ⚠️ token 数据需等服务重启后的**下一次 dreaming 实际执行**才能验证（每日凌晨 4 时）。

#### 域4：Onboarding

| 检查点 | 方法 |
|---|---|
| `fct_onboarding_journey` 行数 = 账号数 | `SELECT COUNT(*) FROM fct_onboarding_journey` vs `accounts` |
| 状态分布与 `accounts.onboarding_state` 一致 | 报告中各 state 数量 vs `SELECT onboarding_state, COUNT(*) FROM accounts GROUP BY 1` |
| 人设分布标注"数据待积累" | 报告中出现占位说明 |
| **A2 埋点验证**（需重启后实际操作） | 用 `send_mock_turn.py` 触发新用户 onboarding；查 `SELECT * FROM analytics_events LIMIT 10` |

#### 质量门禁

```bash
# 应全部通过，exit 0（不带 --skip-quality 即默认执行质量门禁）
python nearline/run_daily.py --date 2026-06-07
# 或单独跑质量检查
python -c "
import sys; sys.path.insert(0, '.')
from nearline.analytics.quality import run_checks, summarize
r = run_checks('2026-06-07')
print(summarize(r))
"
```

期望：5 项硬检查全 PASS，2 项软检查可 WARN（跨层 DAU 差异在早期数据量小时容易触发）。

### 7.4 已知遗留与后续建议

| 项目 | 影响 | 优先级 |
|---|---|---|
| A2 nearline 消费侧未接（`fct_onboarding_event` + 人设分布） | `style_distribution` 永远显示"数据待积累" | 中（数据积累后再接） |
| coverage 分母缺 eligible 账号数 | 覆盖率指标只出分子，报告中应标注 | 低（待 `proactive_account_state`/`channel_bindings` 接入） |
| WAU / 7日留存 | 需数据积累（当前数量级不足） | 低（Phase 3） |
| `fct_cost_event` + 贝壳消耗趋势 | 成本视角缺失 | 中（Phase 3） |
| dreaming token 历史全 NULL | 历史 Dreaming 报告不含 token 数 | 不可追溯（只影响历史，新数据正常） |

---

## 八、报告示例（目标输出格式）

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
