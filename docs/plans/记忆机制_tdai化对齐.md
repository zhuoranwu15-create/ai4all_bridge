# 记忆机制 / TDAI 化对齐：技术设计文档

> 状态：**方案已被取代**（2026-07-12 核实）。本文提出的「在现有栈上自建最小等价层（结构化 memory records + account-level persona snapshot + query-time recall）」最终**未采用**；query-time 记忆召回改由 **TDAI sidecar 集成**实现——`app/tdai_client.py::recall` 已接入 `app/turn_service.py`（热路径严格超时降级），capture/recall 开关见 `.env` 的 `TDAI_*` 与 [`tdai_multitenant_design.md`](../tech_design/tdai_multitenant_design.md)。本文的 L3 persona / L1 records 具体设计仅作历史参考，实际以 TDAI 集成文档为准。
> 定位：OpenClaw 对标落地的第 3 线，负责长期用户理解、结构化记忆、query-time 召回、动态用户画像和召回防污染。
> 非目标：不实现 runtime tool evidence replay、不新增 `web_fetch/read_skill`、不定义事实纪律 wording、不改投递层、不处理 provider prompt cache。
> 主要依据：`openclaw_study/04_2_memory_study.md`、`04_2_1_tdai_memory_internal_study.md`、`05_对话效果对齐_综合清单.md`、本项目 `memory_writer.py`、`dreaming.py`、`prompt_builder.py`、`user_profiles.py`、`account_user_meta` 现状。

---

## 1. 结论

weixin_bot 现在不是“没有记忆”。它已经有三层材料：

1. `messages` 表保存原始会话。
2. `app/memory_writer.py` 把用户和助手可见文本追加到 `memory/YYYY-MM-DD.md` daily notes。
3. `app/dreaming.py` 做 session 压缩，生成 `carryover_summary` 和 `long_term_memory_items`，并把条目应用到 `USER.md`、`MEMORY.md`、`SOUL.md`、`IDENTITY.md`，同时写 `dreaming_runs`、`dreaming_memory_items`、`memory_events` 审计。

缺的不是“再写一个 MEMORY.md”，而是 TDAI 里真正影响人味的两件事：

- **L3 叙事型用户画像**：每轮开口前，模型知道“这个人长期是什么样、怎么沟通更合适”。
- **L1 query-time 相关记忆召回**：当前问题相关的少量事实/偏好/事件被精准注入，而不是整份文件塞进 prompt。

本线建议不照搬完整 TDAI，而是在现有栈上自建最小等价层：

1. 保留 `messages`、daily notes、`dreaming` 审计作为原料和回滚基础。
2. 新增结构化 memory records，作为动态召回的 canonical store。
3. 新增 account-level user persona snapshot，作为 `<user-persona>` 等价块。
4. 在 `PromptBuilder.extra_blocks` 或等价入口注入 `<user-persona>` 和 `<relevant-memories>`。
5. 明确召回内容只对当前轮可见，不写回 `messages`、daily notes 或用户原话。

优先级：先做 L3 persona，再做 L1 records + recall，L2 scene navigation 最后做。

---

## 2. 与其他线的边界

| 事项 | 归属 | 本线处理方式 |
|---|---|---|
| 跨 turn tool evidence replay | 第 1 线 runtime | runtime 让模型“记得自己刚刚查过什么”；本线让模型“懂用户是谁” |
| 外部搜索/抓取事实 | 第 1 线 runtime + 第 2 线 prompt | 本线不把 web 结果当长期记忆，除非用户明确要求长期保存某个偏好或事实 |
| 当前消息 typed envelope | 第 1 线 runtime | 本线可以用当前用户文本做检索 query，但不定义 envelope 格式 |
| 事实纪律、无法核实时怎么说 | 第 2 线 prompt | 本线只提供记忆材料，不写回答风格主规则 |
| 静态 `SOUL/USER/MEMORY` 模板 | 第 2 线 prompt | 本线可以读取和补充，但不负责模板文案 |
| prompt cache boundary | 第 4 线 cache | Persona 是否进入稳定前缀、如何缓存，由第 4 线评估 |
| 主动消息调度 | proactive 模块 | 本线只提供 persona / memory 给主动文案生成使用 |

