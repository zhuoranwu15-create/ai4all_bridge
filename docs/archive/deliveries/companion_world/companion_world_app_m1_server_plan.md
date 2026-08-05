# Companion World App M1 服务端需求评审与开发计划

更新时间：2026-07-31
状态：**已完成并归档。S1–S6 已合入 `main` 并上线。** 当前能力以
[`companion_world_app_prd.md`](../../../products/mingchan/capabilities/companion_world_app_prd.md) 和
[`app_api_handoff.md`](../../../products/mingchan/app_api_handoff.md) 为准；第三方数据处置、
`sample_dialogue` 与灰度能力等剩余项已转入
[`companion_world_app_followups.md`](../../../backlog/products/mingchan/companion_world_app_followups.md)。

> 归属：`product:zhaoxi`。
> 输入：客户端仓库《Companion World M1 服务端需求积压 V0.2》与《AI 陪伴 App 私人平行世界 PRD V1.1》
> （原件冻结在 [`archive/alignments/`](../../alignments/companion_world_m1_server_requirements_v0_2_client.md)）。
> 产品口径：[朝夕相伴 App 端 PRD](../../../products/mingchan/capabilities/companion_world_app_prd.md)。
> 技术权威：[Companion World 3.0 ADR](../../../architecture/products/mingchan/companion_world_3_0_refactor_design.md)。
> 评审基线：`main@7d0943d`，当前最大迁移号 `m0046`。

---

## 1. 评审结论（TL;DR）

客户端提出的 4 个「硬阻断」中，**3 个成立**（CAP-001、BOOT-001、CUSTOM-001），
1 个需要改判（COMPAT-001 降级为客户端盘点项 + 服务端一个字段）。
NAME-001 成立但**实现方案应大幅简化**。此外服务端自查发现 **4 个客户端清单未覆盖的问题**，
其中 BOOT-003 与 SEC-001 是新的 M1 阻断。

| 分类 | 条目 |
| --- | --- |
| **M1 阻断（必须先做）** | CAP-001 公开 capability、BOOT-001 account=null 可恢复 Session、**BOOT-003 legacy 运行时回填**、CUSTOM-001 结构化自建角色、**SEC-001 自建人设直通 Soul** |
| **M1 联调前收口** | NAME-001（降本方案）、CAND-001、CONV-001、TURN-001、TIME-001、ERROR-001 + **ERR-002**、**IDEM-001**、CONTRACT-001（缩范围）、BOOT-002（补测试） |
| **改判** | COMPAT-001 → 客户端盘点为主，服务端只提供分平台最低版本字段 |
| **已拍板** | BOOT-003 老用户按新用户走选择页并带入微信角色（D-A）、SEC-001 自由文本走 LLM 审查+改写（D-B），见 §4.1 |
| **待拍板** | 见 §4.2（Q1–Q13） |
| **确认后置** | FEED-201（P2 阻断）、MEDIA-201、CONFIG-201 |

**核心判断**：M1 主干（世界初始化、4 位候选确认、居民列表、按 `conversation_id` 的私聊与历史）
在生产确实可用，客户端「不能只按 `app_api_handoff.md` 直接开工」的结论成立。但阻断点的性质
不同于客户端的理解 —— 真正的高风险不是命名子流程，而是**老用户被推进新手引导**（BOOT-003）
和**用户自由文本直通 Soul**（SEC-001）。

---

## 2. 逐条评审

### 2.1 CAP-001 公开 Companion World capability — ✅ 成立，M1 阻断

**证据**：`app/products/zhaoxi/api/app.py:151-169`，`features` 只有 `voice_input`。
世界各能力由 7 个独立 flag 门控，关闭时统一返回 `not_found`(404)
（`companion_world.py:193-208`、`companion_world_visits.py:57`、`companion_world_mailbox.py:40`、
`app_notifications.py:42`）。客户端无法区分「明确关闭」与「资源不存在」。

**采纳，但有两点修正**：

1. **capability ≠ flag 一一映射**。真人聊天的**读**由 `COMPANION_WORLD_P1_ENABLED` 门控，
   只有**发送**由 `COMPANION_WORLD_HUMAN_CHAT_ENABLED` 门控
   （`companion_world_human_chat.py:73-75` 复用 `_require_world_session`，
   `:226-228` 单独判 write）。因此不能给一个 `human_chat` 布尔，应给
   `human_chat_send`，读能力随 `resident_world`。否则客户端会在开读关写时整块隐藏历史。
2. `resident_lifecycle` 应映射 `..._LIFECYCLE_COMMIT_ENABLED`（真正会产生 offline 的开关），
   不是 `..._EVALUATION_ENABLED`（只跑评估、不落地）。

**交付形状**（字段只加不改，旧客户端继续读 `voice_input`）：

```json
{
  "features": {
    "voice_input": false,
    "resident_world": true, "world_feed": true, "app_notifications": true,
    "resident_lifecycle": true, "mailbox": true, "world_visits": true,
    "human_chat_send": true
  },
  "client_contract_version": "2026-07-26",
  "server_time": "2026-07-26T12:00:00+08:00",
  "minimum_supported_version": "0.1.0",
  "minimum_supported_version_by_platform": {"ios": "0.1.0", "android": "0.1.0"}
}
```

capability 只反映公网 App API 可用性，**不暴露** scheduler、阈值、安全策略或 flag 名。
需补契约测试：逐个 flag 翻转 → config 字段同步 + 对应端点行为一致，避免生产开关变了而
公共 config 未同步。

### 2.2 BOOT-001 未完成居民确认时也能恢复 Session — ✅ 成立，M1 阻断

**证据**：`app/products/zhaoxi/api/app.py:245-250` → `_account_for_user`（`:121-130`）在
无 binding 时抛 409 `account_not_ready`。P1 开启后新用户恒无 account（`:193-199`），
所以新用户杀进程后**无法**用 `/me` 恢复。

**采纳，选方案 1（扩展 `/me`），不新增 `/app/bootstrap`**：

- 兼容性无损：`/auth/session` 已经会返回 `account: null`，`/me` 返回 `account: null`
  不引入任何新的客户端形态，只是把 409 改成 200。
- 少一个端点、少一套错误码与测试面。

`/me` 新形状（增量字段）：

```json
{
  "status": "ok",
  "platform_user": {"id": "...", "phone_masked": "138****0000"},
  "account": null,
  "world": {"id": "uni_...", "status": "active", "onboarding_state": "selecting"},
  "server_time": "2026-07-26T12:00:00+08:00"
}
```

- `world` 在 capability 关闭或用户尚无 world 时为 `null`；**读不建**（用 `get_home_universe`，
  不用 `get_or_create`），建世界仍只由 `POST /worlds/home/bootstrap` 负责。
- `account.status == "disabled"` 仍返回 403，语义不变。
- 401 只表示 Session 失效；`account_not_ready` 从 `/me` 消失。
- 保持 `Cache-Control: no-store`（已有）。

### 2.3 BOOT-003 legacy 老用户入口 — 🆕 服务端自查发现，M1 阻断（**产品已拍板 2026-07-26**）

