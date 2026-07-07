# 聊天上下文编排：统一分层方案（融合 dreaming / 窗口 / 压缩）

> 设计文档。目标：把当前散落的四套上下文机制（原始窗口、rolling 压缩、carryover、dreaming）
> 收敛成一条「由细到粗、单一水位线」的编排链，借鉴 OpenClaw 的 compaction 策略。
> 时间戳注入是独立议题，本文只在末尾留接口，不展开。

日期：2026-07-06。状态：**已落地**（P0–P4 全部实现，全量测试 1070 passed）。落地细节与偏差见 §9。

---

## 1. 背景与现状

当前并存四套机制（`turn_service.build_turn_llm_input` 组装）：

1. **原始最近窗口**：`list_recent_messages_for_account` **跨 session** 取最近 `llm_context_messages=100` 条
   → `context_window.trim_history_rows` 按 `llm_context_token_budget=3000` token + 单条 `2000` 字上限裁剪。
   **超预算是直接丢弃，无压缩**。
2. **rolling_summary**（会话内滚动摘要）：`context_summarizer.maybe_update_rolling_summary`，
   **默认关**（`llm_rolling_summary_enabled=False`）。开启后把「本会话溢出 live window 的头部」摘要成
   【更早对话摘要】注入，水位线 `rolling_summary_upto_id` 单调推进。
3. **carryover_summary**（跨 session 承接）：daily dreaming 轮转时生成，注入新 session 的【会话延续摘要】；
   当原始历史已覆盖上一 session 时被 `suppress_carryover` 抑制。
4. **daily dreaming**：`session_lifecycle` + `dreaming.py`，业务日以 **4 点**为界。产出
   `carryover_summary` + `rough_summary` + `long_term_memory_items`（写 MEMORY.md）。
   触发有二：`DreamingScheduler`（4 点，**默认关**）+ 懒触发（新业务日首条消息轮转）。

现状问题：
- 原始窗口跨 session → 日轮转并不真正"重置原文"，还催生了 `suppress_carryover` hack。
- 压缩默认关 → 超预算就是纯丢弃，旧上下文无摘要兜底。
- `rough_summary` **从不注入 prompt**（只 `carryover` 进），是冗余产出。
- carryover 与 rolling 各写各的，是两份摘要而非一条流。

---

## 2. 参照锚点：OpenClaw compaction

`src/agents/compaction-planning.ts` / `compaction.ts`：
- **全按 token**，无条数限制。预算 = `contextWindow * share`（常规 0.5），有绝对底
  `MIN_PROMPT_BUDGET_TOKENS=8000`。
- 超预算 → 按 token 切块，**丢最旧块、留最近块**（迭代到预算内），**丢块拿去摘要、摘要替换注入**。
- 丢弃时 **修复 tool_use/tool_result 配对**（`repairToolUseResultPairing`），避免孤儿 tool_result 报错。
- 摘要分块→合并，带 fallback，有硬上限。

结论：OpenClaw 就是「统一 token 预算 → 溢出压缩 → 注入摘要 → 保留最近尾部」，本方案对齐之。

---

## 3. 目标模型：一条"由细到粗"的水位线

| 层 | 内容 | 触发/产生 | 注入 block |
|---|---|---|---|
| **L0 原始尾窗** | 最近若干轮原文（**session-scoped**） | 每轮，按 token 预算留尾部 | messages 尾部 |
| **L1 滚动摘要** | L0 溢出部分的压缩（**默认开**） | 溢出即压，水位线单调推进 | 【更早对话摘要】（唯一摘要 block） |
| **L2 每日 dreaming** | 全天要点 | **4 点**（+懒触发兜底） | carryover → seed 下一 session 的 L1；long_term → MEMORY.md |

核心不变量：**L2 的 carryover 作为新 session L1 的种子**，L1 随会话内溢出继续追加，
到下一个 4 点再由 dreaming 把（L0 尾 + L1）重新粗化为下一份 carryover。
三份摘要合成**一条随时间粗化的单流**，prompt 里只有**一个**摘要 block。