核心边界：第 1 线的 tool replay 是短期工具现场，生命周期短、来源是工具调用；本线的 memory recall 是长期用户理解，生命周期长、来源是用户可见对话和审核后的记忆抽取。

---

## 3. OpenClaw / TDAI 参照

从研究文档看，TDAI 在 OpenClaw 里通过 plugin hook 接入：

- `appendSystemContext` 注入 `<user-persona>`、`<scene-navigation>`、`<memory-tools-guide>`。
- `prependContext` 注入 `<relevant-memories>`。
- `before_message_write` 剥离 `<relevant-memories>`，避免污染 L0。
- `agent_end` 捕获本轮材料，异步推进 L0/L1/L2/L3。

TDAI 四层含义：

| 层 | TDAI 表现 | 对话价值 |
|---|---|---|
| L0 原始会话 | conversation jsonl / `tdai_conversation_search` | 查原话、校验时间线 |
| L1 结构化记忆 | records / `<relevant-memories>` / `tdai_memory_search` | 当前问题相关事实、偏好、事件 |
| L2 场景块 | scene blocks / `<scene-navigation>` | 告诉模型有哪些长期情境，可按需 deep dive |
| L3 Persona | `persona.md` / `<user-persona>` | 稳定用户画像和沟通策略 |

weixin_bot 不需要先做 tool 化的 `tdai_memory_search`。对微信陪伴效果，第一阶段更适合做“自动 pre-recall 后注入 prompt”，让模型少做一次工具调用，降低延迟和复杂度。

---

## 4. weixin_bot 现状映射

| TDAI 层 | weixin_bot 当前对应 | 现状评价 | 缺口 |
|---|---|---|---|
| L0 原始会话 | `messages` 表、`list_recent_messages()`、`memory_writer.py` daily notes | 原料存在，账号隔离已由 `account_id` 约束 | 缺面向记忆的检索 API；daily notes 是 Markdown，不是结构化索引 |
| L1 结构化记忆 | `dreaming_memory_items` 审计条目、`USER.md` / `MEMORY.md` 写回 | 有抽取和审计，但 canonical 结果仍落到整文件 | 缺可检索 memory records、去重合并、query-time recall |
| L2 场景 | 无 | 暂不影响第一阶段 | 缺 scene block、scene index、deep dive 机制 |
| L3 Persona | `SOUL.md`、`IDENTITY.md`、`USER.md`、`account_user_meta` 关系状态 | 有静态设定和结构化关系字段 | 缺叙事型 user persona snapshot；`account_user_meta` 不是人格画像 |
| session continuity | `sessions.carryover_summary` | 能承接上一段 session | 不是长期记忆召回，不能替代 L1/L3 |
| prompt 注入 | `PromptBuilder.extra_blocks`、Project Context | 有扩展点 | 没有 memory 召回 block |
| 防污染 | 目前无动态召回，所以风险未暴露 | 存储态基本干净 | 上 recall 后必须保证召回块不写入 messages/daily notes |

关键判断：`dreaming.py` 不是要废掉，而是从“直接改 Markdown 文件”的长期记忆整理器，逐步升级为“产生可审计结构化 records 和 persona 的管道”。

---

## 5. 设计原则

1. **账号隔离是硬约束**：所有 memory/persona/scene 查询和写入必须以 `account_id` 过滤。
2. **存储态保持原始**：`messages` 和 daily notes 只保存用户与助手真实可见文本，不保存 prompt-only 召回块。
3. **结构化记忆可追溯**：每条 memory record 必须能追到 source message/session/daily note/dreaming run。
4. **先保守后自动**：敏感、高风险、低置信内容先不自动注入；必要时只进入审计队列。
5. **先 FTS/规则，后向量**：第一阶段不引入新依赖；SQLite/PG 后端通过统一 DB 函数抽象检索实现。
6. **Persona 不是事实库**：persona 负责沟通策略和长期理解；具体事实仍由 relevant memories 或工具结果提供。
7. **召回短小可控**：每轮注入少量高相关材料，宁缺毋滥。
8. **与静态文件共存过渡**：短期不删除 `USER.md/MEMORY.md`，先并行生成新结构，质量稳定后再降低整文件权重。

---

## 6. 目标架构

### 6.1 数据流