> **产品决策（D-A，2026-07-26）**：**不做「回填即 confirmed」**。微信端老用户在 App 侧
> **按新用户对待**，唯一区别是不需要重新注册（手机号已存在，直接登录）。登录后同样进入
> **选择角色页**：4 位预设候选 + **带入其在微信端的既有角色**，可叉掉不喜欢的候选、可自建新角色。
> 微信侧既有角色若已有名字/性格（onboarding 设过）则沿用；若没有，直接以「微信里的 bot 好友」
> 身份带入并给出默认展示名。
>
> 因此下文原方案（锁内回填 → 直接 `confirmed` → 0 候选）**作废**，改为下面的 D-A 方案。

**D-A 方案**：`bootstrap_home` 在 `preparing` 分支内、快照 4 位预设候选**之外**，
额外把该真人在朝夕的 active legacy account 映射为 `origin=legacy` 居民带入本世界，
world 仍推进到 `selecting`（不是 `confirmed`）。

```text
world = lock_universe(...)
if world.onboarding_state == "preparing":
    legacy_ids = list_active_account_ids_for_user(platform_user_id)   # 已有原语
    for account_id in legacy_ids[:MAX_ACTIVE_RESIDENTS]:
        repo.ensure_legacy_resident(world, account_id)                # 已有原语，幂等
    if legacy_ids:
        repo.mark_universe_legacy_primary(world.id, legacy_ids[0])    # 只写锚，不置 confirmed
    templates = validate_initial_catalog(...)                          # 老用户同样发 4 位候选
    for template in templates:
        repo.ensure_candidate(world.id, template, "preset")
    world = repo.set_universe_onboarding_state(world.id, "selecting")
```

配套改动：

1. `mark_universe_legacy_confirmed` 现在同时写 `legacy_primary_account_id` **并**置
   `confirmed`（`persistence/companion_world.py:174-190`）。需拆成只写锚的
   `mark_universe_legacy_primary`。`legacy_primary_account_id` 必须继续写——
   它是微信侧主动消息路由的锚（`has_legacy_primary_weixin_route`）。
2. `list_candidates` 硬过滤 `r.origin <> 'legacy'`（`:442`），所以 legacy 居民
   **不会**出现在候选列表里。bootstrap 响应需要新增 `existing_residents` 字段
   （已 active 的 legacy 居民），否则客户端在 selecting 页看不到微信角色。
3. **展示名**：`COALESCE(p.display_name, t.name)`（`:592`）在 `profiles.display_name`
   为空时会回落到 legacy 模板名 `'legacy'`——必须给一个产品默认名（见待确认 Q3）。
4. `> 10` 个 active binding 的用户：只带入前 10 个，其余不带入并记录告警
   （不再像原方案那样整体拒绝——老用户不应被挡在门外）。
5. `dismiss_unselected_candidates` 已排除 `origin <> 'legacy'`（`:499`），
   所以确认时不会误伤微信角色。

回归用例：老用户首次 bootstrap → `selecting` + 4 候选 + N 位 legacy 居民；
重复 bootstrap 幂等不重复带入；无微信绑定的纯新用户行为不变；
确认阶段叉掉全部预设候选但保留微信角色 → 成功且不触发 `resident_capacity_empty`。

---

<details>
<summary>原评审方案（已被 D-A 取代，保留作评审记录）</summary>


**客户端清单未覆盖，但比 NAME-001 严重。**

**证据**：`domain/companion_world/service.py:69-81` 的 `bootstrap_home` 只做
「建世界 → `preparing` 则快照 4 位候选 → `selecting`」，**没有任何 legacy 回填**。
回填只存在于一次性离线脚本 `scripts/backfill_companion_world.py:129-137`
（带 `--created-before` cutoff），且只有该脚本会调用
`ensure_legacy_resident` / `mark_legacy_world`（全仓库搜索确认无其他调用方）。

**后果**：任何**未被那次 cutoff 覆盖**的老用户（cutoff 之后新绑定微信的用户、脚本报错跳过的
用户、后续新增的微信侧用户）在 App 调用 bootstrap 时，会走 `preparing` 分支被创建 4 位候选、
推进到 `selecting` —— 即**老用户被当成新用户走新手引导**，且其微信侧历史 account 永远不会
映射成 legacy 居民，用户在 App 里看不到既有关系。这直接违反 ONB-02 与验收项
「老用户不依据 `account != null` 跳过 world bootstrap；legacy resident 正确恢复」。

**方案**：把回填从「一次性脚本」提升为「bootstrap 内的幂等前置步骤」。

在 `bootstrap_home` 的世界行锁内、快照候选之前插入：

```text
world = lock_universe(...)
if world.onboarding_state == "preparing":
    legacy_ids = repo.list_active_legacy_account_ids(platform_user_id)
    if legacy_ids:                        # 老用户：回填并直接 confirmed，不产生候选
        for account_id in legacy_ids[:MAX_ACTIVE_RESIDENTS]:
            repo.ensure_legacy_resident(world, account_id)
        world = repo.mark_legacy_world(world.id, legacy_ids[0])   # 内部置 confirmed
    else:                                  # 真正的新用户：快照 4 位候选
        ...现有逻辑...
```

- 与离线脚本共用同一套 repository 原语和锁序，行为一致；脚本保留用于批量预热与对账。
- `> 10` 个 active binding 的用户按脚本同款规则拒绝（`active_bindings_over_capacity`），
  返回稳定错误码而不是静默截断。
- 必须新增回归：老用户首次 bootstrap → `confirmed` + N 个 `origin=legacy` 居民 + 0 候选；
  重复调用幂等；已 backfill 用户不受影响。

</details>

### 2.4 COMPAT-001 旧客户端兼容 — ⚠️ 改判为客户端盘点项

生产 `COMPANION_WORLD_P1_ENABLED` 已开启且 `account=null` 已在线上返回一段时间，
「P1 开启导致旧包白屏」的**风险窗口已经过去**，不是 M1 新引入的阻断。

服务端在 M1 只承担一件事：随 CAP-001 一起提供
`minimum_supported_version_by_platform`（iOS/Android 分平台）。
其余（已分发版本清单、`account=null` 行为盘点、升级门 UI）属客户端职责。
若客户端确认从未分发过任何旧包，记录为「不适用」并保留证据即可。

**不做**：服务端按客户端版本 Header 做行为分流。它会把版本判断散进业务代码，
与「gating 少加开关、默认全量」的既有取舍冲突；有真实灰度需求时再单独立项（CONFIG-201）。

### 2.5 CUSTOM-001 结构化自建角色 + SEC-001 人设直通 — ✅ 成立，M1 阻断

**证据**：`api/companion_world.py:117-130` 的 `CreateResidentPayload` 只有
`template_id | name + persona_hint`；`:418-435` 把 `persona_hint` **原样**拼进 `SOUL.md`
（`soul = hint or 默认文案`），随后作为居民人设持久化。

这里有两个层面的问题，必须一起解决：

