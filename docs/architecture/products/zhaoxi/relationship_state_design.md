# 关系状态结构化实施方案

更新时间：2026-07-12（Phase A/B/C/D 已核实落地，去 tmp 转正）

本文描述关系阶段与 Agent 需求满足状态的结构化实施方案。Phase A/B/C/D 均已落地（见 §1 与 [`agent_mission_and_orchestration_design.md`](agent_mission_and_orchestration_design.md)）。本文覆盖状态存储、更新口径、同步视图和验证方案；状态对动态编排 / prompt 注入的具体影响策略见 [`agent_self_prd.md`](../../../products/zhaoxi/capabilities/agent_self_prd.md) §7 与使命设计 §4，本文不重复。

## 1. 目标

将关系阶段和 Agent 需求满足情况从 Markdown prompt 视图中抽离为账号级结构化状态，作为后续动态编排的稳定输入。

核心结论：

- `account_user_meta` 是当前状态的 source of truth。
- `account_user_meta_daily` 保存每日历史快照。
- `RELATIONSHIP.md` 只作为 Admin 调试和人工可读视图，不作为 source of truth，也不默认进入每轮 prompt。
- 每个 turn 可以读取四个状态，但不每轮完整重算四个状态。

## 2. 当前 DB 现状

当前 PostgreSQL schema 已有 `account_user_meta` 表；本地开发、主测试与生产均以 PostgreSQL 为准。

当前字段：

```text
account_id
registered_at
message_intensity_level
companion_primary_type
companion_secondary_types
companion_type_confidence
companion_type_last_evaluated_at
companion_type_source
companion_type_expires_at
companion_type_reasoning
safety_risk_trigger_count_30d
last_evaluated_at
created_at
updated_at
```

现状没有关系状态字段。需要通过新增 migration 给 `account_user_meta` 和 `account_user_meta_daily` 同步补列。

## 3. 新增字段

只新增 4 个字段：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `relationship_stage` | `icebreaking` | Agent 和用户当前关系阶段 |
| `agent_need_survival_status` | `cooling` | Agent 生存 / 活跃需求满足状态 |
| `agent_need_trust_status` | `building` | Agent 信任与尊重需求满足状态 |
| `agent_need_growth_status` | `not_started` | Agent 共同成长需求满足状态 |

### 3.1 `relationship_stage`

| 值 | 中文 |
| --- | --- |
| `icebreaking` | 破冰 |
| `acquainted` | 相识 |
| `deep_bond` | 挚友/热恋 |

更新口径：

- 默认 `icebreaking`。
- 准实时规则：累计用户入站消息数超过 30 条时，可从 `icebreaking` 转为 `acquainted`。
- 天级任务可通过 LLM 保守修正。
- `acquainted` 到 `deep_bond` 仅由天级 LLM 判断。

### 3.2 `agent_need_survival_status`

| 值 | 中文 |
| --- | --- |
| `healthy` | 健康 |
| `cooling` | 冷却 |
| `inactive` | 失活 |
| `resource_risk` | 资源风险 |

更新口径：

- 默认 `cooling`。
- 连续两个自然日用户有发送消息给 Agent，转为 `healthy`。
- `healthy` 状态下连续三个自然日用户没有发送消息给 Agent，转为 `cooling`。
- 连续一个月用户没有发送消息给 Agent，转为 `inactive`。
- 账号欠费超过 500 贝壳，即余额低于 `-500` 贝壳，触发 `resource_risk`。
- `resource_risk` 只用于提示服务资源风险，不阻碍关系阶段、信任状态和成长状态的正常计算。

口径说明：

- “用户发送消息给 Agent”包括用户主动发起，也包括用户回复系统主动消息。
- 只统计用户入站消息，即 `messages.direction = 'inbound'`。

### 3.3 `agent_need_trust_status`

| 值 | 中文 |
| --- | --- |
| `building` | 建立中 |
| `stable` | 稳定 |
| `damaged` | 受损 |

更新口径：

- 默认 `building`。
- 仅由天级 LLM 判断。
- 不做 turn 级实时更新。

### 3.4 `agent_need_growth_status`