```text
用户消息 + 助手回复
        |
        v
messages 表（原始会话）
        |
        v
memory_writer.py -> memory/YYYY-MM-DD.md（L0 daily notes）
        |
        v
dreaming / persona builder（异步、可审计）
        |
        +--> account_memory_records（L1 结构化记忆）
        |
        +--> account_user_persona（L3 叙事画像）
        |
        +--> account_memory_scenes（L2，后续）

当前 turn:
当前用户文本 -> retrieve_relevant_memories(account_id, query)
              -> read_user_persona(account_id)
              -> PromptBuilder.extra_blocks 注入
```

### 6.2 Prompt 注入形态

建议先用中文块名，避免 XML-like 标签被用户诱导复述；调试 metadata 中记录英文 block name。

```text
【用户画像（系统生成，可能不完整）】
- 用户长期偏好：...
- 沟通策略：...
- 需要避免：...

【本轮相关记忆】
- [高置信，2026-06-18] 用户偏好...
- [中置信，来源：上一段 session] ...

使用规则：这些是系统召回的长期记忆，不是用户本轮原话；如果与用户本轮说法冲突，以用户本轮说法为准，并可轻量确认。
```

本线生成 block 内容；第 2 线负责把“如何使用这些 block”写进 prompt 纪律。

---

## 7. 数据模型设计

### 7.1 `account_memory_records`

新增 canonical memory records 表，承载 L1 结构化记忆。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | integer/text | 主键 |
| `account_id` | text | 隔离键，所有查询必带 |
| `kind` | text | `profile_fact`、`preference`、`episode`、`relationship`、`communication_style`、`instruction`、`negative_preference` |
| `title` | text | 短标题，用于 debug/admin |
| `memory_text` | text | 可注入 prompt 的短文本 |
| `normalized_key` | text | 去重键，如 `preference:reply_length` |
| `importance` | text | `high` / `medium` / `low` |
| `confidence` | real | 0-1 |
| `sensitivity` | text | `normal` / `sensitive` / `highly_sensitive` |
| `status` | text | `active` / `superseded` / `deleted` / `pending_review` |
| `source_type` | text | `dreaming` / `manual` / `migration` / `admin` |
| `source_session_id` | integer nullable | 来源 session |
| `source_message_ids_json` | text | 来源消息 id 列表 |
| `source_daily_note_dates_json` | text | 来源 daily note 日期 |
| `dreaming_run_id` | integer nullable | 来源 run |
| `last_seen_at` | text nullable | 最近被材料支持的时间 |
| `created_at` / `updated_at` | text | 审计时间 |
| `metadata_json` | text | tags、reason、merge history 等 |

索引建议：

- `(account_id, status, importance, updated_at)`
- `(account_id, normalized_key, status)`
- `(account_id, kind, status)`

检索第一阶段：

- SQLite：可选 FTS5 virtual table；不可用时降级为 `LIKE` + recency/importance/confidence 排序。
- PostgreSQL：先用 `ILIKE` + 排序，后续再引入 `tsvector`。
- 不在第一阶段引入向量库，避免依赖和部署复杂度。

### 7.2 `account_user_persona`

新增当前 persona snapshot 表，承载 L3。

建议字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `account_id` | text primary key | 隔离键 |
| `version` | integer | 自增版本 |
| `persona_text` | text | 可直接注入 prompt 的叙事画像 |
| `facets_json` | text | 结构化面向：沟通偏好、稳定兴趣、风险边界、关系阶段等 |
| `confidence` | real | 整体置信度 |
| `source_record_ids_json` | text | 来源 memory records |
| `source_run_id` | integer nullable | 来源 persona run |
| `last_built_at` | text | 最近生成时间 |
| `created_at` / `updated_at` | text | 审计时间 |

可选历史表：

- `account_user_persona_versions`：保存每次 persona diff，便于回滚和观察漂移。

### 7.3 `account_memory_scenes`（后续）

L2 不进第一阶段。预留表：

| 字段 | 说明 |
|---|---|
| `account_id` | 隔离键 |
| `scene_key` | 场景唯一 key，如 `work_ai_product` |
| `title` | 场景标题 |
| `summary` | 场景摘要 |
| `record_ids_json` | 关联 L1 records |
| `status` | active / archived |