- **CUSTOM-001（契约）**：缺 `avatar_ref`、`relationship_type`、`relationship_label`、
  `personality_traits`、`persona_summary`、`client_request_id`。客户端只能把关键设定存本地，
  违反 CROLE-01/02。
- **SEC-001（安全，🆕 服务端自查）**：用户自由文本**未经任何审核**直接成为 LLM system prompt。
  这同时是 prompt 注入面和内容安全面，且直接违反 PRD CROLE-11「高风险设定不由自由文本
  自动放行」与 §11.3 内容治理。**这一条本身就足以阻断 M1**，即使不做结构化字段。

**方案：两步式契约（采纳客户端建议）**

```text
POST /worlds/home/resident-drafts/preview
  ← {name, avatar_key, relationship_type, relationship_label?, personality_traits[], style_note?}
  → {normalized_summary, ai_identity_notice, safety, draft_token(短期), draft_id}

POST /worlds/home/residents
  ← {draft_token, client_request_id}       # 自建路径只接受这两个字段
  → {candidate|resident}
```

要点：

> **产品决策（D-B，2026-07-26）**：安全策略取**「LLM 审查 + 改写」**，不取「枚举白名单 +
> fail closed」。用户填写的**各类自由文本**在**进入系统时**统一做一次 LLM 调用，判断是否
> 存在安全风险；有风险则**在尽可能保留用户原意的前提下改写**，去掉风险部分，而不是整体拒绝。

1. **统一自由文本清洗器**（新增 `app/platform/moderation/text_sanitizer.py`，跨产品可复用）：
   输入 `{text, field_kind, context}` → 输出 `{verdict: pass|rewritten|rejected,
   sanitized_text, risk_categories[], notice}`。落地在**入口一次**，下游（Soul 渲染、
   DTO 回显、持久化）一律只用 `sanitized_text`。
   复用现有 `app/platform/moderation/llm_review.py` 的 provider/JSON 解析约定，新增 rewrite 提示词。
2. **覆盖字段**：`persona_hint` / `style_note` / `relationship_label` / `display_name`
   （含 `selections[].display_name`）/ 自建角色摘要。凡是用户自由输入且会进入 prompt 或
   公开展示的文本，都过同一个清洗器。
3. **Soul 仍由服务端模板化渲染**：以清洗后的文本 + 结构化字段渲染 `SOUL.md` / `IDENTITY.md`，
   不把原始输入整段直接当人设。预览返回的 `normalized_summary` 与最终持久化必须来自
   同一段渲染代码（保证「所见即所存」）。
4. **结构化字段仍然要**（CUSTOM-001 与 D-B 不冲突）：`relationship_type`、
   `personality_traits`、`avatar_ref` 继续做受控取值，理由是它们要驱动渲染与后续筛选，
   不是安全兜底手段。D-B 改变的是**自由文本**的处理方式，不是取消结构化。
5. `draft_token` 短期有效（建议 30 分钟）、单次消费、绑定 `platform_user_id`；
   `draft` 中存的是**清洗后**文本。
6. 兼容：保留 `template_id` 单字段路径（预设居民）不变；旧的 `name + persona_hint`
   裸路径在 M1 **下线**，改为经 preview → draft_token 的路径。

### 2.6 IDEM-001 自建居民创建幂等 — 🆕 服务端自查

**证据**：`domain/companion_world/service.py:176-219`。
- `selecting` 期：第一次调用成功创建 custom candidate；重试（网络超时后重发）命中
  `:190-191` 抛 `custom_candidate_limit_exceeded`(409)，客户端**无法区分**「我刚才成功了」
  和「我超限了」。
- `confirmed` 期：无任何去重，重复点击/重试会创建 **N 个** custom template + N 位居民，
  直到撞 10 位上限。

`client_request_id` 因此不是「锦上添花」，是 CUSTOM-001 的必需部分：
以 `(platform_user_id, client_request_id)` 唯一索引承载，命中已存在记录时返回**原结果 + 200**，
不报错。

### 2.7 NAME-001 服务端建议实例姓名 — ✅ 成立，但**方案降本**

**证据**：`api/companion_world.py:226-237` 的候选 DTO 无 `suggested_display_name` /
`persona_key` / `naming_status`；`db/_core.py:2376-2394` 的 `universe_residents` 表
**没有任何展示名列**，展示名只在激活时写进 runtime account 的 `profiles.display_name`
（`service.py:119-129`）。所以候选阶段确实无处安放实例名。

**评审意见：不要做「命名子流程」，做「运营配置 + 快照持久化」。**

客户端文档把它描述成一个可能失败、需要 `naming_status=unavailable` 兜底的**子流程**
（暗示 LLM 生成）。但 D30 已冻结「首版仅把模板工作名替换为人设相容的实例展示名」——
这不需要任何在线生成：

- 运营为每个模板配置 3–5 个已审核候选名（沿用 `scripts/import_companion_world_presets.py`）。
- bootstrap **首次快照候选**时，用确定性 hash（`universe_id + template_id + name_pool_version`）
  选名，**连同 `naming_version` 一起写入 `universe_residents` 新列**。
- 之后每次 bootstrap 都从该行直接读回 → 天然满足「一旦生成即固定、重复 bootstrap /
  换设备 / 重装返回同值、不随算法升级静默变化」。

这个方案消除了整类失败模式：`naming_status` 只在**模板未配置名池**时才是 `unavailable`，
不存在运行期命名失败。客户端的本地小姓名池仍按契约保留为兜底，但实际只会在
「旧服务端 / 运营漏配」时触发。

候选 DTO 新增：`suggested_display_name`、`naming_version`、`persona_key`、`naming_status`。
命名不可用**不得**让 bootstrap 整体失败，仍返回真实候选。

**同时必须补服务端校验**：`confirm` 当前对 `display_name` 只做
`strip()` + 非空（`service.py:122-124`）与 Pydantic `max_length=50`
（`api/companion_world.py:108`），没有字符白名单、控制字符过滤与内容审核。
客户端提交值不可信 —— 这一项与 SEC-001 同批修。

### 2.8 CAND-001 候选稳定身份与 preview — ✅ 成立

`persona_key` 随 NAME-001 一并加（`character_templates` 新列，跨模板版本稳定）。

`long_summary` / `sample_dialogue`：**建议 M1 只做 `long_summary`**，
`sample_dialogue` 进 P2。理由：示例对话需要单独的内容审核与版本管理流程，
而角色预览页（PRD 5.3 第 3 步）用一句话身份 + 长介绍 + 三个标签已能成立。
两者都必须是运营录入并经审核的静态内容，不返回 `persona_seed_json`、内部 resident id
或 runtime account id（现状已满足）。

### 2.9 BOOT-002 bootstrap 幂等 — ✅ 基本具备，补测试与文档

`get_or_create_home_universe` 靠 `UNIQUE(owner_platform_user_id)` 保证幂等
（`persistence/companion_world.py:98-121`），`preparing` 分支只走一次
（`service.py:75-79`），`confirmed` 不会退回 `selecting`，
`preset_catalog_not_ready` 已有（`service.py:49-67`，校验 rank 必须恰好为 (1,2,3,4)
且四项元数据齐全）。缺的是**老用户分支**（见 BOOT-003）与回归用例。

