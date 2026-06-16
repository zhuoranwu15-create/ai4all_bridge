# 基线优化对齐（review 跟踪文档）

创建时间：2026-06-16
状态：**P0 已完成，P1-1/P1-2/P1-3/P1-5 已完成并通过收尾复审（630 测试全绿）**；P1-4 与 P2 铺路项仍待排期。本文用于对齐本轮代码 review 发现的待优化项与落地排期，不替代各项的独立技术设计。

## 背景

当前系统压力很低，但面向后续要开发更多策略（上下文编排、主动消息机制等）和更多功能，需要先把代码基线打扎实。本轮 review 的结论：

- **账号隔离不变量健康**：DB 层逐 SQL 核查 + 文件写入路径全部绑定 `account_id`，未发现真正的跨账号串线 bug。
- 测试 **602 passed / 0 failed（~39s）**，迁移框架幂等，`contacts` 表 / 规则式 Intent Gate 残留已清理干净，`step3_sent` 是有效的历史兼容分支（勿删）。
- SQLite 已开 `WAL` + `busy_timeout=5000` + `synchronous=NORMAL`（早期扩容文档里「未开 WAL」的结论已过时）。
- 问题集中在两类：(1) 几处真实但潜伏的正确性/安全缺口（便宜、独立、应尽快修）；(2) 编排层是线性脚本式结构，几乎没有给「加策略」预留扩展点。

> 容量/扩容（async 化、迁 PG、Redis 限流、Gateway 分片等）已有专门文档：
> [`../backlog/100k_dau_scaling_review.md`](../backlog/100k_dau_scaling_review.md)。本文不重复。

---

## P0 — 现在就修（便宜、独立、不依赖大重构）

| 编号 | 问题 | 证据 | 影响 / 修法 | 状态 |
|---|---|---|---|---|
| **P0-1** | 调度链路用 `datetime.now()`（server-local naive）与存北京时间的 DB 列比较 | `proactive/scheduler.py:72-73`（`current=now or datetime.now()` 喂给全部 dispatcher）、`reminders.py`/`commitments.py`/`messaging.py`/`content_invitations.py`/`state.py`/`account_checks.py`/`reactivation.py` 的 `now or datetime.now()` 默认；DB 列处处 `datetime('now','+8 hours')` | 宿主机若为 UTC（云默认），提醒晚 8 小时触发、quiet hours 偏移 8 小时。统一改用北京 naive 时间（`time_utils.beijing_naive_now()`）。**第二层防御见下方「宿主机时区不变量」。** | ✅ 本次修复 |
| **P0-2** | `account_checks`/`reactivation` 用 `local_to_utc_string` 把窗口阈值额外偏移一个时区；注释「created_at is UTC」是过时错误 | `reactivation.py:50-64`（def）/`:511`、`account_checks.py:647/665`；权威反例见 `reactivation.py:448-450` 已修正的 `_inbound_count_after` | 与 P0-1 同根。活跃窗口/去重判定不准。改为全程北京时间直接比较，删除 `local_to_utc_string`。 | ✅ 本次修复 |
| **P0-3** | 生产无强制 secret 守卫：`ai4all_bridge_secret`/`admin_token` 默认 `dev-secret`/`dev-admin-token`，仅 `/ready` 软提示、启动不阻断 | `config.py:6-7`、`health.py:37-49` | 运维漏配即暴露 admin 全权限。加 `model_validator`：非 local/development/test 环境且仍是 dev 默认/空密钥则启动 fail-fast；health/ready 软提示保留为双保险。 | ✅ 本次修复 |
| **P0-4** | `billing.py`（2880 行）零直接单元测试，扣费/赠贝/幂等只有黑盒间接覆盖 | 仅 3 个测试经 HTTP/脚本入口触及 billing | 金钱红线。已补 `tests/test_billing_charges.py`：token→贝壳换算、`grant_shells` 幂等+正数校验、新用户赠送恰好一次、`record_chat_usage_charge` 扣费正确+幂等+无 owner 返回 None。referral 链路单测后续补。 | ✅ 本次修复（referral 待补） |
| **P0-5** | `background_loop` 为 None 时记忆写入 + commitment 抽取被静默跳过、无日志 | `turn_service.py:1120`、`web.py:412` | 改为非静默：loop 为 None 时记 `logger.warning`（数据丢失可观测）。**完整 sync fallback 转 P2 after-turn hook 重构**——届时可干净区分「无 loop」与「测试/调用方主动 opt-out（多处单测显式传 `background_loop=None`）」，避免误触发 LLM 调用。 | ✅ 本次修复（sync fallback 转 P2） |

### 宿主机时区不变量（第二层防御）

