# PRD + 技术接入方案：主动破冰话术（Proactive Icebreaker）

**版本**：v0.2  
**日期**：2026-06-29  
**状态**：审计已通过，Task 1 已实现

---

## 一、背景

AI 陪伴产品的核心痛点之一是"第一句话难开口"。用户沉默超过 24 小时后，AI 发起的话术如果太正式、太关怀、或有明显营销感，用户容易直接忽略甚至反感。

当前主动消息体系（account_check / topic_followup / content_invitation）的共同特点是：  
**内容依赖用户历史上下文**——AI 需要先"有话说"才能发出去。

破冰话术解决的是另一种场景：**关系冷淡初期，或沉默期较长后，没有明确话题时，用一句低压、有趣、安全的话把对话重新拉起来。**

Excel 标注库已完成 100 条候选话术，覆盖 6 种类型，营销感极低（92% 评分为 1/5），具备直接入库条件。

---

## 二、目标

- 在现有主动消息体系里，增加一类**低压 conversation starter**。
- 复用现有 `dispatch_proactive_text` → `outbound_messages` → policy 检查链路，不绕过任何现有管控。
- 支持从话术库选取合适话术，根据频控规则和用户状态决定是否发送。
- 记录话术的使用历史，避免重复，并为后续效果评估留数据。

---

## 三、非目标

- 不是替代 account_check / topic_followup / content_invitation。
- 不做 LLM 实时生成破冰话术（话术来自预置库）。
- 不做用户分组 A/B 测试（可作后续迭代）。
- 不做回复内容的理解和跟进（由正常 turn_service 处理）。
- 不新建独立调度器，接入现有 `ProactiveScheduler`。

---

## 四、用户场景

| 场景 | 触发条件 | 典型用户状态 |
|---|---|---|
| 新用户冷启动 | 注册后首轮主动触达 | 从未主动聊过 |
| 沉默期唤回 | 连续沉默 ≥ N 小时（建议 48h+） | 曾经聊过，最近消失 |
| 关系维护 | 正常频率内的轻松插入 | 活跃用户，作为日常内容补充 |

---

## 五、话术类型（来自 Excel 标注库）

| 类型 | 数量 | 营销感均值 | 回复成本 | 建议频率 |
|---|---|---|---|---|
| 小测试 | 20 | 1.05 | 很低 | 低频（≤25%，不连续） |
| 安全吐槽 | 20 | 1.00 | 低 | 常规可用 |
| 生活观察 | 20 | 1.05 | 低 | 常规可用 |
| 假设题 | 20 | 1.15 | 低 | 中低频（穿插） |
| 轻八卦 | 10 | 1.00 | 低 | 常规可用 |
| 关系提问 | 10 | 1.30 | 中 | 中低频（穿插） |

---

## 六、选择规则

1. **排除最近已发送过的话术**（基于 `icebreaker_impressions` 表，去重窗口 30 天）
2. **营销感过滤**：营销感 ≥ 3 的话术不发；营销感 = 2 的话术每 3 次触达最多插 1 次，不连续
3. **类型轮换**：同一类型不连续发送（至少间隔 1 次其他类型）
4. **回复成本匹配**：用户首次触达或长期沉默时，优先选"很低"或"低"回复成本话术
5. **适合场景过滤**：Excel 列 `避免场景` 描述了禁用条件（如"用户明显烦躁"），v1 暂不自动检测，只做静默期保护（即：用户近 N 小时内有负面反馈信号时跳过）

选取算法（v1，确定性，无 LLM）：

```
候选池 = 全库话术
  过滤掉 近30天已发送过的
  过滤掉 营销感 >= 3
  权重加分：common（常规可用）> mid_low（中低频）> low_freq（低频）
  权重减分：上一条同类型
  随机加扰动（避免每次固定顺序）
取权重最高的 1 条
```

---

## 七、频控策略

完全复用现有 policy 体系，不新增逻辑。