### 2.10 CONV-001 会话列表时间与排序 — ✅ 成立，低成本

**证据**：SQL 已按 `updated_at DESC, id DESC` 排序且 cursor 正确锚定
（`persistence/companion_world.py:711-745`），但 DTO 不返回任何时间
（`api/companion_world.py:253-265`）。

新增：`last_message_at`（无消息为 `null`）、`sort_time`（= `updated_at`，明确 cursor 排序键）、
`can_send`（= `state == "active"`）、`read_only_reason`（`resident_offline` | `null`）。
服务端顺序为权威，客户端不按本地时间重排。

⚠️ 实现注意：当前列表对**每一行**单独查一次最近消息（`:749-753` 循环内调用
`list_app_conversation_messages_before`），已经是 N+1；加 `last_message_at` 时应把
preview 与时间**一次查出**，不要再加一次查询。

### 2.11 CONV-002 AI 未读语义 — ⚠️ 需产品拍板，建议做最小 read cursor

**证据**：`persistence/companion_world.py:755` 硬编码 `item["unread"] = 0`，
DTO 注释也写明「P1 unread 恒为 0」（`contracts.py:113`）。

客户端说得对：不能长期保留「字段存在但永远为 0」。两个选项：

| 选项 | 成本 | 影响 |
| --- | --- | --- |
| A. 明确 M1 不做 AI 未读 | 0，改文档 + capability | 违反 PRD CONV-03（P0 要求展示未读） |
| B. 最小 read cursor | 1 个迁移列 + 1 个端点 + 计数查询，约 1 人日 | 满足 CONV-03，且未读点是「对话 Tab 允许的克制提示」 |

**建议 B**：`ai_conversations` 加 `last_read_message_id INTEGER`，
新增 `POST /ai-conversations/{id}/read {last_message_id}`，
`unread_count` = 该 runtime account 中 `id > last_read_message_id` 的 assistant 消息数。
不引入新表、不改 turn 链路。若产品选 A，必须在 capability 与 OpenAPI 中显式声明。

### 2.12 TURN-001 `no_reply` 与幂等重放 — ✅ 成立，低成本

**证据**：`api/companion_world.py:646-655` 无条件返回 `reply` 对象，
`no_reply=true` 时 `reply.text` 为 `None`；幂等命中路径（`:591-601`、`:636`）
`message_id` 恒为 `null`，因为 `get_duplicate_reply`（`app/db/accounts.py:346-361`）
只 SELECT `content`。

冻结为：

- `no_reply=true` → `reply: null`；否则 `reply.text` 必须为非空字符串。
- 幂等重放返回**原持久化** `reply.message_id`：把 `get_duplicate_reply` 改为同时返回
  `message_id`（同一行已有该列，不加查询）。
- `deduplicated=true` 不改变业务结果，客户端不得新建第二个气泡。
- `client_message_id` 在同一 conversation 内唯一，跨 conversation 可复用（现状已是
  `app:{conversation_id}:{client_message_id}`，保持不变）。

同一改动应同步到 legacy `/chat/turn`（`api/app.py:376-386`），避免两条链路语义分叉。

### 2.13 TIME-001 公开时间统一带时区 — ✅ 成立，范围比客户端描述的小

实测：Feed（`api/companion_world.py:268-273`）、visits（`companion_world_visits.py:74`）、
human chat（`companion_world_human_chat.py:90`）、通知（`app_notifications.py:57-61`）、
mailbox（`companion_world_mailbox.py:53-57`）**都已**转成 `+08:00`。

只有两处透传 DB naive 时间：
- `GET /ai-conversations/{id}/messages` 的 `created_at`（`api/companion_world.py:563`）
- legacy `GET /chat/messages` 的 `created_at`（`api/app.py:297`）

修法：复用现成的 `_public_time` 工具。⚠️ legacy `/chat/messages` 的改动会影响
已分发客户端的解析，需与客户端确认解析容错后再改；世界端点先改。

### 2.14 ERROR-001 稳定错误信封 + ERR-002 命名空间漏洞 — ✅ 成立（含 🆕 发现）

现状：领域错误已有统一 `{code, request_id, server_time, message}` 信封
（`api/companion_world.py:173-183`、`658-664`），错误码表齐备（`:43-87`）。

**ERR-002（服务端自查，M1 前必修）**：`_validation_error_handler`（`:667-690`）
只对 `/v1/worlds/`、`/v1/conversations` 等**旧前缀**生效。但产品路由同时挂在三个前缀
（`manifest.py:16-22`）：`/v1`、`/api/v1/products/zhaoxi`、`/v1/products/zhaoxi`。
客户端如果按 [`app_client_brief.md`](../../../products/mingchan/app_client_brief.md)
的推荐使用**规范前缀**，422 校验失败会拿到 FastAPI 默认的 `{"detail": [...]}` 而不是
统一信封 —— 即「推荐路径」的错误形状与「兼容路径」不一致。

修法：把前缀判断改成后缀/路由级判断（例如匹配去掉三种已知前缀后的路径），
并补一条对三个前缀都断言信封形状的测试。

其余采纳项：
- 新增 `feature_disabled`（HTTP 404）区别于资源不存在的 `not_found`，与 CAP-001 配套。
- M1 主链路错误码冻结：`preset_catalog_not_ready`、`resident_capacity_empty`、
  `resident_capacity_exceeded`、`resident_selection_invalid`、`conversation_read_only`、
  `turn_in_progress`、`rate_limited`（全部已存在，只需写进契约文档并加冻结测试）。

### 2.15 CONTRACT-001 OpenAPI snapshot — ⚠️ 采纳但缩范围

客户端要求导出 `/api/v1` OpenAPI JSON 并在 CI 做 breaking-change 检查。
**问题**：当前世界端点全部返回裸 `dict`、没有 `response_model`
（`api/companion_world.py` 各处），直接导出得到的 schema **只有路径和请求体，响应是空对象**，
客户端 CI 无法检测任何响应侧 breaking change —— 会产生「有契约门禁」的错觉。

**方案（分两步）**：
1. **M1**：只为主链路 8 个端点补 `response_model`
   （`/app/config`、`/me`、`bootstrap`、`resident-candidates`、`residents`、
   `residents/confirm`、`conversations`、`ai-conversations/{id}/{messages,turn}`），
   再导出 snapshot。其余端点先在文档层维持现状。
2. 提供 `scripts/export_openapi.py` + 提交 `docs/products/zhaoxi/openapi/app_v1.json`
   + 一条 CI 测试断言「导出结果与提交的 snapshot 一致」（改契约必须显式更新 snapshot）。

生成 DTO 不替代客户端领域模型；`app_api_handoff.md` 继续作为说明文档。

### 2.16 P2 及后续