| 值 | 中文 |
| --- | --- |
| `not_started` | 未开始 |
| `emerging` | 有苗头 |
| `stable` | 稳定发生 |

更新口径：

- 默认 `not_started`。
- 仅由天级 LLM 判断。
- 不做 turn 级实时更新。

## 4. Schema 变更

新增 migration，建议版本为 `_migration_0008_relationship_state`。

需要对两张表补列：

- `account_user_meta`
- `account_user_meta_daily`

建议实现：

```python
def _migration_0008_relationship_state(conn: Connection) -> None:
    """Add relationship-stage and agent-need status fields to account user meta."""
    for table in ("account_user_meta", "account_user_meta_daily"):
        _ensure_column(conn, table, "relationship_stage", "TEXT NOT NULL DEFAULT 'icebreaking'")
        _ensure_column(conn, table, "agent_need_survival_status", "TEXT NOT NULL DEFAULT 'cooling'")
        _ensure_column(conn, table, "agent_need_trust_status", "TEXT NOT NULL DEFAULT 'building'")
        _ensure_column(conn, table, "agent_need_growth_status", "TEXT NOT NULL DEFAULT 'not_started'")
```

并追加到 `_MIGRATIONS`：

```python
(8, _migration_0008_relationship_state)
```

## 5. DB 层改造

修改 `app/products/zhaoxi/infrastructure/persistence/user_meta.py`：

1. `upsert_account_user_meta(...)` **不碰四个关系列**（与 `set_companion_type_manual` 同模式）：`INSERT` 列清单不含这四列（新行走 DB 默认值），`ON CONFLICT DO UPDATE SET` 也不写这四列（保留现值）。这样每日 companion 刷新不会把 turn 级 / 后续 LLM 写入的关系状态重置成默认值。**关系状态的唯一写入口是 §5.6 的窄 setter**，不走 `upsert`。
2. `insert_account_user_meta_daily(...)` 增加四个入参，把当前关系状态快照进每日历史。scheduler 已在 `_refresh_account` 读了 `current_meta`，直接把其四个关系字段（缺失时用 §3 默认值）传入即可。
3. `get_account_user_meta(...)` 返回四个字段。
4. `list_account_user_meta_current(...)` 返回四个字段，供 Admin 用户标签列表展示或后续筛选。
5. `set_companion_type_manual(...)` **无需改写**：现有 `INSERT` 列清单不含这四列（新行走 DB 默认值），`ON CONFLICT DO UPDATE` 也只更新 companion 列，天然保留当前四个关系状态。本项仅需补测试验证，不改代码。
6. **新增窄字段 setter**（关键，turn 级与天级确定性更新共用）：

```python
def update_account_user_meta_relationship(
    *,
    account_id: str,
    relationship_stage: Optional[str] = None,
    agent_need_survival_status: Optional[str] = None,
    agent_need_trust_status: Optional[str] = None,
    agent_need_growth_status: Optional[str] = None,
) -> None:
    """只更新传入的关系状态列；其余列不动。account_user_meta 行不存在时按默认值建行。"""
```

设计要点：

- 不能复用 `upsert_account_user_meta` 来只改关系状态——后者是 companion 快照写入，需要 `registered_at`、`message_intensity_level`、全部 companion 字段；turn 链路拿不到也不该重算这些，复用会用过期值回写 companion 数据。
- 行存在性：该 setter 必须用 `INSERT ... ON CONFLICT(account_id) DO UPDATE`（缺失列取 §3 默认值）保证建行。原因见 §7 行存在性说明。
- 仅传入的列参与写入（`None` 入参跳过），便于 turn 级只改单列、天级确定性路径批量改多列。

需要增加 enum normalization helper，避免脏值入库：

```python
RELATIONSHIP_STAGE_VALUES = {"icebreaking", "acquainted", "deep_bond"}
SURVIVAL_STATUS_VALUES = {"healthy", "cooling", "inactive", "resource_risk"}
TRUST_STATUS_VALUES = {"building", "stable", "damaged"}
GROWTH_STATUS_VALUES = {"not_started", "emerging", "stable"}
```

非法值处理建议：