---

## 4. 已定决策（本次拍板）

1. **原始尾窗改为 session-scoped**：主路径由 `list_recent_messages_for_account`（跨 session）
   切换到 session 范围取数（复用/改造 `list_context_messages_for_session`）。轮转即真正重置原文。
2. **carryover 作为新 session rolling_summary 的 seed**：新 session 创建时
   `rolling_summary := carryover_summary`，之后会话内溢出继续 merge 进同一 `rolling_summary`，
   水位线 `rolling_summary_upto_id` 串起来。**prompt 合并为单一【更早对话摘要】block**，
   删除独立的【会话延续摘要】block。
3. **token 预算保留 3000**（`llm_context_token_budget` 不变）；**最近硬底**由"K 轮"改为
   **「15 分钟内、最多 10 轮」**：这部分原文**永不被摘要/丢弃**；预算与硬底冲突时**硬底优先**
   （即 raw 可能短暂超 3000）。
4. **rough_summary 合并后不保留**：dreaming 产出收敛为 `carryover_summary` + `long_term_memory_items`
   两项；相关分析脚本/字段对齐到合并后的 carryover（详见 §6）。
5. **4 点 dreaming scheduler 默认开，挂在 proactive-scheduler 单例进程**
   （`scripts/run_proactive_scheduler.py`，已是单例 asyncio 循环）。懒触发保留作兜底。

**决策 1+2 的连带收益**：原始窗口不再跨 session → `suppress_carryover` 抑制逻辑**可整体删除**
（不再有 raw 与 carryover 重复的问题）。

---

## 5. 详细设计

### 5.1 组装期（每轮，`build_turn_llm_input`）—— 水位线不变量

核心不变量:**摘要覆盖到水位线,原文 = 水位线之后的全部消息;组装期永不丢弃"尚未进摘要"的原文**
(关掉「已丢出窗口但后台还没压完」的信息缺口)。预算不在组装期靠丢弃维持,而是靠 §5.2 的后台
chunk 压缩推进水位线来维持。

- **rolling 开(默认)**:`pool = 本 session 中 id > rolling_summary_upto_id 的消息`;
  水位线及之前的已折叠进 rolling 摘要,不再以原文出现。正常态 `token(pool) ≤ 预算`(压缩维持);
  若压缩持续失败致 pool 无界,用 **2× 预算硬顶**兜底(此时才从最旧端丢,并 `min_keep=硬底 F`
  护最近)——正常运行永不触发,仅防 prompt 无界膨胀/成本失控。
- **rolling 关**:退回旧行为——按 3000 token 预算从最旧端整条丢 + 硬底 F 优先(无摘要兜底,必须丢)。
- **硬底 F** = 最近「距 now ≤ 15 分钟」∧「≤ 10 轮」的原文(交集),永不被丢/被压。
- 注入:`messages` = pool(尾部原文,历史 user 轮带时间戳);`rolling_summary` 注入唯一【更早对话摘要】。

> token 估算沿用 `context_window.estimate_tokens`(`len/1.5`,非计费);单条 `2000` 字上限保留。

### 5.2 压缩(`context_summarizer.maybe_update_rolling_summary`)—— token chunk,异步

- **默认开**(`llm_rolling_summary_enabled=True`),turn 后**后台异步**跑,不进用户回复链路。
- **触发**:尾窗(id > 水位线)累计 token **> 预算**即压(token 驱动,不再按"攒够 N 条";
  `llm_rolling_summary_trigger_messages` 已废弃)。
- **粒度**:一次压最老 `max(rolling_summary_chunk_tokens(默认 1500), 溢出量)` token 对应的**整条消息**,
  但**绝不碰硬底 F**;把这批 merge 进 `rolling_summary`、水位线前移过这批、落库。压完把 raw 降到
  预算内并留 headroom → 下次要等尾窗重新涨过预算才再压,既不丢信息又不必每轮调 LLM。