| 条目 | 结论 |
| --- | --- |
| **FEED-201** 主人删除/隐藏动态 | ✅ 成立，P2 阻断。底层已有 `delete_post`（`domain/companion_world/feed.py:91`）但**无任何调用方**，只需补 owner-scoped 幂等 HTTP 路由 + 访客可见性一致性测试。成本低。 |
| **MEDIA-201** 媒体 | ✅ 同意后置。首个 Feed 版本保持纯文字。 |
| **CONFIG-201** 分平台灰度 | 部分提前：`minimum_supported_version_by_platform` 随 CAP-001 交付；完整灰度/维护态后续立项。 |

---

## 3. 开发计划

每批一个 PR，独立可回滚。`main` 受保护：必须走 PR，且**必须等「PostgreSQL 档全量测试」
全绿**才能合入（PG 是唯一生产后端，是唯一阻塞门禁）。SQLite 档同样在 PR 上跑，但只作
参考信号、不阻塞合入——它守的是本地开发体验，不是生产正确性。

### S1 — 发布门底座 — ✅ 已交付（2026-07-26）

| 项 | 实际改动 |
| --- | --- |
| CAP-001 | `api/app.py` 新增 `_companion_world_capabilities()`（7 项 capability，子能力与 `resident_world` 与运算）+ `client_contract_version` / `server_time` / `minimum_supported_version_by_platform`；`/app/config` 加 `Cache-Control: no-store` |
| BOOT-001 | `_account_for_user` 拆出 `_optional_account_for_user`；`/me` 允许 `account: null`，新增只读不建的 `world` 摘要与 `server_time`；`/chat/*` 仍走 409 那条 |
| BOOT-003（D-A） | `service.py::bootstrap_home` 锁内新增 `_carry_in_legacy_resident`：带入微信既有角色 → 只落 legacy primary 锚 → **照发 4 位候选 → selecting**；`confirm_residents` 的「至少一位」改为按世界最终有人算 |
| 默认展示名 | 迁移 `m0047` + `get_or_create_legacy_template` 自愈，把哨兵模板名 `'legacy'` 改为「来自微信的Bot」；一处改动覆盖 6 个 `COALESCE(p.display_name, t.name)` 站点，不回写 `profiles`（不影响微信侧 AI 自称） |
| ERR-002 | `_validation_error_handler` 改为先剥挂载前缀（长前缀优先）再判路由，三前缀统一信封 |
| 新错误码 | `feature_disabled`(404) 入 `_ERROR_STATUS`，4 个 `_require_*` 门控改用 |
| 附带 | `scripts/backfill_companion_world.py` 标注「不再用于常规上线」——它会替用户跳过选择页，与 D-A 冲突 |

**迁移 `m0047`**（仅一行 UPDATE，无回填、无锁表风险）。S2 已占用 `m0048`，S3 顺延为 `m0049`。

新增回归：`test_companion_world_service.py` 6 例（带入/默认名/幂等/纯新用户/老用户零选择确认/
新用户零选择被拒）、`test_companion_world_api.py` 6 例（capability 映射、子能力不越父、
`/me` 恢复 selecting、`/me` 不建世界、三前缀 `existing_residents`、三前缀 422 信封）。

### S2 — 自建角色与安全 — ✅ 已交付（2026-07-26）

| 项 | 实际改动 |
| --- | --- |
| 迁移 `m0048` | `character_templates` 加 `persona_key`、`long_summary`、`relationship_type`、`personality_traits_json`（纯加列）；新增 `resident_drafts`（`draft_token` 唯一、结构化字段、已渲染 `persona_seed_json`、`safety_json`、`status`、`expires_at`）+ `(platform_user_id, client_request_id)` 偏唯一索引 |
| 受控取值 | 新增 `domain/companion_world/persona_catalog.py`：7 类关系（`custom` 必带 label）、24 个性格标签（1–3 个）、4 个受控头像 key、长度上限与展示名字符白名单，单点定义并由 `GET /worlds/home/resident-options` 下发 |
| CUSTOM-001 | 新增 `POST /worlds/home/resident-drafts/preview`（→ `draft_token`，TTL 30 分钟）；`POST /worlds/home/residents` 自建路径改为只收 `draft_token + client_request_id`；裸 `name + persona_hint` 路径下线（多余字段 422） |
| SEC-001 | `render_persona` 服务端模板化渲染 SOUL/IDENTITY：用户自由文本只进「说话风格」一节，AI 身份声明与边界恒定不可覆盖；preview 与落库共用同一次渲染结果（所见即所存） |
| D-B 清洗器 | 新增跨产品 `app/platform/moderation/text_sanitizer.py`：入口一次 LLM 判定 → 改写为最终值（Q7 不提示「内容已被修改」）；红线 3 类硬拒绝 `content_rejected`(422)；LLM 不可用 fail closed `content_review_unavailable`(503)。因领域层不得 import `app.platform`（`test_layer_boundaries.py`），清洗落在 API 入口，与「入口一次」口径一致 |
| IDEM-001 | 幂等键与草稿同表：`consume_resident_draft` 的 `WHERE status='open'` 让并发双发只有一笔能赢，重放按 `(platform_user_id, client_request_id)` 回放原结果 200 |
| 名称校验 | `confirm_residents` 对用户自定义 `display_name` 加 `is_valid_display_name`（CJK/假名/谚文/拉丁白名单，拒 emoji 与控制/零宽/RTL 字符）；沿用模板名时不设限 |
| 配置 | 新增 `COMPANION_WORLD_ASSET_BASE_URL`（留空=相对路径），避免把站点域名硬编码进代码 |

新增回归：`test_companion_world_persona_catalog.py` 23 例、`test_text_sanitizer.py` 13 例、
`test_companion_world_api.py` 新增 11 例（受控取值下发、改写落库、人设渲染、幂等回放、
草稿 owner 隔离与单次消费、过期、硬拒绝、fail closed、自造枚举、旧路径下线、控制字符名）、
`test_companion_world_schema.py` 新增 2 例（m0048 幂等、draft token 唯一 + owner 隔离）。

**遗留（需运营跟进）**：自建角色目前只能复用首发四张头像资产。扩充头像库是运营任务
（补静态资产 + 在 `AVATAR_KEYS` 加一行），不阻断 M1 联调。

### S3 — 命名与候选身份 — ✅ 已交付（2026-07-26）