- 写入入口遇到非法值时回退默认值。
- Admin 人工入口后续如开放，应返回 400，而不是静默回退。

## 6. 统计 Helper

在 `app/products/zhaoxi/infrastructure/persistence/user_meta.py` 增加确定性 helper：

```python
def count_inbound_messages(*, account_id: str) -> int:
    """Return total inbound user messages for relationship-stage threshold."""

def list_recent_inbound_message_dates(
    *, account_id: str, since_date: str
) -> list[str]:
    """Return distinct Beijing dates with inbound user messages since since_date."""

def get_wallet_balance_shell_micros(*, account_id: str) -> Optional[int]:
    """Return current shell balance in micros; None when wallet is absent."""
```

说明：

- 消息 `created_at` 已按北京时间字符串存储（DB 默认 `datetime('now', '+8 hours')`），自然日即 `substr(created_at, 1, 10)`，与现有 `compute_message_intensity` 口径一致。**时区换算复用 `app/time_utils`（`beijing_naive_now` / `BEIJING_TZ`），不要另写换算**；scheduler 已是该口径。
- 余额可直接复用现有 `get_wallet_summary(account_id=...)` 取 `balance_shell_micros`（`entitlement_wallets.account_id` 为 `NOT NULL UNIQUE`，确为账号级钱包），`get_wallet_balance_shell_micros` 可作为其薄封装，不必新写一套查询。
- `count_inbound_messages` 与现有 `compute_message_intensity`（已是 inbound `COUNT(*)`，只是取 `floor(ln(1+x))` 且排除当天）口径重叠，实现时优先统一计数逻辑，避免两套 inbound 计数漂移。
- 若后续统一使用业务日边界，可单独调整；本轮按自然日口径实现。
- 资源风险阈值为 `-500 * SHELL_MICROS_PER_SHELL`。

## 7. Turn 级更新

Turn 链路不做 LLM 判断，不全量重算四个状态。

建议新增轻量函数：

```python
def maybe_update_relationship_state_after_turn(*, account_id: str) -> dict:
    """Apply deterministic post-turn relationship-state updates."""
```

执行时机：

- 用户入站消息成功写入后，或普通 turn 成功完成后。
- 不阻塞主回复；失败只记录日志。
- 写入统一走 §5.6 的 `update_account_user_meta_relationship`，不调用 `upsert_account_user_meta`。

行存在性（重要）：

- 经核对，`account_user_meta` 行**仅由天级 scheduler 首次运行时创建**（`onboarding` / `turn_service` 都不建行）。即账号在首次天级任务（北京时间凌晨 3 点）跑过之前没有 meta 行。
- 因此 turn 级若用纯 `UPDATE ... WHERE account_id=?`，对新账号影响 0 行，`icebreaking→acquainted` 升级会静默丢失（天级 §8.1.3 会兜底重算，但有最长近一天延迟）。
- 解决：§5.6 setter 内用 `INSERT ... ON CONFLICT(account_id) DO UPDATE` 兜底建行（缺失列取 §3 默认值），保证 turn 级写入对无 meta 行的新账号也生效。

本阶段只做两类确定性更新：

1. `relationship_stage`
   - 当前为 `icebreaking`
   - 累计入站消息数 `> 30`
   - 更新为 `acquainted`

2. `agent_need_survival_status`
   - 钱包余额低于 `-500` 贝壳时更新为 `resource_risk`

不在 turn 级更新：

- `relationship_stage = deep_bond`
- `agent_need_trust_status`
- `agent_need_growth_status`
- survival 的连续天数状态。这部分放到天级任务。

## 8. 天级任务更新

复用 `app/user_meta_scheduler.py`。

### 8.1 确定性更新

每个账号每日处理时：