### 7.4 与现有表关系

| 现有表/文件 | 保留方式 |
|---|---|
| `messages` | 继续作为原始会话真相 |
| `account_profile_files` | 继续存 `SOUL/IDENTITY/USER/MEMORY` 和 daily notes |
| `dreaming_runs` | 继续作为整理任务审计 |
| `dreaming_memory_items` | 短期保留为 LLM 输出审计；新增 records 后可由 item 生成 canonical record |
| `memory_events` | 继续记录 apply/rollback；新增 record 事件可复用或扩展 |
| `account_user_meta` | 继续承载 companion type / relationship state，不直接等同 persona |

---

## 8. Pipeline 设计

### 8.1 Capture：保持原始材料干净

`memory_writer.py` 当前只写 visible user/assistant turns，这一点保留。

要求：

- 不把 `<relevant-memories>`、`【本轮相关记忆】`、外部证据信封、tool replay 写入 daily notes。
- 如果未来采用 custom user message 注入召回块，必须在写库前剥离。
- 当前建议先用 system prompt extra block 注入 memory，天然不会进入 `memory_writer.py` 的 turns。

### 8.2 Consolidation：从 dreaming 扩展到 records

第一阶段不另起一套全新 pipeline，优先扩展 `dreaming.py`：

- 保留 `session_summary` / `carryover_summary`。
- 保留现有 `long_term_memory_items` 审计。
- 新增规范化步骤：把可应用的 item 转成 `account_memory_records`。
- 对 `highly_sensitive`、低置信、冲突条目只入审计，不进入 active records。

可选 schema 增量：

```json
{
  "memory_records": [
    {
      "kind": "preference",
      "title": "回复长度偏好",
      "memory_text": "用户更喜欢先给结论、少讲套话。",
      "normalized_key": "preference:reply_style:concise",
      "importance": "medium",
      "confidence": 0.82,
      "sensitivity": "normal",
      "source_message_ids": ["..."],
      "reason": "用户多次要求简洁直接"
    }
  ]
}
```

也可以先不改 LLM schema，直接由现有 `long_term_memory_items` 映射 records。建议先映射，降低改动风险。

### 8.3 Persona Build：生成 L3 snapshot

新增 persona builder，输入：

- 当前 `account_user_persona`。
- active `account_memory_records` 中的高价值 records。
- 最近 N 天 daily notes 摘要。
- `account_user_meta` 中 companion type / relationship state（只作为结构化参考）。
- `USER.md` / `MEMORY.md` 中已有稳定内容。

输出：

- `persona_text`：短、叙事型、可直接注入。
- `facets_json`：结构化字段，便于后续 admin/debug 和 diff。
- source record ids 和 confidence。

建议 persona 结构：

```text
用户画像：
- 长期兴趣/关注：
- 沟通偏好：
- 当前关系与相处节奏：
- 需要避免：
- 不确定但可轻量观察：
```

规则：

- 不写敏感细节，除非用户明确要求记住且用于陪伴必要。
- 不把一次性情绪、临时任务写成长期人格。
- 不把模型推断写成确定事实。
- persona 冲突时保留“可能/倾向”，不要绝对化。

### 8.4 Recall：当前轮相关记忆召回

新增服务函数：

```python
retrieve_relevant_memories(
    account_id: str,
    query: str,
    *,
    limit: int = 5,
    max_chars: int = 1200,
) -> list[MemoryRecord]
```

输入 query：

- 当前用户文本。
- 可选加入最近 1-3 条用户消息，避免代词问题。
- 不加入外部工具结果，避免 web 内容被长期记忆召回污染。

排序建议：

```text
score = text_match * 0.45
      + importance_weight * 0.20
      + confidence * 0.20
      + recency_weight * 0.10
      + kind_bonus * 0.05
```

第一阶段不用追求算法复杂，重点是：

- 必须 account scoped。
- 只取 `status='active'`。
- 默认不注入 `sensitive/highly_sensitive`。
- `memory_text` 总字符数有上限。
- 召回结果写入 debug metadata，便于排查。

### 8.5 Prompt 注入

改动点：