- **无缝**:因组装期(§5.1)永不丢水位线之后的原文,"已丢窗口但未压缩"的缺口不存在——
  溢出消息在被压缩前一直以原文留在 prompt 里,压完的下一轮才变成摘要。
- **tool 配对**:压缩跨越 `tool_evidence_replay` 证据时需修复孤儿 tool 证据(实现约束,落地点定)。

### 5.3 轮转与 seed（`session_lifecycle`）

- dreaming 产出 carryover 后开新 session：新 session `rolling_summary := carryover`、
  `rolling_summary_upto_id := 0`（新 session 尚无消息，种子不对应旧 id）。
- 之后新 session 内溢出继续 merge 进这份 rolling_summary。
- 删除 `suppress_carryover` 及其调用（决策 1+2 后无对象）。

### 5.4 Dreaming 4 点调度（`run_proactive_scheduler.py`）

- 在 proactive 进程 `main()` 内追加一个 `DreamingScheduler`（4 点唤醒，跑
  `run_daily_dreaming_scan`），与 `ProactiveScheduler` 并存于同一 asyncio 循环、同一 stop_event。
- FastAPI in-process 的 `DREAMING_SCHEDULER_ENABLED` 保留但**默认仍关**（避免多 backend worker 重复）；
  单例职责统一由 proactive 进程承担。
- 懒触发（业务日轮转）保留：4 点若漏跑，当天首条消息仍会补 dreaming。

### 5.5 L0 历史消息时间戳（本次并入）

**目标**：解决 §1 之外的一个并行问题——用户隔很久（几小时/跨天）再发消息时，历史轮次原样喂给
LLM 且不带时间信息，模型把"很久以前"当成"刚刚"（背景见
`docs/archive/investigations/message-time-gap-handling-investigation.md`，尤其 §6 线上实测）。

**为什么并进本文**：决策 3 的「15 分钟内」硬底本就要求 L0 取数带 `created_at`；时间戳注入复用
**同一列**，是近乎零成本的搭车项，且落点同为 L0，故一并设计。

**设计（对齐 OpenClaw-weixin，见投研 §6.2 / §6.4）**：

1. **粒度**：给 L0 尾窗里**每条 user 原文**注入它自己的 `created_at`（绝对时间戳）；**assistant 不注入**
   （与 OpenClaw weixin 一致，也省 token、不暗示 bot 给自己盖时间）。
2. **格式**：`[周一 2026-07-06 11:39]`（weekday + 日期 + HH:MM，北京时区隐含）。weekday 显式给出——
   小模型不擅自推星期几（OpenClaw envelope 注释同因）。复用 `time_utils._WEEKDAY_CN` /
   `beijing_weekday_str`，`created_at`（`YYYY-MM-DD HH:MM:SS` 北京裸串）`strptime` 后取 weekday、
   丢秒。**绝对时间戳、无 `+elapsed` 相对量**（weixin 实测也无相对量，见投研 §6.2 claim 3）。
3. **落点**：组装期在 `build_turn_llm_input` 由 `kept_rows` 构造 `history` 时，对 user 行把
   `[ts]\n` 前缀进 `content`——**只作用于喂 LLM 的副本，落库 content 不变**（与
   `_wrap_current_message_envelope` 同原则）。必须在 `inject_tool_evidence_replay` **之前**、
   与 `kept_rows` 保持 1:1 的那次列表推导里完成，避免破坏 `zip(history, kept_rows)` 对齐。
4. **当前消息不重复盖**：最后一条 user 消息走 `<current_message>` envelope，其时间≈now 已由
   【运行时信息】block 覆盖，L0 时间戳只作用于**历史** user 轮，避免与 runtime 冗余。
5. **不计入裁剪预算**：前缀在 `trim_history_rows`（单条字符上限 + token 预算）**之后**注入，不占
   2000 字上限、几乎不影响 3000 token 预算（每条 ~14 字，窗口内 user 轮合计 ~100 字，可忽略）。