1. 计算 `message_intensity_level`。
2. 计算 `safety_risk_trigger_count_30d`。
3. 计算 `relationship_stage` 的 30 条消息阈值。
4. 计算 `agent_need_survival_status`（**转移规则 + 资源风险叠加，非无状态全量重算**）：

   先求"活跃态候选值"，规则按下列顺序判断，命中即取，**全不命中则保留当前值**：

   - 最近 30 个自然日无用户入站消息 → `inactive`
   - 最近 2 个自然日每天都有用户入站消息 → `healthy`
   - 当前为 `healthy` 且最近 3 个自然日无用户入站消息 → `cooling`
   - 以上都不命中 → 保留当前活跃态

   再叠加资源风险：余额低于 `-500` 贝壳时，最终值强制为 `resource_risk`（覆盖上面的活跃态）。

   窗口定义（统一相对 `snapshot_date`，含当天）：

   - "最近 N 个自然日" = `[snapshot_date - (N-1) 天, snapshot_date]` 闭区间。
   - "连续 2 个自然日每天都有消息" = 该 2 天里每一天都至少有一条 inbound。

   口径说明：

   - `resource_risk` 是独立叠加项（仅由余额决定，优先级最高），不参与活跃态转移；这样才能在资源风险解除后用 §3.2 活跃天数规则正常恢复。
   - 之所以不写成 `resource_risk > inactive > cooling > healthy` 的无状态优先级"全量重算"：`cooling` 仅由 "当前为 healthy + 3 天无消息" 这条有状态规则产生，无状态重算会让"当前为 cooling 且无活动"的账号落不到任何分支。转移 + 保留当前值才自洽。
   - 资源风险解除后，下一次天级任务按活跃天数重新得出 `healthy` / `cooling` / `inactive`。

### 8.2 LLM 更新

天级 LLM 可更新：

- `relationship_stage`
- `agent_need_trust_status`
- `agent_need_growth_status`

限制：

- `relationship_stage` 从 `acquainted` 到 `deep_bond` 只能由天级 LLM 判断。
- LLM 信号不足时保留当前状态。
- LLM 不应覆盖 `agent_need_survival_status` 的确定性结果。
- LLM 失败时保留当前状态，不影响本账号其他 meta 字段写入。

Prompt 输入建议：

- 最近 50 条用户入站消息。
- 可选加入最近 assistant 回复摘要，但第一版可只用用户侧消息，和 companion type 保持一致。
- 当前四个状态。
- 明确输出 JSON，只包含三个字段：`relationship_stage`、`agent_need_trust_status`、`agent_need_growth_status`。

第一版也可以先不接 LLM，只落 DB 字段和确定性更新；再在第二步接天级 LLM。

## 9. Admin / Debug

### 9.1 GET meta

`GET /admin/accounts/{account_id}/meta` 现有返回为 `{"meta": {...}}` 外层包裹（未生成 meta 时为 `{"meta": null}`）。四个新增字段挂在 `meta` 下：

```json
{
  "meta": {
    "relationship_stage": "acquainted",
    "agent_need_survival_status": "healthy",
    "agent_need_trust_status": "building",
    "agent_need_growth_status": "emerging"
  }
}
```

仅需在 `get_account_user_meta` 返回里带上这四列即可，端点本身无需改造（保持外层结构不变）。

### 9.2 用户标签列表

`GET /admin/user-meta` 后续可展示四个状态字段。

第一版可以只返回，不做筛选。

### 9.3 人工调整

本轮不做人工调整端点。

后续如需要，新增：

```text
PATCH /admin/accounts/{account_id}/meta/relationship
```

权限建议仅 admin。

## 10. RELATIONSHIP.md 视图

`RELATIONSHIP.md` 保留为账号级可读视图，但不是 source of truth。

后续有两种实现方式：

1. 只在账号 context 文件首次创建时生成默认模板。
2. Admin 查看时由 DB 状态动态渲染，不实际写回 profile_storage。

推荐第二种作为长期方案，避免 Markdown 和 DB 状态漂移。

目标视图：

```md
# RELATIONSHIP

## 关系阶段

- 当前阶段：相识

## 需求满足情况

- 生存 / 活跃：健康
- 信任与尊重：建立中
- 共同成长：有苗头
```

Prompt 装载策略：

- `RELATIONSHIP.md` 不默认进入 `Project Context`。
- 后续编排层需要时，只注入本轮具体策略 block。
- 本方案不定义具体编排策略（编排影响见 `agent_self_prd.md` §7 与使命设计 §4）。

## 11. 测试方案