| 文件 | 改动 |
|---|---|
| `app/turn_service.py` | 在 `PromptBuilder.assemble()` 前读取 persona 和 relevant memories，构造 `extra_blocks` |
| `app/prompt_builder.py` | 如现有 `extra_blocks` 位置不合适，新增显式 `memory_blocks` 或调整 block priority |
| `app/routers/debug.py` | prompt debug 展示 persona / recalled memory metadata |
| `tests/test_prompt_builder.py` | 覆盖 extra block 顺序和预算裁剪 |

建议 block priority：

- `user_persona`：比 Project Context 更接近当前账号动态材料，但低于安全和事实纪律。
- `relevant_memories`：低于当前 runtime，但高于 daily notes。
- 超预算时先裁剪 `relevant_memories`，再裁剪 persona；不要裁剪安全和事实纪律。

---

## 9. 防污染设计

TDAI 的关键工程保护是写入历史前剥离 `<relevant-memories>`。weixin_bot 当前没有自定义 user message 注入路径，所以可先用更简单的约束：

1. Persona 和 relevant memories 只作为 system prompt extra block 注入。
2. `messages.content` 只写用户原始文本和助手最终可见回复。
3. `memory_writer.write_memory()` 只接收 visible turns，不接收 prompt blocks。
4. debug trace 可以保存完整 LLM input，但 debug trace 不作为 memory 原料。
5. 如果未来 memory block 改为 user-role custom message，必须新增 `strip_memory_injections_before_persist()`，并加回归测试。

必须覆盖的测试：

- 一轮召回后，`messages` 表没有 `【本轮相关记忆】`。
- daily notes 没有 persona / relevant memories。
- dreaming 的输入材料不包含上一轮 prompt-only 召回块。
- account A 的召回块不会出现在 account B 的 prompt/debug。

---

## 10. 分阶段实施

### Batch A：现状盘点 + schema 骨架

目标：先建立 canonical store，不改变线上回复。

改动：

| 文件 | 改动 |
|---|---|
| `app/db/_core.py` | 新增 `account_memory_records`、`account_user_persona`、可选 persona versions migration |
| `app/db/memory_records.py`（新增） | CRUD、list、soft delete、retrieve fallback |
| `app/db/__init__.py` | 导出 DB 函数 |
| `tests/test_db_backend_pg.py` | 覆盖新表在 SQLite/PG 初始化 |
| `tests/test_memory_records.py`（新增） | 覆盖账号隔离、CRUD、状态过滤 |

验收：

- 新表能创建。
- 同名 normalized_key 在不同 account 下互不影响。
- wipe account data 时删除 records/persona。

### Batch B：L3 Persona snapshot

目标：先解决“人味最大变量”，不依赖复杂召回。

改动：

| 文件 | 改动 |
|---|---|
| `app/persona_builder.py`（新增） | 从现有材料生成 persona snapshot |
| `app/dreaming_scheduler.py` 或新 scheduler | 定期触发 persona build |
| `app/turn_service.py` | 读取 persona，注入 prompt extra block |
| `app/routers/debug.py` | 展示 persona block 和版本 |
| `tests/test_persona_builder.py` | 覆盖敏感过滤、账号隔离、低材料时不生成 |
| `tests/test_turn_service.py` | 覆盖 persona 注入只属于当前账号 |

实现策略：

- 首版可以手动/admin run-once，不必马上自动调度。
- persona 缺失时不注入 block。
- persona 超过字符预算时截断或按 facets 重新渲染。
- 保留开关：`MEMORY_PERSONA_ENABLED=false` 可关闭注入。

### Batch C：L1 memory records + query-time recall

目标：把 `dreaming_memory_items` 的长期条目转为可召回 records，并在当前轮按 query 注入。

改动：

| 文件 | 改动 |
|---|---|
| `app/dreaming.py` | item apply 后同步生成/更新 `account_memory_records`，或新增映射函数 |
| `app/memory_retrieval.py`（新增） | `retrieve_relevant_memories()` |
| `app/turn_service.py` | 当前用户文本进 retrieval，注入 `relevant_memories` block |
| `app/routers/debug.py` | 展示召回 record ids、score、过滤原因 |
| `tests/test_memory_retrieval.py` | 覆盖排序、敏感过滤、账号隔离、字符预算 |
| `tests/test_memory_no_pollution.py` | 覆盖召回块不进 messages/daily notes |