本系统当前**假设单一北京时区**：DB 时间列均为 `datetime('now','+8 hours')`（北京 naive），
调度/主动消息统一用 `time_utils.beijing_naive_now()`，日志与其余 `datetime.now()` 调用方也按
宿主机本地时区呈现。P0-1 修复后，**即使宿主机不是 Asia/Shanghai，主动消息时序也已正确**
（不再依赖宿主机 TZ）。

在此之上加了一层运维防御 `time_utils.verify_host_timezone()`：

- 检测宿主机本地 UTC 偏移；不是 +8h 时记 **ERROR 日志**（经 Feishu 告警 handler 自动上报），
  但**不阻断启动**（兼容继续运行）。
- 接入点：FastAPI 启动 `app/main.py::startup()`（紧随 `configure_error_log_alerting` 之后，
  确保 ERROR 能进 Feishu）；独立调度进程 `scripts/run_proactive_scheduler.py::main()`
  （自行接 Feishu handler + 校验）。
- 现状：线上机已确认为 `TZ=Asia/Shanghai`，此校验为回归防护。

**补修（2026-06-16 收尾复审后）**：P0-1 当时只扫了 `app/proactive/*` 的调度 dispatcher，漏了
`app/tools/content_invitation_handlers.py`——该文件 6 处 `datetime.now()` 既写 DB 北京时间列
（`retrieved_at`/`expires_at`/`responded_at`/`cooldown_until`），又拿宿主机本地时间与北京时间列
比较（`get_active_content_invitation(now=...)` 活跃窗口、`expires_at <= now` 过期判定）。后者与
P0-1/P0-2 同类，非北京宿主机会偏移。已全部改为 `beijing_naive_now()`，630 测试全绿。

**补修二（2026-06-16，同类待定项一并统一）**：上述两个同类待定项已处理：

- `app/session_lifecycle.py:61`：`datetime.now().date().isoformat()` → `beijing_now().date().isoformat()`
  （dreaming 业务日 fallback；`beijing_now` 本已导入）。
- `app/db/ops.py:39` `_runtime_timestamp`：写入端 `datetime.now()` → `beijing_naive_now()`，scheduler
  heartbeat 落库时间戳统一北京墙钟。**该改动非孤立**：`scripts/monitor_health.py:87` 的陈旧度比较
  `datetime.now() - last_seen_at` 是配套消费端，同步改为 `beijing_naive_now()`，使写入/比较成对自洽
  （否则只改写入端会让非北京宿主机偏移 8h、误判心跳新鲜/陈旧）。`monitor_health.py` 其余 `datetime.now()`
  （备份目录名时间戳比较 line 124、脚本自身状态文件 bookkeeping）非 DB 北京列消费端，保持不动。

纯展示/耗时类（`health.py`/`admin_ops.py`/`debug.py:766` 的 `checked_at`、`debug.py:916/924`
的耗时差值、`aliyun_alerting.py` 告警文本）无不变量风险，不需改。

> **⚠️ 海外业务修订点**：以上单一北京时区假设是**全局不变量**。若未来开展海外业务，
> 必须重新设计为**按账号/用户时区**处理（提醒、quiet hours、活跃窗口、日报边界等），
> 并修订 `app/time_utils.py` 所有 `beijing_*` 工具、`EXPECTED_HOST_UTC_OFFSET_HOURS`、
> 以及全部 DB 时间列写入口径。改造前请先在本文档登记影响面。

---

## P1 — 编排扩展性地基（加更多策略之前做，回报最大）

当前「加一个能力」的真实成本：加一个上下文来源 = 改 3 处（无注册表）；加一个工具 = 改 3 处（schema↔handler 字符串名靠人工对齐）；加一类主动消息 = 改 4-5 处（dispatcher 骨架手抄）。