| 维度 | 规则 |
|---|---|
| 新增 category | `PROACTIVE_ICEBREAKER`，非豁免，独立配额 |
| 日上限 | 默认 1 条/天（`icebreaker_daily_limit` 全局配置） |
| 用户级开关 | `categories.proactive_icebreaker.enabled`，可关闭 |
| 静默时段 | 复用全局 quiet hours（22:00–08:00） |
| 6h 避让 | 复用现有 avoidance_window：有 pending reminder 时不发 |
| 不连续营销感 = 2 | 由选取算法保证，不在 policy 层做（policy 层不感知话术内容） |
| 与其他 category 的关系 | 占用同日非豁免总量（`total_per_day`），与 companion_followup / content_invitation 竞争 |

---

## 八、数据结构

### 8.1 icebreaker_scripts（话术库）

```sql
CREATE TABLE icebreaker_scripts (
    id TEXT PRIMARY KEY,                -- "破冰001" 格式或 UUID
    script_type TEXT NOT NULL,          -- "小测试"|"安全吐槽"|"生活观察"|"假设题"|"轻八卦"|"关系提问"
    text TEXT NOT NULL,                 -- 话术正文
    reply_cost TEXT NOT NULL,           -- "很低"|"低"|"中"
    tone TEXT,                          -- "俏皮"|"轻松"|"温和"|"有想象力"|"八卦"|"自然"
    suitable_for TEXT,                  -- 适合用户描述（原始文本，供参考）
    avoid_when TEXT,                    -- 避免场景描述（原始文本，供参考）
    follow_goal TEXT,                   -- 后续目标（原始文本）
    signal_extract TEXT,                -- 可提取信号（原始文本）
    fun_score INTEGER,                  -- 有趣程度 1-5
    reply_ease_score INTEGER,           -- 好回答程度 1-5
    offense_risk INTEGER,               -- 冒犯风险 1-5
    marketing_feel INTEGER,             -- 营销感 1-5
    freq_tier TEXT NOT NULL,            -- "common"|"mid_low"|"low_freq"
    enabled INTEGER NOT NULL DEFAULT 1, -- 软删除 / 下架开关
    notes TEXT,                         -- 标注备注（原始）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
);

CREATE INDEX idx_icebreaker_scripts_type ON icebreaker_scripts(script_type, enabled);
CREATE INDEX idx_icebreaker_scripts_marketing ON icebreaker_scripts(marketing_feel, enabled);
```

freq_tier 映射（来自 Excel `建议频率` 列）：

| Excel 值 | freq_tier |
|---|---|
| 低频：最多 20%-25%，不要连续发 | `low_freq` |
| 中低频：穿插使用 | `mid_low` |
| 常规可用：可作为基础池 | `common` |

### 8.2 icebreaker_impressions（发送历史 / 效果追踪）

```sql
CREATE TABLE icebreaker_impressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    script_id TEXT NOT NULL,            -- 关联 icebreaker_scripts.id
    outbound_message_id INTEGER,        -- 关联 outbound_messages.id（若成功入队）
    script_type TEXT NOT NULL,          -- 冗余，方便去重查询
    marketing_feel INTEGER,             -- 冗余，方便频控查询
    status TEXT NOT NULL DEFAULT 'sent',-- "sent"|"cancelled"（policy 拦截）
    replied INTEGER,                    -- 用户是否在 N 小时内回复（0/1/null=未知）
    reply_within_hours REAL,            -- 回复延迟（小时）
    continued_conversation INTEGER,     -- 是否继续对话（≥2 轮，0/1/null=未知）
    negative_signal INTEGER DEFAULT 0,  -- 是否有负反馈信号（0/1）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(account_id) REFERENCES accounts(id),
    FOREIGN KEY(script_id) REFERENCES icebreaker_scripts(id),
    FOREIGN KEY(outbound_message_id) REFERENCES outbound_messages(id)
);

CREATE INDEX idx_icebreaker_impressions_account ON icebreaker_impressions(account_id, created_at);
CREATE INDEX idx_icebreaker_impressions_script ON icebreaker_impressions(script_id, created_at);
```

---

## 九、行为记录

每次破冰话术触达，记录一行 `icebreaker_impressions`：