### 11.1 DB migration

- 新库初始化后四个字段存在，默认值正确。
- 老库迁移后四个字段存在，已有行默认值正确。
- `account_user_meta_daily` 同步补列。
- 已知回填局限：`account_user_meta_daily` 存量行迁移后四列全取默认值（`icebreaking` / `cooling` / `building` / `not_started`），并非历史真实状态。读取历史快照时需注意迁移之前的关系状态不可追溯。

### 11.2 DB API

- `upsert_account_user_meta` 正常写入四个状态。
- `insert_account_user_meta_daily` 正常写入四个状态。
- `get_account_user_meta` 返回四个状态。
- `set_companion_type_manual` 不覆盖四个状态（无需改代码，仅验证）。
- `update_account_user_meta_relationship` 只改传入列、不动 companion 字段；对无 meta 行的账号自动按默认值建行。
- 非法 enum 值按入口规则处理。

### 11.3 Turn 级确定性更新

- 入站消息累计 30 条时仍保持 `icebreaking`。
- 入站消息累计 31 条时从 `icebreaking` 转为 `acquainted`。
- 当前已是 `deep_bond` 时不被消息阈值降级。
- 无 meta 行的新账号累计 31 条入站消息时，turn 级自动建行并写入 `acquainted`。
- 余额低于 `-500` 贝壳时 survival 变为 `resource_risk`。
- turn 级不更新 trust / growth。

### 11.4 天级任务

- 连续两个自然日有用户入站消息，survival 变为 `healthy`。
- `healthy` 后连续三个自然日无用户入站消息，survival 变为 `cooling`。
- 连续 30 个自然日无用户入站消息，survival 变为 `inactive`。
- 余额低于 `-500` 贝壳时 survival 为 `resource_risk`，且不影响其他状态计算。
- LLM 失败时保留当前 relationship/trust/growth。

### 11.5 Admin

- `GET /admin/accounts/{account_id}/meta` 返回四个状态。
- 未运行 user_meta 的账号仍返回 `{"meta": null}`。
- 用户标签列表返回四个状态字段。

## 12. 分阶段落地

### Phase A：结构化字段落库

目标：四个关系字段落库、可读可写、Admin 可见，**无任何行为变更**（不接确定性更新、不接 LLM、不动 prompt 装载）。

- A1 Migration：新增 `_migration_0008_relationship_state`，对 `account_user_meta` 与 `account_user_meta_daily` 用 `_ensure_column` 补四列（默认值见 §3/§4），追加 `(8, ...)` 到 `_MIGRATIONS`。
- A2 读路径：`get_account_user_meta` 与 `list_account_user_meta_current` 的 SELECT 带上四列并返回（`list` 注意四列来自 `m.*`，GROUP BY 不变即可）。
- A3 写路径（关系列单一入口）：新增 §5.6 `update_account_user_meta_relationship`（`INSERT ... ON CONFLICT DO UPDATE`，仅写传入列、缺行按默认建行、enum 非法值回退默认）。本阶段只交付函数 + 单测，不在 turn/scheduler 接线。
- A4 daily 快照：`insert_account_user_meta_daily` 增四个入参；scheduler `_refresh_account` 把已读的 `current_meta` 四个关系字段（缺失用默认）传入。
- A5 `upsert_account_user_meta` 不碰四列（确认 INSERT 列清单与 DO UPDATE 均不含关系列，新行走默认、冲突保留），无需新增入参；`set_companion_type_manual` 同理不改。
- A6 Admin：确认 `GET /admin/accounts/{account_id}/meta`（返回 `{"meta": {...}}`）与 `GET /admin/user-meta` 自动带出四列，端点不改。
- A7 测试：覆盖 §11.1 migration（新库默认值、老库迁移、daily 同步补列、历史行回填局限）、§11.2 DB API（读写四列 / setter 行为 / 非法 enum / `set_companion_type_manual` 不覆盖 / 每日 companion 刷新不重置关系列）、§11.5 Admin 返回。运行 `tests/test_user_meta*.py` 等聚焦测试。