| 项 | 实际改动 |
| --- | --- |
| 迁移 `m0049` | `character_templates` 加 `name_pool_json`、`name_pool_version`；`universe_residents` 加 `suggested_display_name`、`naming_version`。纯加列无回填，既有候选两列为 NULL → DTO 表现为 `naming_status=unavailable`，与「模板未配名池」同一条退化路径 |
| 选名算法 | 新增 `domain/companion_world/naming.py`：`normalize_name_pool`（3–5 个、去重、复用展示名白名单）、`select_suggested_name`（`sha256(universe_id \| template_id \| name_pool_version)` 取模）、`naming_status`。**刻意不用内建 `hash()`**——CPython 对 str 按进程加盐，重启即变 |
| NAME-001 | `bootstrap_home` 与 `_create_resident_locked` 在 `ensure_candidate` 前选名并随 INSERT 落库；`get_or_create_candidate_resident` 是 `ON CONFLICT DO NOTHING`，快照只写一次，之后一律读回。运营换名池换版本不影响已快照的世界 |
| 默认展示名 | `confirm_residents` 未传 `display_name` 时依次取 快照实例名 → 模板工作名；`_create_resident_locked` 的 confirmed 期直接激活同样口径 |
| CAND-001 | 候选 DTO 增 `suggested_display_name`、`naming_version`、`persona_key`、`naming_status`、`long_summary`；`persona_seed_json` 与内部 resident/runtime id 仍不外泄 |
| 运营配置 | `import_companion_world_presets.py` 支持 `name_pool` / `name_pool_version` / `long_summary` / `persona_key`。这四项属**可原地更新的运营元数据**（生产四模板已上线、id 不能换，否则名池永远配不上去），不参与人设内容的不可变判定；`persona_key` 允许从空补上但非空后不许改值。manifest 未提供的字段一律不动，重放老 manifest 不会抹掉已配名池；`name_pool` 与 `name_pool_version` 必须成对出现 |

**仓库不预设名池值**：与 Feed 窗口时间同惯例，名池是运营内容，由部署评审提供。未配置时
候选 `naming_status=unavailable`、bootstrap 正常成功，客户端按契约回落本地兜底名池。

新增回归：`test_companion_world_naming.py` 21 例（名池校验 6、选名确定性/三输入敏感/
**跨进程稳定**（子进程 `PYTHONHASHSEED` 校验）/状态映射 5、service 快照写一次与换名池不变、
未配名池仍成功、跨真人世界隔离、确认默认名两级回落 4、API DTO 新字段与 unavailable 2、
运营导入名池落库/原地更新/重放不抹除/`persona_key` 不可变 5）；`test_companion_world_schema.py`
新增 2 例（m0049 幂等、候选选名快照只落一次）。

验收结论：同一 world/candidate 多次 bootstrap / 换设备 / 重装返回同值 ✅；
模板未配名池时 `naming_status=unavailable` 且 bootstrap 仍成功 ✅。
两档全量：SQLite 1652 passed（2 例为本机 sqlite 3.26 缺 `DROP COLUMN` 的既有失败）、
PG 1684 passed / 9 skipped。

### S4 — 会话与 turn 契约冻结 — ✅ 已交付（2026-07-26）

| 项 | 实际改动 |
| --- | --- |
| CONV-001 DTO | `ConversationSummary` 增 `last_message_at`、`sort_time`，并把 `can_send` / `read_only_reason` 做成派生属性（`state == 'active'` 才可发；只读原因码恒为 `resident_offline`）。`sort_time` 与 `last_message_at` **刻意分开**：没聊过的居民 `last_message_at=null`，但仍有稳定排序键（会话 `updated_at`，也是分页 cursor 锚），不伪造消息时间 |
| CONV-001 N+1 | 新增 `db/accounts.py::summarize_app_conversations`：两条 SQL 批量取回整页的「最近一条 + 未读数」，取代逐会话查询。session scope 与 `list_app_conversation_messages_before` 同源（只认 `__app_active__` 与 `__app_active__:<id>`，微信/Web 会话不进预览与未读），message 与 session 双向约束 `account_id` |
| CONV-002（Q9 选 B） | 迁移 `m0050` 给 `ai_conversations` 加 `last_read_message_id`（纯加列无回填，既有会话 NULL = 一条都没读过）；新增 `POST /ai-conversations/{id}/read` 与真实 `unread`。游标**只前进不回退**，且向该会话 App scope 内真实最新一条收敛——客户端传超大 id 不会把未来消息预标已读。**刻意不动 `updated_at`**：它同时是列表排序键与 cursor 锚，标记已读不应让会话跳序 |
| TURN-001 | `_turn_data` 冻结响应形状：`reply` 要么是 `{text, message_id}` 且 `text` 非空，要么整体 `null`，不再有「有 reply 对象但 text 为 null」的中间态。`get_duplicate_reply` 拆出 `get_duplicate_reply_record`（返回 `content` + `message_id`），重放回放**原持久化** `message_id`，客户端据此认出同一条消息不新建气泡；legacy `/chat/turn` 同步 |
| TURN-001 根因修复 | `agent_runtime/turns/service.py` 的 `response_metadata` 从未包含 `reply_message_id`，而世界端与 legacy 两处都在读它 —— **首次 turn 的 `message_id` 一直是 null**（不只是重放路径不一致）。补上该键后两条路径才真正对齐 |
| TIME-001（Q10 选「同批改」） | 世界端 `/ai-conversations/{id}/messages` 与 legacy `/chat/messages` 的公开时间统一显式带 `+08:00`（`api/app.py::_public_time`），客户端不再按设备时区猜 |

**迁移 `m0050`**（单列 `INTEGER`，无回填、无锁表风险）。四处硬编码 schema head 断言随之
从 49 上调到 50：`test_companion_world_schema.py`、`test_companion_world_m3_storage.py`、
`test_companion_world_m5_storage.py`、`test_multi_product_isolation.py`。

新增回归：`test_companion_world_conversation_contract.py` 11 例（DTO 时间/排序/可发送性、
只读会话拒发、**列表查询次数恒定**（monkeypatch 计数，退回逐行查询即失败）、预览与未读不跨
居民、未读只计 assistant、游标只进且收敛、标记已读不改排序、`/read` 越权与不存在同为
`conversation_not_found` + 参数 422 + 匿名 401、重放同 `message_id`、`no_reply` 回 `null`、
世界端时间带偏移）；`test_app_api.py` 新增 2 例（legacy 时间偏移、legacy 重放同
`message_id`）；`test_companion_world_schema.py` 新增 1 例（m0050 幂等）。

验收结论：列表排序/分页无重复遗漏（标记已读不改 `sort_time`）✅；只读会话 `can_send=false`
且发送被拒 409 `conversation_read_only` ✅；同一 `client_message_id` 重放返回同一
`message_id` ✅。两档全量：SQLite 1661 passed（2 例为本机 sqlite 3.26 缺 `DROP COLUMN` 的
既有失败）、PG 1698 passed / 9 skipped。

### S5 — 契约门禁 — ✅ 已交付（2026-07-26）

