# Campaign 人设 v1 技术设计（乙女向优先 · 甜宠健康向）

> 状态：**代码已落地（改动 #1–#7），待运营建活码**。全量回归 1208 passed。落地方案（人设内容、营销素材）见 [campaign_persona_marketing_playbook_v1](../product/campaign_persona_marketing_playbook_v1.md)。
> 关联：[campaign_codes_technical_design](campaign_codes_technical_design.md)、[agent_mission_and_orchestration_design](agent_mission_and_orchestration_design.md)、[agent_self_prd](../product/agent_self_prd.md)

## 0. 范围与既定决策

给营销活码（`campaign_codes`）补齐 **5 套恋爱/宝妈向人设**（SOUL + 名字 + mission），使运营能建出"进链接即被指定 AI 角色接待"的注册码。两个高风险点已拍板：

| 决策点 | 结论 | 影响 |
|---|---|---|
| 首条开场白是否按人设定制 | **v1 不定制**。首条沿用全局常量 `ONBOARDING_WELCOME_TEXT`，人设从第 2 条起靠强制 SOUL + `onboarding_script_variant` 引导语差异化 | `turn_service.py:894-908` 发送分支**零改动**，风险最低 |
| 恋爱人设与关系阶段基调冲突 | **中性化 `agent_self_state` 阶段文案**（去掉"像朋友一样/保持克制和分寸/不用急着交心"友谊措辞），不做 per-persona 分叉 | 改共享模块、影响全量账号，但纯文本无逻辑，低风险；竹马"从小认识"残留轻微违和，接受 |

`onboarding_script_variant` 经核实是**自由文本、无白名单**，会被逐字注入 onboarding prompt（`onboarding.py:238`）。**v1 直接把整段中文引导话术写进该字段（零代码）**；playbook 里 `otome_v1`/`mom_v1` 这类"代号"写法作废——写代号会当成垃圾指令注入。

**不在本期范围**：per-persona 首条开场白、variant→脚本注册表、relationship_state 阶段推进阈值调整、`romance_roleplay` companion_type 进 prompt。

---

## 1. 现状核对（架构审查结论）

| 子系统 | 关键事实（file:line） | 对本设计的含义 |
|---|---|---|
| SOUL 注册 | `_SOUL_TEMPLATES` 是手工白名单元组 `("blank","xiaotaiyang","xiaoyueya","ju")`（`user_profiles.py:21`），key→文件全文 | 新增人设必须在此元组追加 key；否则 `_validate_soul_preset_key`（`campaign.py:105-111`）拒绝活码 |
| SOUL 渲染 | `_render_soul_template`（`user_profiles.py:572-575`）只 `.format(name_clause, user_clause)` | 模板正文任何**字面 `{`/`}`** 会 `KeyError`/`ValueError` 崩溃 —— 落文件前必须逐一核对 |
| SOUL 注入 | `_CONTEXT_BLOCK_LIMITS` SOUL=3000 / IDENTITY=1500 / MISSION=1500（`prompt_builder.py:192-200`），超限**静默截断** | 5 个模板均需 ≤3000 字（现草稿约 500 字，安全） |
| 性别假设 | 全代码层**无** AI 性别假设、无称谓改写 | 带"他"的男性人设可安全上线 |
| onboarding 菜单 | `onboarding.py` 的 `PERSONA_PRESETS` 是"用户自选人设菜单"，与营销强制人设无关 | 营销人设**不进**该菜单，**不改** onboarding.py |
| mission 注册 | `MISSION_TEMPLATES`（dict，事实源）+ `_TEMPLATE_ORDER`（list，展示序）必须**手动同步**（`mission_registry.py:61-87`） | 新增 mission 两处都要改；`_validate_mission_id` 查 dict 自动放行 |
| mission 注入 | `bar` 字段**无运行时消费方**；真正进 prompt 的是 prose 文件正文 + `short_label`/`inquiry` | prose 文件是模型实际读到的内容，`bar` 仅元数据 |
| 关系基调 | `agent_self_state.py:25-72` 硬编码 icebreaking→"破冰/保持克制和分寸/不用急着交心"、acquainted→"像朋友一样"、deep_bond 标签"密友"，**无 per-persona 钩子** | 见 §3 中性化 |
| 死文件 | `soul_templates/chaochao.md`、`xixi.md` 磁盘存在但未注册、无引用 | 顺手清理（可选，见 §6） |

---

## 2. 改动清单（变更面）

按依赖顺序，共 **5 处代码/数据 + 2 处测试**：