| 编号 | 问题 | 证据 | 改造方向 | 状态 |
|---|---|---|---|---|
| **P1-1** | `handle_openclaw_turn` 是 811 行上帝函数 | `turn_service.py:374-1184`；纯装配器 `build_turn_llm_input:163-295` 已是干净种子 | 行为保持型拆分为脊柱 + 4 阶段函数：`_prepare_turn`（解析+守卫）→ `_persist_and_screen_inbound`（入站持久化+筛查）→ `_resolve_turn_reply`（解析回复）→ `_finalize_turn`（终结）；3 个内部 dataclass（`_TurnSetup/_InboundResult/_ReplyResult`）显式承载跨阶段状态。脊柱约 50 行。612 测试全绿，invariant（monotonic 5/debug_metadata.update 3/事务 2/响应构造 9）守恒。 | ✅ 本次完成 |
| **P1-2** | prompt block 顺序硬编码、block 是裸字符串、无注册表、无 token 预算 | `prompt_builder.py:120-217` | 重构为声明式 block 流水线：`ContextBlock`(name/section/char_limit/trim_priority) + `assemble()->BuildResult`（自产 block 元数据，消除调用方手维护的 `*_chars` 与僵尸 `daily_notes_loaded`）；`build()` 委托 `assemble().prompt` 保持 str 契约。新增 **token 预算裁剪机制（默认 `None` 关闭，零行为变更）** 按 trim_priority 丢弃 volatile、stable 永不丢；新增 `extra_blocks` 动态来源注入钩子。范围已确认只建机制不启用、不 wire daily notes。630 测试全绿（43 prompt 回归逐字不变 + 6 新机制测试）。 | ✅ 本次完成 |
| **P1-3** | tool registry 三处分裂，schema↔handler 无单一事实源 | `definitions.py:377-389`、`executor.py:36-67` | 新建 `app/tools/registry.py`：`ToolSpec`(name/schema/group/handler/call_style/runtime_flag/default_flag) 为单一事实源，schema 仍由 definitions 提供、handler 惰性引用（避免循环导入）。`get_default_tools` + 分发都从注册表派生，布尔开关收敛成 `default_when_flag`，**import 时双向校验把 schema↔handler 漂移变成启动期崩溃**。executor 两个手写 dict + web_search 特判全部删除。624 测试全绿（新增注册表一致性/gating 测试）。 | ✅ 本次完成 |
| **P1-4** | 主动消息每类各写一套 dispatcher，scheduler 硬编码 5 步；policy 闸门用散落的 `category in {...}` | `scheduler.py:86-118`、`reminders.py` vs `commitments.py` 近乎逐行同构、`policy.py:203/288/365/396` | 抽 `ProactiveStrategy` 协议 + 公共 `run_strategy()` helper；category→闸门做成声明式表。 | 待排期 |
| **P1-5** | LLM 400 bug 根因未除：产品域意图识别（正则强制 `tool_choice`）硬编码进通用 `generate_reply_with_tools` | `llm.py:206-217`（误判源）/`:402-415`（创可贴）、`account_checks.py:857-866`（触发路径） | 把意图判定（`_infer_proactive_update_tool_choice` + 正则）从 `llm.py` 移到 turn 域；通用入口改为消费调用方传入的 `first_round_tool_choice="auto"`（保留"工具不在列表则降级"防御）。主对话 turn 路径显式传入，主动消息生成路径默认 auto → **误判从根上消除**，新策略默认安全。618 测试全绿（新增意图单测 + 通用入口契约测试 + turn 接线测试）。 | ✅ 本次完成 |

### P1-4 关联的主动消息正确性子项

| 编号 | 问题 | 证据 | 改造方向 |
|---|---|---|---|
| P1-4a | reactivation 发送前无按 account 的原子 claim，多实例并跑有竞态（最终仅靠 `outbound_messages.idempotency_key` 兜底，不会真重复投递但状态机有竞态） | `reactivation.py:699/847`；对比 `reminders.py:43`/`commitments.py:286` 的 rowcount==1 claim | 给 reactivation 补按 account 的原子 claim，与其它三类对齐；或文档/启动校验强制单实例 dispatch。 |
| P1-4b | 周期类主动消息若复用 `{type}-{id}` 幂等键会被上周期 sent 行短路、静默不发 | `reminders.py:19-33` 注释记录了此雷；`create_outbound_message` 的 `INSERT OR IGNORE` 把错误变成静默不发 | 提供 `proactive_idempotency_key(category, id, occurrence)`，对周期类强制 occurrence。 |

---

## P2 — 随 P1 顺手归位 / 铺路

- **token 预算与裁剪**：历史只按条数截断（100 条）、各 block 按字符硬截，无总预算、无跨 block 优先级。依赖 P1-2 的 block 一等对象。
- **after-turn 统一 hook**：记忆走 background loop、计费/审核/onboarding 推进各自同步 try/except，机制不一。
- `main.py` 三处 `@app.on_event` 已废弃 → 迁 `lifespan`（`main.py:428/439/444`）。
- `config.py` 162 个字段零校验：对采样百分比、时间窗等高风险字段加 `@field_validator`。
- 加 `test_init_db_full_schema` 全量 schema 基准，为追加 m0002 铺路。
- **预存 dead code**（收尾复审记录，非本轮回归）：`turn_service.py:943/965` 的 `agent_context = read_agent_context(...)` 赋值未被消费——`build_turn_llm_input` 内部会自行重新读取。原始文件（重构前）即存在，按最小改动原则本轮未动；后续清理时需先确认 `read_agent_context` 是否有需要保留的副作用。

---

## 落地顺序建议