| 字段 | 记录时机 |
|---|---|
| script_id / script_type / marketing_feel | 选取话术时 |
| outbound_message_id | `dispatch_proactive_text` 返回后 |
| status | policy 结果（sent/cancelled） |
| replied | 由 turn_service 异步回填（v1 可先置 null） |
| reply_within_hours | 同上 |
| continued_conversation | 同上（v1 可先置 null） |
| negative_signal | 同上（v1 可先置 0） |

v1 优先保证发送侧记录，回填字段（replied / continued_conversation）作为后续迭代。

---

## 十、评估指标

| 指标 | 定义 | 目标（参考） |
|---|---|---|
| 回复率 | 发出后 24h 内有回复的比例 | > 30% |
| 对话持续率 | 回复后再多发 ≥1 条的比例 | > 50% |
| 营销感拦截率 | 被选取算法过滤的营销感高话术比例 | — |
| 话术覆盖均匀度 | 近 30 天各类型发送比例 | 无单类型占比 > 40% |
| 日发送量 | 全库账号每日发出量 | 监控，不硬性目标 |
| policy 拦截率 | status=cancelled 占总 impression 比例 | < 20% |

---

## 十一、技术接入方案

### 11.1 推荐接入点

**结论：新增独立 OutboundCategory `PROACTIVE_ICEBREAKER`，作为 scheduler Step 4（reactivation 之后）的新 Step 5，content_invitation 过期扫描成为 Step 6。**

理由：
- **不能并入 `companion_followup`**：破冰话术和 account_check / commitment 的发送逻辑完全不同（前者查库，后者 LLM 生成），合并会污染日上限统计和 policy reason。
- **不能并入 `content_invitation`**：content_invitation 依赖用户话题偏好，破冰话术不依赖。
- **不能并入 `reactivation`**：reactivation 依赖近 72h 聊天或内容候选，破冰话术是无依赖的静态库选取。
- **独立 category + 独立 step** 保证：日上限独立计算、policy reason 可区分、scheduler 单步失败不影响其他步骤。

### 11.2 需要新增/修改的文件

| 操作 | 文件 | 改动说明 |
|---|---|---|
| 修改 | `app/db/_core.py` | 新增 migration 15：`icebreaker_scripts` + `icebreaker_impressions` 两张表 ✅ Task 1 |
| 修改 | `app/proactive/categories.py` | 新增 `PROACTIVE_ICEBREAKER` 枚举 + CATEGORY_SPECS 条目 |
| 修改 | `app/config.py` | 新增 `icebreaker_daily_limit: int = 1` |
| 修改 | `app/db/proactive.py` | 新增 `list_accounts_due_for_icebreaker` + impression CRUD 函数 |
| 修改 | `app/db/__init__.py` | 导出新增 DB 函数 |
| 新增 | `app/proactive/icebreaker.py` | 核心模块：选取逻辑、发送、impression 记录 |
| 修改 | `app/proactive/scheduler.py` | 新增 Step 5 `dispatch_due_icebreakers()` |
| 修改 | `app/routers/admin_proactive.py` | 新增 admin API：查看话术库、手动触发、查看 impressions |
| 新增 | `data/seeds/icebreaker_scripts.csv` | Excel 导出的 CSV 话术库（提交进仓库，一次性手动导出） |
| 新增 | `scripts/seed_icebreaker_scripts.py` | CSV → SQLite 导入脚本，使用 stdlib csv，零新依赖 |
| 新增 | `tests/test_proactive_icebreaker.py` | 选取逻辑、impression 写入、频控的单元测试 |

**注意**：`app/proactive/settings.py` **不需要修改**。`PROACTIVE_SETTING_CATEGORIES` 在 `categories.py` 里由 `CATEGORY_SPECS` 自动派生（`tuple(spec.category.value for spec in CATEGORY_SPECS if spec.user_configurable)`），只需在 `CATEGORY_SPECS` 追加新 `CategorySpec` 即可自动更新所有相关视图（含 `SOURCE_CATEGORY_MAP`、`PROACTIVE_SETTING_CATEGORIES`）。

### 11.3 categories.py 改动