| # | 文件 | 动作 | 类型 |
|---|---|---|---|
| 1 | `app/soul_templates/{peiyan,shenyan,qiyue,lushian,jiangye}.md` | 新增 5 个 SOUL 模板文件（内容见 playbook §3/§4） | CREATE · 数据 |
| 2 | `app/user_profiles.py:21` | `presets` 元组追加 5 个 key | EDIT · 1 行 |
| 3 | `app/mission_templates/{heartbeat_moments,seen_moments}.md` | 新增 2 份 mission prose | CREATE · 数据 |
| 4 | `app/mission_registry.py:61-87` | `_TEMPLATE_ORDER` + `MISSION_TEMPLATES` 各追加 mission_003/004 | EDIT · 数据常量 |
| 5 | `app/agent_self_state.py:25-64` | 中性化 `_STAGE_LABELS` + `_render_trust` 文案 | EDIT · 共享模块（仅文本） |
| 6 | `tests/test_mission_registry.py:9,35` | 更新 order 断言为 4 个 id + 补 003/004 字段/prose 用例 | EDIT · 测试 |
| 7 | `tests/test_agent_self_state.py` | 更新受影响的阶段文案断言 | EDIT · 测试 |

**活码本身不写代码**：评审通过后由运营经 `POST /admin/campaign-codes` 逐条创建（`code`/`ai_name_preset`/`soul_preset_key`/`mission_id`/`onboarding_script_variant` 引导语），是运行时数据，不入库迁移、不进代码。

**无 schema 迁移**：mission/SOUL 是代码常量与资源文件；`campaign_codes`/`account_campaign_attribution` 表结构已存在，不动 `_MIGRATIONS`。

---

## 3. 关键设计：中性化关系阶段文案（改动 #5）

**目标**：`agent_self_state` 只表达"关系推进程度 / 可袒露的深浅"，**把关系类型（朋友/恋人）交给 SOUL 决定**，使恋爱人设、宝妈人设、通用陪伴三类都不违和。纯文本替换，不动 `compute_dominant_need` 等任何逻辑与分支结构。

### 3.1 `_STAGE_LABELS`（`agent_self_state.py:25-29`）

标签去友谊化，同时消除与 `relationship_state` "密友 vs 挚友/热恋" 的口径分歧：

```python
_STAGE_LABELS = {
    "icebreaking": "初识",
    "acquainted": "熟络",
    "deep_bond": "亲近",   # 原"密友"——关系类型无关，恋爱/陪伴通用
}
```

### 3.2 `_render_trust`（`agent_self_state.py:54-64`）

删除 `保持克制和分寸`/`不用急着交心`/`像朋友一样`，改为关系类型无关表述：

```python
def _render_trust(stage: str) -> str:
    stage_label = _STAGE_LABELS.get(stage, stage)
    if stage == "icebreaking":
        return (
            f"- 关系阶段：{stage_label}（关系还在早期，节奏不用急）\n"
            "- 此刻的主导心境：争取被记住、被信任——真诚、不讨好，"
            "按你和ta关系本来的样子相处。"
        )
    return (
        f"- 关系阶段：{stage_label}（关系在升温，可以更有连续性）\n"
        "- 此刻的主导心境：争取被认同、被信任——真诚、不讨好。"
    )
```

`_render_growth`（`:67-72`）文案本身不含友谊措辞，仅标签经 `_STAGE_LABELS` 变为"亲近"，无需改函数体。

> **标签分面（落地时确认，刻意保留）**：中性化后 `agent_self_state` 注入的标签是 **初识/熟络/亲近**，而 `relationship_state._STAGE_VIEW_LABELS`（admin 运营视图）与 `prompts/user_meta_relationship.py`（天级 LLM 分类器提示词）仍是 **破冰/相识/挚友热恋**。这是**不同受众的刻意分面**——陪伴体自身 prompt 走中性口径避免恋爱人设违和，运营/分类面保留原措辞不影响判定（分类器输出的是枚举值 icebreaking/acquainted/deep_bond，标签仅辅助理解）。三处不强行统一。

### 3.3 已知延后项（v1 不修，仅记录）

**竹马（祁越）早期基调残留违和**：新账号系统默认 `relationship_stage=icebreaking`，中性化后注入"初识（关系还在早期）"，与祁越 SOUL 里"从八岁认识"的设定仍有轻微张力。要累计 >30 条入站消息（`relationship_state.py:63`）才升到 acquainted。

- **决策**：v1 **不修**（用户明确决定）。SOUL 优先级高于本 block，通常能盖过；且这是选「中性化」而非「per-persona 基调分叉」的已知取舍。
- **触发升级条件**：若上线后竹马类人设违和明显（用户反馈/对话崩设），再评估升级为 per-persona 基调分叉——给 campaign/SOUL 增加"关系基调"维度，`agent_self_state` 按基调选文案。届时改动面见 §0 决策表 B 选项。
- 霸总（裴衍）、覆面（沈砚）是"新识"设定，与 icebreaking 天然吻合，无此问题——**首发优先选这两套可完全规避**。

---

## 4. 数据流（不变量确认）