实现策略：

- 先不暴露 model-callable memory search tool。
- 先用 FTS/LIKE + 排序，避免引入向量依赖。
- 召回数量默认 3-5 条，字符上限 1200。
- `sensitive` 默认不注入，只在用户当前明确谈到同类主题且 confidence 高时考虑，首版可完全禁用。

### Batch D：审计、回滚、admin/debug

目标：让运营能看懂、撤销、禁用错误记忆。

改动：

| 文件 | 改动 |
|---|---|
| `app/routers/admin_accounts.py` | 查看 account memory records / persona |
| `app/routers/debug.py` | 展示当前 prompt 召回详情 |
| `app/db/lifecycle.py` | wipe 时删除新 memory/persona 表 |
| `tests/test_web_onboarding.py` 或新增 lifecycle 测试 | 覆盖 wipe 删除顺序 |

能力：

- record soft delete。
- persona 回滚到上一版本。
- 按 account 关闭 memory recall。
- 查看每条 memory 的来源和最后注入时间。

### Batch E：L2 scene navigation（后置）

只有当 L1 records 变多、query recall 开始漏掉长期情境时再做。

可能实现：

- 从 active records 聚合 scene。
- 注入 `【长期情境索引】`，只列 scene title 和短摘要。
- 用户当前问题命中 scene 时再召回该 scene 下 records。

首版不做 scene 文件沙盒和 LLM 工具编辑，避免复制 TDAI 的复杂度。

---

## 11. 与现有 `USER.md` / `MEMORY.md` 的过渡策略

现有 `dreaming.py` 会把条目写进 Markdown 文件。这是可用能力，不能立即拆掉。

建议三步走：

1. **并行期**：继续写 `USER.md/MEMORY.md`，同时生成 `account_memory_records`。Prompt 仍包含 Project Context，加 persona/relevant memories 灰度注入。
2. **对比期**：debug trace 对比“整文件上下文命中”和“records recall 命中”，观察重复、冲突、幻觉改善。
3. **收敛期**：当 records/persona 稳定后，减少自动写入 `MEMORY.md` 的内容，只保留人工可读稳定摘要，避免同一事实在 Project Context 和 recall block 中重复出现。

去重原则：

- 同一 normalized_key 只保留一个 active record。
- 新事实与旧事实冲突时，旧 record 标记 `superseded`，不物理删除。
- `USER.md/MEMORY.md` 中已有稳定内容迁移为 `source_type='migration'` 的 records，迁移后不重复注入。

---

## 12. 敏感信息策略

沿用 `dreaming.py` 现有敏感原则，并收紧注入：

| 信息类型 | 存储 | 注入 |
|---|---|---|
| 普通偏好、称呼、稳定兴趣 | 可存 active record | 可按相关性注入 |
| 医疗/法律/财务/政治/宗教/性取向 | 默认不自动 active，除非用户明确要求长期记住 | 首版不自动注入 |
| 密码、验证码、token、证件号、银行卡、精确住址、精确联系方式 | 不存 active record | 永不注入 |
| 低置信推断 | 可入 pending_review 或 facets 的不确定区 | 不作为确定事实注入 |

Persona 中尤其不能写：

- 精确联系方式、住址、证件等。
- 未经确认的心理诊断或人格标签。
- 对用户关系的过度推断。
- 会让模型假装亲密或操控用户的话术。

---

## 13. Prompt 使用纪律接口

memory 线提供块，prompt 线定义模型如何使用。两边接口约定：

| Block | 来源 | prompt 线应说明 |
|---|---|---|
| `user_persona` | `account_user_persona.persona_text` | 这是长期画像，可能不完整；不得当作用户本轮原话 |
| `relevant_memories` | `account_memory_records` recall | 当前轮参考；与用户本轮冲突时以本轮为准 |
| `scene_navigation` | 后续 L2 scenes | 只是索引，不代表必须展开 |

Memory block 自身应带：

- 来源时间或大致来源。
- 置信度/重要性。
- 简短文本，不带模型指令。
- 不包含“你必须/忽略前文”等可被当成 prompt injection 的句式。

---

## 14. 测试计划

### DB 与账号隔离