**连带收益**：采用「每条绝对时间戳」而非「gap note」后，投研 §5 开放问题 3（裁剪后相邻两条
不连续、gap 该按 kept 相邻还是 DB 相邻算）**自动消解**——绝对时间戳不依赖相邻关系。

**留待后续**：摘要层（L1/L2）是否带时间范围，另议（§7）。

---

## 6. 影响面与迁移

- **DB / 取数**：主路径改 session-scoped；`list_recent_messages_for_account` 若无其它调用方可下线或保留给分析。
- **schema**：`session_summary.rough_summary` 停止产出。dreaming schema（`dreaming.py`）去掉 rough 分支，
  `rough_summary` 相关的 `update_session_summary` 落库字段与读取处一并清理。
- **分析脚本**：凡读 `rough_summary` 的（`scripts/*`、admin analytics）对齐到 `carryover_summary`；
  落地前先 grep 全量消费点，逐个改。
- **prompt_builder**：删【会话延续摘要】block，【更早对话摘要】成为唯一摘要 block（trim_priority 需重定）。
- **时间戳（§5.5）**：L0 session-scoped 取数须含 `created_at`（与「15min 硬底」同一需求）；`turn_service`
  构造 `history` 处加 user 行时间戳前缀；`time_utils` 复用现成 weekday/解析。落库不变、无 schema 改动。
- **config**：`llm_rolling_summary_enabled` 默认翻 True（或移除）；新增「硬底 15min/10 轮」两个参数；
  `dreaming_scheduler_enabled` 语义收敛到 proactive 进程。
- **测试**：`test_turn_context_optimization` / `test_prompt_builder` / `test_agent_context` 需覆盖
  session-scoped 取数、硬底优先、carryover seed、单一摘要 block。

---

## 7. 留给后续（不在本文展开）

- **摘要层时间范围**：L1/L2 摘要是否带"覆盖 X 到 Y"的时间范围标注，另议。
- **idle-based session reset**：同一业务日内因长间隔（如早 8 点→晚 10 点）触发轮转，是否需要——
  先上 §5.5 时间戳观察效果再定（投研 §4.2）。
- token 预算是否随模型窗口调大（现锁 3000），后续按成本/效果再评。

---

## 8. 开发方案（落地步骤）

按「先能独立验证、后动主编排」排序，最小化每步回归面。时间戳（§5.5）与 P1 同批，因二者共享
L0 session-scoped + `created_at` 取数。

### P0 · 取数与时间戳（低风险，可独立上线）

1. **`app/db/accounts.py`**：L0 主取数改 session-scoped 且带 `created_at`。
   - 复用/改造 `list_context_messages_for_session`：确认其 SELECT 含 `created_at`（当前只到 id/role/
     content，需补列并回填 dict）；或新增 `list_context_messages_for_session(..., with_created_at)`。
   - `list_recent_messages_for_account`（跨 session）保留给分析，不再是主路径。
2. **`app/time_utils.py`**：新增 `format_history_timestamp(created_at: str) -> str`，把
   `YYYY-MM-DD HH:MM:SS` → `周一 2026-07-06 11:39`（复用 `_WEEKDAY_CN`，解析失败返回空串兜底）。
3. **`app/turn_service.py` `build_turn_llm_input`**：
   - 取数切到 session-scoped（`session["id"]`），`history_rows` 带 `created_at`。
   - 构造 `history` 的列表推导里，对 `role == "user"` 且**非最后一条**的行，`content` 前缀
     `[{ts}]\n`；ts 空则不加。保持与 `kept_rows` 1:1，位置在 `inject_tool_evidence_replay` 之前。
   - metadata 增 `history_session_scoped: True`、`history_timestamped_count`。
4. **测试**：`tests/test_turn_context_optimization.py` / 新增用例覆盖：user 行带前缀、assistant 不带、
   最后一条 user 不带（envelope 覆盖）、tool_evidence zip 未错位、落库 content 未变。