1. **P0**（本次先做 P0-1 + P0-2；P0-3/P0-4/P0-5 随后，独立低风险）。
2. **P1-1**：拆 `handle_openclaw_turn`（拆前先补 P0-4 + turn 主链路纯函数单测当安全网）。
3. **P1-2/P1-3/P1-4** 三个注册表抽象；P1-5 与 P1-2 一并解耦。
4. **P2** 跟随。

## 变更记录

- 2026-06-16：创建文档；完成 P0-1 / P0-2 时区修复。
- 2026-06-16：完成 P0-3（secret fail-fast）、P0-4（billing 关键路径单测）、P0-5（background_loop 非静默告警）；新增宿主机时区第二层防御 `verify_host_timezone` 并标注海外业务修订点。P0-5 的完整 sync fallback、P0-4 的 referral 单测分别转入 P2 / 后续。
- 2026-06-16：完成 P1-1，行为保持型拆分 `handle_openclaw_turn`（811 行上帝函数 → 脊柱 + `_prepare_turn`/`_persist_and_screen_inbound`/`_resolve_turn_reply`/`_finalize_turn` + 3 dataclass）。612 测试全绿，无行为变更。后续可选：为特殊命令分支补单元测试，把这层从纯黑盒升级为单元保护。
- 2026-06-16：完成 P1-5，解耦强制 `tool_choice`。意图判定从通用 `generate_reply_with_tools` 移到 turn 域（`turn_service._infer_proactive_update_tool_choice`）；通用入口新增 `first_round_tool_choice="auto"` 参数 + 保留"工具不在列表降级"防御。主动消息生成路径默认 auto，误判 400 从根上消除。618 测试全绿。
- 2026-06-16：完成 P1-3，工具注册表 `app/tools/registry.py` 成为 schema↔handler 单一事实源（import 双向校验）；executor 改为从注册表分发，删除手写 dict + web_search 特判；布尔开关收敛成 `default_when_flag`。行为保持（默认集顺序/gating/三种 handler 调用风格/运行时开关均不变），624 测试全绿。至此 P1 仅剩 P1-2（ContextSource + token 预算）。
- 2026-06-16：完成 P1-2，`prompt_builder` 重构为声明式 block 流水线 + `assemble()` 自产元数据（消除僵尸 `daily_notes_loaded`）+ token 预算机制（默认关闭，零行为变更）+ `extra_blocks` 动态来源钩子。turn_service/debug.py 改 `build→assemble` 并消费自产元数据。630 测试全绿。至此 **P1-1/P1-2/P1-3/P1-5 已完成，P1-4 仍待排期**；剩余 P0-3/4/5 已在更早完成，待办还包括 P2 项（token 预算实际启用、after-turn hook、main.py lifespan、config 校验、init_db 全量 schema 基准测试）。
- 2026-06-16：**收尾复审（P0+P1 全量）**。逐文件审 diff + pyflakes 全量扫描 + 全量测试。结论：所有改动行为保持、账号隔离不变、无未定义名/变量泄漏。三处收尾处理：(1) 修复 `test_proactive_sync_guard_cancels_without_gateway_send` 的**预存时间依赖缺陷**——该用例验证 moderation sync guard，但未固定 `now`、未 bypass 静默时段，夜间(北京 22:00–08:00)会被 `quiet_hours` 先行短路而失败（与本轮改动无关，宿主机=CST 时在 main 上同样失败）；加 `bypass_quiet_hours=True` 使其与墙钟无关。(2) 清理 `registry.py` 未使用的 `Any`/`Callable` import。(3) 登记 `turn_service.py:943/965` 预存 dead code 至 P2（本轮不动）。最终 **630 测试全绿**。
- 2026-06-16：**补修 P0-1 漏网点**（Codex 复审提出）。`app/tools/content_invitation_handlers.py` 6 处 `datetime.now()` → `beijing_naive_now()`，统一到北京时区不变量（含活跃窗口/过期比较两处真实潜在 bug + 4 处落库时间戳口径）。同步全量审计了 `app/` 其余 `datetime.now()` 调用方，归类记于上方「宿主机时区不变量·补修」段：另有 `session_lifecycle.py:61`、`db/ops.py:39` 为同类待定项，纯展示/耗时类无需改。630 测试全绿。
- 2026-06-16：**同类待定项一并统一**（接上条）。`session_lifecycle.py:61` dreaming 业务日 fallback 改 `beijing_now()`；`db/ops.py:39` `_runtime_timestamp` scheduler heartbeat 落库改 `beijing_naive_now()`，并**配套**改 `scripts/monitor_health.py:87` 陈旧度比较为 `beijing_naive_now()`（写入/比较成对自洽，避免非北京宿主机偏移 8h 误判心跳）。`monitor_health` 其余 `datetime.now()`（备份名比较/状态文件 bookkeeping）非 DB 北京列消费端，保持不动。630 测试全绿。