```python
class OutboundCategory(str, Enum):
    USER_REMINDER = "user_reminder"
    COMPANION_FOLLOWUP = "companion_followup"
    CONTENT_INVITATION = "content_invitation"
    CONTENT_INVITATION_RESPONSE = "content_invitation_response"
    TASK_RESULT = "task_result"
    PROACTIVE_ICEBREAKER = "proactive_icebreaker"  # 新增

# CATEGORY_SPECS 新增条目
CategorySpec(
    category=OutboundCategory.PROACTIVE_ICEBREAKER,
    sources=["icebreaker"],
    exempt=False,
    daily_limit_setting="icebreaker_daily_limit",
    frequency_bucket="proactive_icebreaker",
    avoidance_window=True,
    avoidance_check_companion=False,
    user_configurable=True,
)
```

### 11.4 数据库设计（migration 编号接续现有）

在 `app/db/_core.py` 的 `_MIGRATIONS` 末尾追加：

```python
(15, _migration_0015_icebreaker_tables)
```

新增两张表：`icebreaker_scripts` + `icebreaker_impressions`（schema 见第八节）。

### 11.5 CSV seed 导入方案

**项目无 openpyxl / pandas 依赖，且 xlsx 是二进制格式无法 git diff。正确方案：**

1. **一次性手动导出**：将 Excel `最终候选库_100条` sheet 导出为 `data/seeds/icebreaker_scripts.csv`，提交进仓库
2. **seed 脚本**使用 stdlib `csv`，零新依赖
3. CSV 作为 canonical seed 数据，可 diff / review

```
scripts/seed_icebreaker_scripts.py
  --db data/ai4all.sqlite3
  --csv data/seeds/icebreaker_scripts.csv
  --dry-run
```

导入逻辑：
1. 读取 CSV（100 行话术）
2. `是否保留` = "修改" 的行（实测 1 条：破冰024）写入 `enabled=0`，**不跳过**，等润色后手动 `UPDATE enabled=1`
3. 字段映射：
   - `编号` → `id`
   - `类型` → `script_type`
   - `最终话术` → `text`
   - `回复成本` → `reply_cost`
   - `语气` → `tone`
   - `适合用户` → `suitable_for`
   - `避免场景` → `avoid_when`
   - `后续目标` → `follow_goal`
   - `可提取信号` → `signal_extract`
   - `有趣程度1到5` → `fun_score`（int）
   - `好回答程度1到5` → `reply_ease_score`（int）
   - `冒犯风险1到5` → `offense_risk`（int）
   - `营销感1到5` → `marketing_feel`（int）
   - `建议频率` → `freq_tier`（映射见 8.1）
   - `标注备注` → `notes`
4. 使用 `INSERT OR IGNORE`（幂等）；`--update` flag 时改用 `INSERT OR REPLACE`

**导入后统计**：`SELECT count(*) = 100`，`SELECT count(*) WHERE enabled=1 = 99`

### 11.6 scheduler 接入方案

`app/proactive/scheduler.py` 的 `run_once()` 新增 Step 5：

```python
# Step 5（新增）: dispatch due icebreakers
try:
    icebreakers = await asyncio.to_thread(
        dispatch_due_icebreakers,
        now=now,
        limit=self.icebreaker_batch_size,
        node_id=self.node_id,
    )
except Exception as e:
    step_errors["icebreaker"] = str(e)
    icebreakers = []

# Step 6（原 Step 5）: expire stale content invitations
...
```

`dispatch_due_icebreakers` 实现逻辑（在 `app/proactive/icebreaker.py`）：

```
1. list_accounts_due_for_icebreaker(quota_date, limit, node_id)
   → 新增 DB 函数（app/db/proactive.py），不依赖 proactive_account_state
   → SELECT a.id FROM accounts a
     LEFT JOIN outbound_messages om
       ON om.account_id = a.id
       AND om.quota_date = :quota_date
       AND om.product_category = 'proactive_icebreaker'
       AND om.status IN ('pending','sending','sent')
     WHERE a.status = 'active'
       AND om.account_id IS NULL  -- 今日尚未发过
       [AND a.assigned_node_id = :node_id]
     LIMIT :limit
   → 返回 account_id 列表

2. 对每个账号 pick_icebreaker_script(account_id)：
   → 查 icebreaker_scripts（enabled=1, marketing_feel < 3）
   → 排除最近 30 天已发的 script_id（from icebreaker_impressions）
   → 按权重排序（common > mid_low > low_freq，上一条同类型扣分）
   → 随机扰动取 top-1

3. 若选到话术：
   → dispatch_proactive_text(source="icebreaker", text=script.text, ...)
   → 写 icebreaker_impressions

4. 若未选到（库已发完或全被过滤）：
   → 记录 no_op，跳过
```