| 项 | 实际改动 |
| --- | --- |
| 响应模型 | 新增 `api/contracts.py` 作为冻结契约的单一来源：A 类扁平（`AppConfigResponse`、`MeResponse`）+ B 类泛型信封 `WorldEnvelope[T]` 与各 `data` 模型 + `WorldErrorEnvelope`。**10 条**主链路路由补 `response_model`：计划原列的 8 项（`/app/config`、`/me`、bootstrap、resident-candidates、residents、residents/confirm、conversations、`ai-conversations/{id}/{messages,turn}`）+ S4 冻结的 `/ai-conversations/{id}/read`——漏掉它会在 snapshot 上留个洞。世界类端点另经 `WORLD_ERROR_RESPONSES` 声明 401/403/404/409/422/429 的错误信封 |
| 导出脚本 | `scripts/export_openapi.py`：只留客户端直连的 `/v1/*`，排除 `/v1/products/*` 与 `/api/v1/products/*`（同批路由的另外两个挂载点，否则条目翻倍）；按引用传递闭包裁剪 `components.schemas`，admin/bridge 模型不混入；确定性序列化（`sort_keys` + 2 空格 + 结尾换行），否则字典顺序抖动会让 snapshot 比对随机红。支持 `--check` / `--stdout` |
| snapshot | 提交 `docs/products/zhaoxi/openapi/app_v1.json`：49 条路径、52 个 schema |
| CI 门禁 | `tests/test_app_openapi_contract.py` 15 例，三条门禁：① 提交的 snapshot 与实时导出逐字节一致；② 10 条主链路的 200 响应 schema 必须非空（防退回裸 `dict`）；③ **真实响应体与声明 schema 逐层键集比对**——`response_model` 会静默过滤未声明字段，模型漏写一个就悄悄少返回，这条把它变成测试红 |
| 文档 | `app_api_handoff.md` 增 §2.3 契约 snapshot 段与 §7 错误信封声明说明；`app_client_brief.md` 增 snapshot 使用说明 |

**生产安全**：导出脚本只 `create_app()` + `app.openapi()`，不触发 FastAPI startup，因此不会像
其它脚本那样触发 `init_db()` 去动 `.env` 指向的生产库；脚本 docstring 已写明。

门禁有效性已实测：临时从模型里删掉 `ConversationItem.can_send`，门禁 ③ 报
「契约多出 `['can_send']`」、门禁 ① 同时报 snapshot 过期，二者都咬住。

**已知边界**：只有这 10 条端点有真实响应 schema；信箱/访问/通知/真人会话等其余端点当前只
冻结路径与请求体，属计划 §2.15 的既定分步，不是遗漏。

回归：本批只跑聚焦测试——契约门禁 15 passed；受影响文件（`test_app_api.py`、
`test_companion_world_api.py`、`test_companion_world_conversation_contract.py`、
`test_companion_world_service.py`、`test_multi_product_isolation.py` 等）81 passed；
相邻世界/分层用例 50 passed / 14 skipped。**S1–S5 的双档全量回归留到合入前一次性跑**。

### S6 — 「我的」Tab 收尾 — ✅ 已交付（2026-07-26）

M1 范围内仍是缺口的服务端小项（不阻塞联调，正式版必需）：ME-01 / ME-06/07 / ME-10。

| 项 | 实际改动 |
| --- | --- |
| 迁移 m0051 | `platform_users.avatar_key` 加列；新表 `account_deletion_requests`（注销执行流水：`status='executed'` + `executed_at` + `purge_stats_json` 删除行数快照；**不加唯一约束**，同一真人注销后可重新注册再注销，每次独立一行）与 `app_notification_preferences`。纯加列 + 新表，无回填、无锁表；两后端幂等 |
| ME-01 Profile | `GET /me/profile-options` 下发受控头像表与昵称限额（客户端不硬编码枚举）；`PATCH /me/profile` 支持单独改昵称或头像（`null` = 本次不改，不是清空）。头像走受控 key（`user_01..user_08`），**未知 key 直接 422 不回落默认**；昵称先过字符白名单再过 D-B `text_sanitizer`（`FIELD_USER_NICKNAME`），清洗器不可用时 fail closed 503。`/me` 与更新响应统一返回 `display_name`/`avatar_key`/`avatar_ref` |
| ME-06/07 注销 | 只保留 `POST /me/account/deletion`：按 Q14 口径**立即清除**聊天记录与相关记忆，无冷静期故无查询/撤销接口。body 必带 `confirm:true`（不可撤销的破坏性操作不接受空 body），`reason_code` 受控取值。**参数校验一定跑在清除之前**——否则会出现「数据删完了才报 422」。执行完吊销该真人全部 session，客户端就地登出 |
| ME-10 通知偏好 | `GET/PATCH /notifications/preferences`（B 类信封），取值 `standard` / `quiet`，缺行等价默认值故无需回填。`quiet` 压制**全部** AI 主动通知——App inbox 里没有一条属于 PRD §3.3 说的「必须提示的安全/邀请/账号事件」，不做分类豁免 |
| 门控落点 | 安静模式在 `dispatch_proactive_text` 建 outbound 行**之前**拦截，落 `cancelled` + `app_inbox_quiet_hours_preference`；`AppInboxAdapter.can_deliver` 同步收口（`_select_route` 因此不再选 App 路由）。刻意不在 `deliver()` 里抛错：那会被上游 `except` 记成 `failed`，让运营看到的失败率被用户偏好污染 |
| CHAT-04 | 无服务端缺口。M1 口径为**打开**：`voice_input` 由 `ASR_API_KEY` 是否配置驱动，属运营配置项（见 §4.3-Q15） |
| 契约 | `api/contracts.py` 增 `ProfileOptionsResponse`、`ProfileUpdateResponse`、`AccountDeletionResponse`、`NotificationPreferencesResponse`，`MePlatformUser` 补 3 个字段；snapshot 重新导出（53 路径 / 57 操作）；`MAIN_CHAIN_OPERATIONS` 从 10 条扩到 **15 条**，并新增一条门禁 ③ 用例覆盖「我的」Tab 真实响应体 |

**注销清除范围**（`app/products/zhaoxi/application/account_deletion.py`，删数据只有这一个
入口便于审计）：该真人在本产品下每个 runtime account 走 `unbind_and_wipe_account`（聊天原文、
session、L1/L2 与 dreaming 记忆、账号 profile 文件、提醒/承诺），**外加**逐账号 wipe 抓不到的
`universe_memory_facts`（L3 挂在 universe 上，漏删就成了「删了聊天记录但 AI 还记得你」）、
`ai_conversations`、`app_notifications`；居民置 `dismissed`、世界回 `preparing`（世界行受
`UNIQUE(owner_platform_user_id)` 约束不能删）。**刻意保留**：涉及第三方的真人会话/来访/邀请
（见 §11.2-9）、财务审计流水（`wipe_account_data` 既定规则）、`platform_users` 行本身（手机号
可重新注册成全新用户，而不是被永久占用）。

**运营交付物**：`user_01..user_08` 头像 PNG 需按 `COMPANION_WORLD_ASSET_BASE_URL`
前缀落到资产站，与既有居民头像同一条路径。key 列表已在代码中声明，缺图不影响接口可用。

回归：聚焦测试——`tests/test_app_me_settings.py` 16 例（Profile / 注销 / 通知偏好三组各自
的**跨用户越权**用例；注销组直接断言聊天原文、L3 事实、`ai_conversations` 真的清空且另一
真人分毫未动）；SQLite 档 110 passed / 3 skipped，`AI4ALL_TEST_DB=postgres` 档 48 passed
（迁移触及持久化，两后端均验证）。**全量双档回归留给 PR CI 一次性跑。**