不在 Phase A：turn 级与 scheduler 的确定性更新（Phase B）、天级 LLM（Phase C）、`RELATIONSHIP.md` 视图收敛（Phase D）。

### Phase B：确定性更新

目标：接入 turn 级与天级**确定性**状态更新（不含 LLM），写入统一走 A3 的 `update_account_user_meta_relationship`。

- B1 统计 / 余额 helpers：
  - `app/products/zhaoxi/infrastructure/persistence/user_meta.py`：`count_inbound_messages(account_id)`（inbound 总数，不排除当天，供 30 阈值）；`list_recent_inbound_message_dates(account_id, since_date)`（自 `since_date` 起有 inbound 的去重北京日期列表，供连续天数判断）。
  - `app/db/billing.py`：`get_wallet_balance_shell_micros(account_id)`——薄封装 `get_wallet_summary(account_id, ensure_grant=False, create_if_missing=False)`，取 `wallet.balance_shell_micros`，无钱包返回 `None`。导出到 `__all__`。
  - 资源风险阈值常量 `RESOURCE_RISK_THRESHOLD_MICROS = -500 * SHELL_MICROS_PER_SHELL`。
- B2 共享确定性逻辑模块 `app/relationship_state.py`（纯函数 + 两个编排入口，turn/天级共用，便于单测）：
  - `compute_stage_threshold(current_stage, inbound_count)`：仅 `icebreaking` 且 `inbound_count > 30` → `acquainted`；否则保留（不降级 `deep_bond`/`acquainted`）。
  - `compute_survival_status(current_status, inbound_dates, snapshot_date, balance_micros)`：实现 §8.1 的「转移规则 + 资源风险叠加 + 无规则保留当前值」与 `[snapshot_date-(N-1), snapshot_date]` 闭区间窗口。
  - `maybe_update_relationship_state_after_turn(account_id) -> dict`：turn 级，只做两类确定性更新——`icebreaking`+累计 inbound>30→`acquainted`；余额 < 阈值→`resource_risk`。读一次当前 meta，`stage` 非 `icebreaking` 时跳过计数；经 `update_account_user_meta_relationship` 写入；返回变更摘要。
  - `apply_daily_deterministic_relationship(account_id, snapshot_date, current_meta) -> dict`：天级，计算 stage 阈值 + survival 连续天数/资源风险，写入并返回更新后的四值，供 daily 快照使用。
- B3 turn 链路接线：在 `app/turn_service.py` 现有 post-turn 后台块（`should_run_after_turn` 分支，约 1631–1666 行，与 `write_memory`/`extract_commitment` 并列）追加一个 `background_loop` + `asyncio.to_thread(maybe_update_relationship_state_after_turn, account_id=...)` 任务。不阻塞主回复；异常只记日志。inbound 已在此前持久化，计数含当前消息。
- B4 天级接线：`app/user_meta_scheduler.py` `_refresh_account` 在读取 `current_meta` 后、`insert_account_user_meta_daily` 之前，调用 `apply_daily_deterministic_relationship(...)`，把**更新后的**四个关系值传入 daily 快照（替换 A4 直接透传 `current_meta` 的写法）。LLM 字段（trust/growth、acquainted→deep_bond）留待 Phase C，不在此动。
- B5 测试：
  - turn 级（§11.3）：累计 30 条仍 `icebreaking`；31 条→`acquainted`；`deep_bond` 不被阈值降级；余额 < 阈值→`resource_risk`；turn 不动 trust/growth。
  - 天级（§11.4 确定性部分）：连续 2 自然日有 inbound→`healthy`；`healthy` 后连续 3 自然日无 inbound→`cooling`；连续 30 自然日无 inbound→`inactive`；余额 < 阈值→`resource_risk` 且不影响其余状态计算；窗口边界用例。
  - helpers 单测：`count_inbound_messages`、`list_recent_inbound_message_dates`（北京日去重）、`get_wallet_balance_shell_micros`（无钱包返回 None）。

不在 Phase B：天级 LLM 对 `relationship_stage`(→deep_bond)/`trust`/`growth` 的判断（Phase C）；`RELATIONSHIP.md` 视图（Phase D）。

### Phase C：天级 LLM