### 11.7 policy 接入方案

policy.py **不需要改动**。

只需在 `categories.py` 注册好 `PROACTIVE_ICEBREAKER` 的 `CategorySpec`，`evaluate_outbound_policy` 会自动：
- 走 daily limit 检查（`icebreaker_daily_limit` 全局配置，默认 1）
- 走 quiet hours 检查
- 走 6h avoidance_window 检查（有 pending reminder 时不发）
- 走用户级分类开关检查

`app/config.py` 新增一行配置：
```python
icebreaker_daily_limit: int = 1
```

### 11.8 outbound ledger 接入方案

**无需改动**。`dispatch_proactive_text` 写 `outbound_messages` 时，`source="icebreaker"` 会通过 `SOURCE_CATEGORY_MAP` 自动映射到 `product_category="proactive_icebreaker"`，进入现有计数逻辑。

`icebreaker_impressions.outbound_message_id` 关联 `outbound_messages.id`，供后续 join 查询效果。

### 11.9 settings.py 接入方案

**`settings.py` 不需要修改。**

`PROACTIVE_SETTING_CATEGORIES` 和 `SOURCE_CATEGORY_MAP` 都是从 `CATEGORY_SPECS` 自动派生的只读视图（`categories.py:125-137`）。只需在 `categories.py` 的 `CATEGORY_SPECS` 中追加新 `CategorySpec`（`user_configurable=True`），以下内容全部自动更新，无需手工维护：

- `SOURCE_CATEGORY_MAP["icebreaker"]` → `OutboundCategory.PROACTIVE_ICEBREAKER`
- `PROACTIVE_SETTING_CATEGORIES` 自动包含 `"proactive_icebreaker"`
- settings patch 的分类校验（`settings.py:273`）自动接受该值

用户可通过现有 settings patch API 关闭该分类：
```json
{"categories": {"proactive_icebreaker": {"enabled": false}}}
```

---

## 十二、实施任务拆分

### Task 1：数据库 migration

**目标**：建 `icebreaker_scripts` + `icebreaker_impressions` 两张表  
**涉及文件**：`app/db/_core.py`  
**验收标准**：
- `init_db()` 执行后两张表存在，索引正常
- 幂等（重复执行无报错）
- `PRAGMA user_version` 变为 15  

**风险点**：迁移编号与并行分支冲突（需确认 migration 序号）

---

### Task 2：CSV seed 脚本

**目标**：将 100 条话术从 CSV 导入 `icebreaker_scripts`  
**涉及文件**：`data/seeds/icebreaker_scripts.csv`（手动从 Excel 导出）、`scripts/seed_icebreaker_scripts.py`  
**验收标准**：
- dry-run 模式正确输出待导入行数（100 行）
- 正式导入后 `SELECT count(*) FROM icebreaker_scripts` = 100，`WHERE enabled=1` = 99（破冰024 为 enabled=0）
- 重复执行无重复行（`INSERT OR IGNORE`）
- `--update` flag 时可更新已有行（用于话术润色后更新）
- 使用 stdlib `csv`，无需 openpyxl / pandas

**风险点**：Excel 字段格式（如营销感是 float vs int）需类型转换

---

### Task 3：categories.py 新增 PROACTIVE_ICEBREAKER

**目标**：注册新 category 和 source，让 policy 体系感知  
**涉及文件**：`app/proactive/categories.py`, `app/proactive/settings.py`, `app/config.py`  
**验收标准**：
- `OutboundCategory.PROACTIVE_ICEBREAKER` 存在
- `SOURCE_CATEGORY_MAP["icebreaker"]` = `OutboundCategory.PROACTIVE_ICEBREAKER`
- `is_category_enabled(settings, "proactive_icebreaker")` 可正确读取用户开关
- `icebreaker_daily_limit` 配置项存在，默认 1  