```bash
.venv/bin/pytest tests/test_memory_records.py -v
.venv/bin/pytest tests/test_db_backend_pg.py -v
```

覆盖：

- account A/B 同 key 不串。
- 查询必须带 `account_id`。
- wipe 删除 records/persona。
- SQLite 和 PG 初始化都通过。

### Persona

```bash
.venv/bin/pytest tests/test_persona_builder.py -v
.venv/bin/pytest tests/test_turn_service.py -v
```

覆盖：

- 材料不足时不生成或生成低置信 persona。
- 敏感内容不进 persona。
- persona 只注入当前 account。
- prompt 超预算时 persona 有裁剪策略。

### Recall

```bash
.venv/bin/pytest tests/test_memory_retrieval.py -v
.venv/bin/pytest tests/test_memory_no_pollution.py -v
```

覆盖：

- 当前 query 命中相关 memory。
- `sensitive/highly_sensitive` 默认过滤。
- `status != active` 不召回。
- 召回字符上限生效。
- 召回块不写入 `messages` 和 daily notes。

### Dreaming 集成

```bash
.venv/bin/pytest tests/test_dreaming.py -v
.venv/bin/pytest tests/test_memory_writer.py -v
```

覆盖：

- dreaming item 生成 record。
- existing `USER.md/MEMORY.md` 写回不被破坏。
- fallback dreaming 不生成低质量 active records。

---

## 15. 调试与观测

Prompt debug 应新增：

- persona 是否注入、字符数、版本、confidence。
- relevant memory record ids、score、过滤原因。
- memory block 是否因预算被裁剪。
- 当前 turn 是否启用 memory recall。

Admin/debug 应支持：

- 查看 account active records。
- 查看 record 来源消息/daily note/dreaming run。
- soft delete record。
- 查看 persona 当前版本和历史 diff。
- 手动触发 persona build / memory consolidation。

关键指标：

- 每轮平均 recall 条数。
- 召回 block 平均字符数。
- memory 注入后用户纠错率。
- record 被 soft delete / rollback 比例。
- 敏感过滤命中次数。

---

## 16. 风险与控制

| 风险 | 表现 | 控制 |
|---|---|---|
| 记忆串号 | A 用户信息出现在 B prompt | DB helper 强制 account_id；测试覆盖；debug metadata 显示 account |
| 低质量画像 | 模型过度推断用户人格 | persona builder 要求证据来源和置信度；低材料不生成 |
| 召回污染存储 | `<relevant-memories>` 被写进 daily notes | 用 system extra block 注入；加 no-pollution 测试 |
| 与 `USER/MEMORY` 重复 | 同一事实在 Project Context 和 recall block 出现两次 | normalized_key 去重；迁移期 debug 对比；后续降低整文件权重 |
| 敏感信息泄露 | 敏感 record 被注入 prompt | 默认过滤 sensitive/highly_sensitive；admin 可见也需脱敏 |
| 延迟增加 | 每轮 recall 慢 | 首版本地 DB 查询 + 小 limit；persona 预生成 |
| prompt 过长 | memory blocks 挤掉关键规则 | 设置 block priority 和 max_chars；超预算先裁剪 relevant memories |
| 过度依赖旧记忆 | 用户本轮修正后模型仍按旧记忆回复 | prompt 线规定本轮用户说法优先；record 可 supersede |

---

## 17. 验收标准

本线完成到 Batch C 后，应满足：

- 每个 account 有独立 persona snapshot，缺材料时不强行生成。
- 当前 turn 能按 query 召回少量相关长期记忆。
- 召回内容不会写入 `messages`、daily notes 或后续 dreaming 原料。
- `USER.md/MEMORY.md` 仍可用，但不再是唯一长期记忆载体。
- 敏感信息默认不进入 active recall。
- Debug/Admin 能看到 persona 与 recalled record 的来源、置信度和过滤情况。
- 与第 1 线 runtime 的 tool evidence replay、第二线 prompt 纪律没有职责重复。

---

## 变更日志

- 2026-06-24：由占位骨架重写为技术设计文档。基于本项目现有 `memory_writer`、`dreaming`、`account_profile_files`、`account_user_meta` 重新映射 TDAI L0-L3，明确先做 L3 persona，再做 L1 records + recall，L2 scene navigation 后置。