### P1 · 压缩转为默认路径（中风险）

5. **`app/config.py`**：`llm_rolling_summary_enabled` 默认 `True`（或移除开关——见下方待确认）；
   新增硬底参数 `llm_context_floor_minutes=15`、`llm_context_floor_turns=10`。
6. **`app/context_window.py`**：`trim_history_rows` 增「硬底优先」——先圈定 F（15min ∧ ≤10 轮），
   token 预算裁剪时 F 内消息永不丢弃（可短暂超 3000）。需要 `created_at`，签名加时间参数。
7. **`app/context_summarizer.py` `maybe_update_rolling_summary`**：取数改 session-scoped；溢出口径
   与 §5.1 同源；tool 证据配对修复（实现约束，落地点定）。
8. **测试**：`test_prompt_builder` / `test_agent_context` 覆盖硬底优先、单一摘要 block、默认开压缩。

### P2 · carryover→seed 与轮转（中风险，改跨 session 语义）

9. **`app/session_lifecycle.py`**：dreaming 开新 session 时 `rolling_summary := carryover`、
   `rolling_summary_upto_id := 0`；删除 `_history_covers_previous_session` / `suppress_carryover`
   及其在 `turn_service` 的调用。
10. **`app/prompt_builder.py`**：删【会话延续摘要】block，`carryover` 走 rolling 单一【更早对话摘要】；
    重定 trim_priority。`turn_service` 不再单独传 `carryover_summary`。

### P3 · rough_summary 停产 + 分析对齐（低风险，收尾）

11. **`app/dreaming.py`**：dreaming schema 去 `rough_summary` 分支，产出收敛为 `carryover_summary` +
    `long_term_memory_items`。
12. **grep 全量消费点**：`grep -rn rough_summary app/ scripts/`，逐个改到 `carryover_summary`；
    `update_session_summary` 落库/读取字段清理。

### P4 · 4 点 scheduler 默认开（低风险）

13. **`scripts/run_proactive_scheduler.py`**：`main()` 内挂 `DreamingScheduler`（4 点唤醒，跑
    `run_daily_dreaming_scan`），与 `ProactiveScheduler` 共用 asyncio 循环 + stop_event。
14. FastAPI in-process `DREAMING_SCHEDULER_ENABLED` 默认仍关；懒触发保留兜底。

**已定**：`llm_rolling_summary_enabled` **翻默认 True**（保留开关作回滚闸），不移除。

---

## 9. 落地记录（2026-07-06 实现）

全部 P0–P4 已实现，全量测试 **1070 passed**。相对 §8 的实现选择与偏差：

**新增/改动的取数与工具**
- `app/db/accounts.py`：新增 `list_recent_context_messages_for_session(session_id, limit)`——session tail，
  字段对齐旧的 account 取数（id/session_id/message_id/role/content/created_at），下游 tool_evidence /
  时间戳 / metadata 无需改动即复用。旧 `list_recent_messages_for_account`（跨 session）保留给分析。
- `app/context_window.py`：硬底以 **`trim_history_rows(min_keep=...)`** + 纯函数 **`compute_floor_count()`**
  实现（未按 §8「给 trim 签名加时间参数」，而是在组装期算出 F 的条数传入 `min_keep`——trim 恒保留尾部
  连续块，「保留最近 F 条」== `min_keep=len(F)`，更纯、更好测）。`compute_floor_count` / `parse_db_timestamp`
  放在 context_window 供 turn_service 与 context_summarizer 共用。**两维都 ≤0 = 不设硬底（保底 1 条）**。

**carryover block 已物理删除（§5.3 / §6 字面完成）**
- `prompt_builder.build()/assemble()` 的 `carryover_summary` 参数与【会话延续摘要】block emission 已删除；
  【更早对话摘要】(rolling) 成为 builder 里唯一的摘要 block。