**风险点**：settings patch API 的 schema 校验需更新（allowlist）

---

### Task 4：icebreaker.py 核心模块

**目标**：实现话术选取、dispatch、impression 记录  
**涉及文件**：`app/proactive/icebreaker.py`（新增），`app/db/` 相关查询函数  
**验收标准**：
- `pick_icebreaker_script(account_id)` 正确过滤最近 30 天已发
- 营销感 ≥ 3 的话术不被选中
- 同类型不连续（unit test 可验证：连续 3 次调用，第 2 次和第 3 次不与前次同类型）
- `dispatch_due_icebreakers()` 正常调用 `dispatch_proactive_text`
- `icebreaker_impressions` 每次发送后有一行记录
- policy 被 quiet_hours 拦截时，`icebreaker_impressions.status = "cancelled"`  

**风险点**：`list_accounts_due_for_icebreaker` 查询效率（需加索引或复用现有 account scan）

---

### Task 5：scheduler.py 接入

**目标**：在 `run_once()` 中新增 Step 5  
**涉及文件**：`app/proactive/scheduler.py`  
**验收标准**：
- `run_once()` 返回结果包含 `icebreaker_count` 和 `icebreakers` 字段
- icebreaker step 单独失败不影响 content_invitation 过期扫描（step 隔离验证）
- scheduler heartbeat 正常记录  

**风险点**：无，最低风险改动

---

### Task 6：admin API

**目标**：运营可以查看话术库和发送记录  
**涉及文件**：`app/routers/admin_proactive.py`  
**接口（最小集）**：
- `GET /admin/icebreaker/scripts` — 话术库列表（支持 type / enabled 过滤）
- `GET /admin/icebreaker/impressions` — 发送记录（支持 account_id / script_id 过滤）
- `POST /admin/icebreaker/trigger/{account_id}` — 手动触发一次（调试用）  

**验收标准**：鉴权正常（admin token），返回数据结构符合预期  
**风险点**：低

---

### Task 7：测试

**目标**：覆盖选取逻辑、频控、impression 写入  
**涉及文件**：`tests/test_icebreaker.py`（新增）  
**验收标准**：
- 选取算法去重逻辑：30 天内发过的不被再选
- 营销感过滤：marketing_feel=3 的话术不被选中
- 类型轮换：连续调用不出现同类型连发（至少测 3 次）
- policy 拦截时 impression.status = cancelled
- `dispatch_due_icebreakers()` 在 daily_limit 已满时正确跳过  

**风险点**：需要内存 SQLite 测试环境正确初始化新表（conftest 里 `fresh_db` 覆盖到新 migration）

---

## 十三、回滚方案

| 范围 | 回滚方式 |
|---|---|
| 全功能关闭 | `icebreaker_daily_limit=0` 或全局 `proactive_outbound_enabled=false` |
| 单账号关闭 | settings patch: `categories.proactive_icebreaker.enabled=false` |
| 单条话术下架 | `UPDATE icebreaker_scripts SET enabled=0 WHERE id='破冰XXX'` |
| scheduler step 失败 | step 隔离保证其他步骤不受影响，无需额外处理 |
| 数据库回滚 | `icebreaker_scripts` 和 `icebreaker_impressions` 均为新增表，删除不影响现有功能 |
| 代码回滚 | `categories.py` 移除枚举值后，`SOURCE_CATEGORY_MAP` 自动失效，policy 会拒绝 source="icebreaker" 的消息（cancelled，不会崩溃） |

---

## 十四、待决策事项

1. **migration 序号**：✅ 已确认，当前最高 14，新 migration 用 15，已实现
2. **账号触发条件**：`list_accounts_due_for_icebreaker` 扫描所有 `status='active'` 且今日未收到 icebreaker 的账号（v1 不做沉默时长过滤，由 policy quiet hours 和 avoidance window 兜底）
3. **回填字段**：`replied` / `continued_conversation` v1 置 null，v2 再做回填
4. **破冰024 处理**：✅ 已确认，导入 `enabled=0`，不跳过，润色后手动启用