> Q14 拍板后的返工记录：冷静期实现（`GET`/`DELETE` 路由、`cooling_days`、
> `mark_due_deletion_requests`、`(platform_user_id, app_id)` 部分唯一索引）已整体删除，
> m0051 建表语句就地改写而非追加 m0052——该迁移尚未合入 main，线上无任何库停在 v51。
> 返工中测试抓到一个真实缺陷：非法 `reason_code` 原先会先清完数据再报 422，已把入参校验
> 提到清除之前。

### P2 批次（不进 M1）

FEED-201：`POST /worlds/home/feed/posts/{id}/delete`（或 `DELETE`）owner-scoped 幂等路由 +
访客 Feed 立即一致性测试 + 审计保留策略明确。约 2 人日。

### 3.1 排期与依赖

```text
S1 ──┬── S2 ── S5
     ├── S3 ──┘
     └── S4 ──┘
```

S1 是所有后续批次的前置（capability 与错误信封）。S2/S3/S4 相互独立，可并行。
总计约 **16–19 人日**（不含 CONV-002 选 B 的 1 人日与 P2 的 2 人日）。

### 3.2 测试策略

- 按 AGENTS.md：S1/S4 触及请求路由与跨模块契约 → 跑全量；S2/S3 含迁移与 schema → 跑全量
  且 **SQLite + PostgreSQL 双档**。
- 新增聚焦用例集中在 `tests/test_companion_world_service.py`、`test_companion_world_api.py`、
  `test_companion_world_conversations.py`、`test_app_api.py`。
- 账号隔离是硬不变量：每个新端点都必须有「跨用户越权返回 not_found / 空页」的用例。
- 文档改动后必须跑 `tests/test_documentation_links.py`。

### 3.3 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| BOOT-003 在生产误判老用户（binding 状态漂移） | 锁内重读 binding；`>10` 拒绝而非截断；先用离线脚本 dry-run 盘点受影响用户规模 |
| `/me` 行为变更影响已分发客户端 | 只把 409 改 200 且新增字段，不改既有字段语义；上线前用生产只读探测确认无客户端依赖 409 |
| S2 迁移在 PG 上锁表 | 全部为加列 + 新表，无回填；按既有 expand/contract 惯例分步 |
| capability 与真实 flag 漂移 | 契约测试逐 flag 翻转；capability 从 `settings` 单点读取，禁止硬编码 |
| 下线 `persona_hint` 影响联调中的客户端 | 客户端 M1 尚未开工该路径；在 S2 PR 说明中同步客户端 |

---

## 4. 待拍板事项

### 4.1 已拍板（2026-07-26）

| 编号 | 决策 | 影响 |
| --- | --- | --- |
| **D-A** | 微信老用户不做「回填即 confirmed」；按新用户走选择角色页（免注册、可叉候选、可自建），并带入微信侧既有角色（有名字/性格则沿用，否则以微信 bot 好友身份带入） | 重写 §2.3；`mark_universe_legacy_confirmed` 拆分；bootstrap 新增 `existing_residents` |
| **D-B** | 用户自由文本统一走一次 LLM 安全审查，有风险则**改写**（保留原意、去掉风险部分），不是枚举白名单 + 拒绝 | 重写 §2.5；新增跨产品 `text_sanitizer` |

### 4.2 Q1–Q13 拍板结果（2026-07-26，全部已定）

**A 组 — 老用户流程（D-A 收尾）**

| # | 问题 | 结论 |
| --- | --- | --- |
| Q1 | 微信带入的角色在选择页**能不能被叉掉** | **不能**。它是既有关系不是候选；UI 展示为「已在你的世界里」。 |
| Q2 | 微信角色**是否占名额**、算不算「至少保留 1 位」 | **占名额、算数**。老用户叉掉全部 4 位预设也能通过确认。 |
| Q3 | 微信角色**没设过名字**时的默认名 | **「来自微信的Bot」**（产品指定）。 |
| Q4 | 一个真人在微信侧的 active 账号数 | **产品保证只有 1 个**。实现只带入最早绑定的那一个；出现多个视为异常，带入第一个并告警，不静默全量带入。 |

**B 组 — 自由文本安全（D-B 收尾）**

| # | 问题 | 结论 |
| --- | --- | --- |
| Q5 | 改写救不回、必须直接拒绝的红线 | 保留一小类**硬拒绝**：指名复刻具体真人 / 已故亲友纪念 / 未成年人形象。其余一律改写放行。 |
| Q6 | LLM 审查调用失败/超时 | **fail closed**，返回可重试错误，不放行原文。 |
| Q7 | 改写后是否让用户二次确认 | **是**。preview 直接返回改写后文案作为最终值（所见即所存）；不弹「内容已被修改」提示，避免对抗性试探。 |
| Q8 | 自建角色受控取值表 | 由服务端按 PRD CROLE 章节起草一版，S2 开工前冻结。 |

**C 组 — 契约**

| # | 问题 | 结论 |
| --- | --- | --- |
| Q9 | AI 会话未读 | **做**最小 read cursor（`last_read_message_id` + read 端点 + `unread_count`）。 |
| Q10 | legacy `/chat/messages` 时间加 `+08:00` | **改**，与世界端点同批；客户端需确认解析容错。 |
| Q11 | 分平台最低版本首版值 | iOS/Android 均 `0.0.0`（不拦），字段先上线。 |
| Q12 | 候选 `sample_dialogue` | **推迟到 P2**，M1 只做 `long_summary`。 |
| Q13 | NAME-001 名池 | 先落字段与快照机制；名池未配时退化为模板名，不阻断。 |

### 4.3 S6 遗留待拍板（2026-07-26 新增）

| # | 问题 | 服务端已按什么假设交付 | 谁来拍 |
| --- | --- | --- | --- |
| Q14 | 注销生效后**实际清除**哪些数据、由谁执行、留存多久 | ✅ 已拍板（2026-07-26）：**注销立即删聊天记录和相关记忆**，无冷静期、不可撤销。S6 原先交付的冷静期设计已按此重做（删掉 `GET`/`DELETE` 路由与 `cooling_days` 字段，m0051 建表语句同步改写）。第三方相关数据的处置仍未决，转 §11.2-9 | 法务 + 产品 |
| Q15 | `voice_input` 是否在 M1 打开 | ✅ 已拍板（2026-07-26）：**打开**。无代码缺口——该位取自 `is_asr_available()`，运营在生产 `.env` 配置 `ASR_API_KEY` 并重启即翻 `true`，客户端不用发版。**这是本批次唯一未完成的落地动作** | 产品 |

---

## 5. 明确不做

- 服务端按客户端版本 Header 做业务分流（见 §2.4）。
- 在线 LLM 命名子流程（见 §2.7）。
- Feed 媒体上传/审核/EXIF 清理链路（MEDIA-201，P2+ 单独评审）。
- 为世界端点全量补 `response_model`（M1 只做主链路 8 个）。
- 新增 `/app/bootstrap` 端点（用扩展 `/me` 替代）。