```
运营建活码（code+soul_preset_key+ai_name_preset+mission_id+引导语）
  → 用户经链接注册：billing.py:2142 apply_campaign_code_attribution(increment_usage=True)
      ├─ write_campaign_attribution 写死快照（account_campaign_attribution，写入即定型/幂等）
      ├─ write_ai_name_to_identity(account_id, name)   # 先写名字进 IDENTITY.md
      └─ apply_soul_preset(account_id, soul_preset_key) # 再渲染 SOUL（回读名字填 {name_clause}）
  → 首条 inbound：发全局 ONBOARDING_WELCOME_TEXT（v1 不定制）
  → 第 2 条起 LLM turn：
      ├─ SOUL.md（强制人设）/IDENTITY.md（名字）/MISSION.md（分配的使命 prose）→ project_context
      ├─ onboarding_script_variant 引导语 → 【本账号专属引导语】块
      └─ agent_self_state block（中性化后的关系阶段 + 使命进度）
```

**账号隔离（核心不变量）**：所有写入（快照、SOUL/IDENTITY/MISSION、user_meta）均以 `account_id` 为键；本设计**不新增任何跨账号查询或全局写入**。改动 #5 是无状态纯渲染函数，读的是入参 stage，天然隔离。

**mission 绑定**：`campaign.mission_id`（乙女→`mission_003`，宝妈→`mission_004`）→ 快照 → `assign_mission_if_absent` 优先命中快照 mission_id → 写 `MISSION.md`。新增 mission 登记进 registry 后，校验（`_validate_mission_id`）与注入全自动泛化，无别处硬编码 mission_001/002。

---

## 5. 测试方案

### 5.1 聚焦自动化测试（改动直接覆盖）

```bash
.venv/bin/pytest tests/test_mission_registry.py tests/test_agent_self_state.py \
  tests/test_db_campaign.py tests/test_onboarding.py tests/test_agent_context.py -v
```

- `test_mission_registry.py`：`:9` 断言改为 `["mission_001","mission_002","mission_003","mission_004"]`；`:35` prose 存在性校验会要求两份新 prose 落盘且含 `# MISSION`；补 003/004 字段用例。
- `test_agent_self_state.py`：grep 现有断言里的 `"破冰"`/`"密友"`/`"像朋友一样"`/`"不用急着交心"` 子串，逐一改为新文案。**这是改动 #5 的回归闸门**。
- `test_db_campaign.py`：确认新 `soul_preset_key`（peiyan 等）经 `_validate_soul_preset_key` 放行、`mission_003/004` 经 `_validate_mission_id` 放行。

### 5.2 全量回归（触及共享基础设施，按 CLAUDE.md 策略）

改动 #5 落在 prompt 注入共享路径，提交前跑全量：

```bash
.venv/bin/pytest tests/ -v
```

### 5.3 手工端到端验证

```bash
# 建一个带 peiyan 活码的调试账号（debug 路径 increment_usage=False），走注册→首轮
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "在吗"
# 核对组装后的 system prompt
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account <账号>
```

验收点：① SOUL/IDENTITY/MISSION 三块均注入且自称为"裴衍"；② 关系阶段块显示"初识…"新文案、无"像朋友一样"；③ 无客服腔、无红线内容；④ 各 block 未触发 `...[已截断]`。

### 5.4 落文件前静态自查

- 5 个 SOUL 模板逐一确认**无字面花括号**（防 `.format` 崩溃），且同时含 `{name_clause}` 与 `{user_clause}`。
- 字数：SOUL ≤3000、IDENTITY ≤1500、MISSION prose ≤1500。

---

## 6. 可选清理（独立小改，不阻塞本期）

- 删除死文件 `app/soul_templates/{chaochao,xixi}.md`（未注册、无引用），或明确注释归档。
- 对齐 `deep_bond` 标签口径：`agent_self_state`（本期改为"亲近"）与 `relationship_state._STAGE_VIEW_LABELS`("挚友/热恋" admin 视图)服务不同界面，可保留差异；如需统一，另开小 PR。

建议本期**不夹带**这两项，保持改动最小、评审面清晰。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| #5 影响全量账号的 prompt | 纯文本、无逻辑分支变更；全量回归 + 手工核对；出问题 git revert 单文件即回滚 |
| SOUL 模板花括号导致渲染崩溃 | §5.4 静态自查 + `test_agent_context.py` 渲染用例覆盖 |
| mission order 断言遗漏 | §5.1 聚焦测试即暴露 |
| 竹马人设早期基调残留违和 | 已知接受项；v1 先发新识型人设（裴衍/沈砚）看数据，竹马观察后再评估是否升级为 per-persona 分叉 |
| 活码 `onboarding_script_variant` 误填代号 | 本文明确废弃代号写法；运营建码 SOP 写清"填整段引导语或留空" |

## 8. 交付顺序建议

1. 先落 #5（中性化）+ 测试 #7，单独验证共享改动安全 → 可先行合并。
2. 再落 #1/#2（SOUL）+ #3/#4（mission）+ 测试 #6 → 人设资产就绪。
3. 运营建活码（数据）→ 拿链接接营销素材。
4. 首发 **裴衍 + 祁越**（playbook §8），对比漏斗数据再放量。