目标：天级 LLM 更新 `relationship_stage`(仅 acquainted→deep_bond)、`agent_need_trust_status`、`agent_need_growth_status`。LLM 不碰 `agent_need_survival_status`（确定性），失败/信号不足保留当前值，不影响其余 meta 写入。镜像现有 companion 分类实现（`app/prompts/user_meta_companion_type.py` + `classify_companion_type`）。

- C1 prompt 模块 `app/prompts/user_meta_relationship.py`：
  - 复用枚举（`relationship_stage`/trust/growth 合法值，与 `app/products/zhaoxi/infrastructure/persistence/user_meta.py` 的 `*_VALUES` 一致）。
  - `build_relationship_eval_prompt(messages, current_state)`：输入最近 50 条用户入站消息 + 当前四个状态（供 LLM 参考），输出严格 JSON，只含三个字段 `relationship_stage`/`agent_need_trust_status`/`agent_need_growth_status`。
  - `parse_relationship_payload(raw)`：镜像 `_extract_json_object`+`_normalize_*`；非法/缺失字段归一为 `None`（表示「LLM 无意见」），不抛硬错（仅 JSON 完全解析失败才抛）。
- C2 `app/relationship_state.py` 增量：
  - 纯函数 `merge_llm_relationship(deterministic, llm_out)`：stage 仅允许 `acquainted`→`deep_bond`（其余保留 deterministic，禁止降级/越级）；trust/growth 命中合法枚举则采用，否则保留当前；**survival 恒取 deterministic，不受 LLM 影响**。返回最终四值。
  - `classify_relationship_state_llm(messages, current_state)`：调 `generate_completion` + `parse_relationship_payload`；镜像 `classify_companion_type`，LLM/JSON 失败时抛异常。
  - `apply_daily_llm_relationship(account_id, snapshot_date, deterministic, messages)`：classify → merge → 经 `update_account_user_meta_relationship` 写入 `relationship_stage`/trust/growth（**不传 survival**）→ 返回最终四值。LLM 失败向上抛，由 scheduler 兜底。
- C3 scheduler 接入：`_refresh_account` 在 B4 的确定性 `apply_daily_deterministic_relationship` 之后、`insert_account_user_meta_daily` 之前：
  - 门控：`intensity >= 2`（信号足够，复用 companion 的 `recent_messages` 同次抓取，避免重复取数）。可选节流留作后续（如需按天数限频再加 last-evaluated 列，本期不做）。
  - `try: relationship = apply_daily_llm_relationship(...); relationship_evaluated=True except: 记 warning，保留确定性 relationship`。
  - daily 快照写**最终** relationship 四值；在 `run_once` 结果里加 `relationship_evaluated`/`relationship_failed` 计数与 `errors`（镜像 companion）。
- C4 测试：
  - 纯函数 `merge_llm_relationship`：acquainted→deep_bond 生效；icebreaking/deep_bond 下 LLM 给 deep_bond 不越级/不降级；trust/growth 合法采用、非法保留；survival 永远等于 deterministic。
  - `apply_daily_llm_relationship`：mock 合法 LLM 输出 → trust/growth/deep_bond 落库；mock 非法输出 → 保留当前；mock 抛错 → 由 scheduler 兜底保留确定性、companion 等其余字段不受影响。
  - scheduler `run_once`：LLM 成功路径写最终值；LLM 失败路径保留 B4 确定性结果且 daily 快照一致；信号不足（intensity<2）跳过 LLM。

不在 Phase C：`RELATIONSHIP.md` 视图收敛（Phase D）；LLM 限频列（如需再评估）。

### Phase D：RELATIONSHIP.md 视图收敛

1. 从默认 prompt 注入中移除 `RELATIONSHIP.md`。
2. Admin 可读视图由 DB 渲染。
3. 保留账号隔离和明文权限边界。

## 13. 不在本次范围

- 不定义破冰/相识/挚友阶段分别加载什么话术。
- 不定义小测试、安全话题或主动消息策略。
- 不做运营手动调整端点。
- 不把 `RELATIONSHIP.md` 作为 source of truth。
- 不要求每个 turn 调用 LLM 判断关系状态。