- 连带清理：删掉 2 个专测该 block 的用例（`test_carryover_summary_truncated_at_2000` /
  `test_carryover_summary_present_when_provided`）；builder pipeline 测试里原拿 carryover 当通用 volatile
  示例的几处改用 `rolling_summary`（prio 25）。`sessions.carryover_summary` 落库列与 DB
  `get_or_create_session(carryover_summary=…)` 参数**保留不变**（dreaming 仍产出 carryover 作为 rolling seed）。

**P3 rough_summary 停产（保留向后兼容）**
- dreaming schema `required` 收敛为 `["carryover_summary"]`；`rough_summary` 属性保留为**可选**，规范化时
  若只给了 rough 则折叠进 carryover（兼容旧输出）。`session_summary` 落库列改写 carryover。debug preview、
  session_lifecycle 兜底摘要一并对齐。scripts/ 无 rough_summary 消费点。

**P4 调度**
- proactive 进程新增 `DreamingScheduler`（4 点），受新开关 `proactive_dreaming_scheduler_enabled`（默认 True）
  门控；节点角色逻辑沿用 main.py（中心角色扫全量、纯 node 只扫自身）。FastAPI in-process
  `DREAMING_SCHEDULER_ENABLED` 默认仍关，二者互斥。

**追加：token chunk + 水位线不变量（修复信息缺口）**
- 起因：原「攒够 20 条溢出才压缩」会让「已丢出窗口但尚未进摘要」的消息在若干轮里从 prompt 里缺失。
- 组装期（turn_service）改为**水位线不变量**：rolling 开时 `pool = id > 水位线`，永不丢未进摘要的原文；
  预算靠后台压缩推进水位线维持；2× 预算硬顶仅作 anti-OOM 兜底。rolling 关时退回旧的按预算丢弃 + 硬底。
- 压缩（context_summarizer）改为 **token 驱动 + chunk**：尾窗 token > 预算即压最老
  `max(chunk_tokens=1500, 溢出量)` token 的整条消息（不碰硬底），merge + 前移水位线，压完留 headroom。
- config：新增 `rolling_summary_chunk_tokens=1500`；`llm_rolling_summary_trigger_messages` 废弃。
- `list_context_messages_for_session` 增 `created_at`（供压缩算硬底/token）。

**修复两处 review 发现的缺陷（P1）**
- **scheduler 路径丢 carryover seed**：4 点 `run_daily_dreaming_scan` 只**关闭**旧 session（carryover 落在
  被关闭的行上），新 active session 由下条消息懒创建、不走轮转分支 → 拿不到 seed；叠加 L0 已 session-scoped
  （不再跨 session 兜底）→ 该账号丢失前一天延续。**修复**：`_maybe_seed_new_session_from_last_closed` 在
  "无 rolling 且 turn_count==0 的新 active session 首条消息"时，从 `get_latest_closed_carryover_for_account`
  回填最近已关闭 session 的 carryover（首条后 turn_count>0 即短路，不再查库）。
- **水位线不变量取数不完整**：组装期原先"取最近 N 条再 filter id>水位线"，当水位线之后消息**条数 > N**
  （大量碎消息、总 token 仍在预算内、未触发压缩）时，最老的未摘要消息既不在原文也不在摘要 → 缺口。
  **修复**：rolling 开时改为 `list_context_messages_for_session(after_id=水位线, limit=2000)` 取"水位线之后
  全部"，`llm_context_messages` 仅在 rolling 关的兜底路径作最近 N 条上限。

**测试**
- 新增/改写：session-scoped 尾窗、硬底优先于预算、单一摘要 block、carryover→rolling seed（轮转端到端，
  确定性兜底路径）、floor 纯函数。chunk 压缩：超预算压最老 chunk、溢出<chunk 留 headroom、within_budget
  跳过、溢出落硬底内跳过。水位线不变量：折叠水位线之前、保留水位线之后（哪怕超预算）；**碎消息条数超取数
  上限仍不丢**；**scheduler 关闭后下条消息补种 carryover**。
