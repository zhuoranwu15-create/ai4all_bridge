# Dreaming 记忆压缩与长期记忆技术设计

更新时间：2026-05-28

本文承接 [记忆与上下文 PRD](../product/memory_prd.md)、[Agent Context Files 与记忆机制](agent_context_files.md) 和 [Conversation Orchestrator 主对话场景技术设计](conversation_orchestrator_design.md)，详细定义 AI4ALL Phase 1 的 Dreaming 机制。

检索式记忆是另一条复杂链路，暂不在本文展开；本文只覆盖 session 压缩、跨 session 延续、长期记忆片段生成、自动应用、debug 观测和回滚。

## 1. 设计结论

本版本 Dreaming 明确走 LLM 调用，不再使用 deterministic excerpt 作为最终压缩结果。

核心结论：

- session 压缩必须由 LLM 生成，不能只截取最近几条消息。
- 延续到下一个 session 的 `carryover_summary` 必须由 LLM 生成。
- LLM 输出必须包含两类核心结果：
  - 粗略摘要：用于后续聊天承接，类似 Claude Code compact 后的上下文承接摘要。
  - 长期记忆片段：排除敏感信息后，提取用户方面值得长期记住的信息。
- Dreaming 生成的长期记忆片段没有人工审核环节；系统按规则自动应用或自动跳过。
- 后台保留 Dreaming run、生成片段、跳过原因、diff 和 LLM 输出摘要，用于 debug、质量评估和 prompt 调优。
- 500 轮压缩服务于上下文窗口和对话连续性，不应因为 session 过长就扩大长期记忆；它可以生成长期记忆片段，但应用规则要更保守。
- daily notes 是原始文字化历史材料，不是长期记忆摘要，也不默认注入普通聊天 prompt。

## 2. 产品心智

长期记忆不是“保存所有聊天内容”，而是模拟现实中一个人对好朋友会自然记住的信息。

应优先识别：

- Profile 类信息：用户称呼、语言、沟通偏好、长期偏好、禁忌和稳定身份。
- 人际关系：家人、朋友、同事、伴侣、重要合作对象，以及这些关系对用户的意义。
- 正在经历的事情：生活事件、工作项目、学习目标、长期计划、正在承受的压力和持续关注事项。
- 用户对 AI 的明确要求：希望如何称呼、如何回复、不要提什么、哪些话题需要谨慎。
- 明确纠错：用户指出旧记忆错误、过时或不希望继续保留。

不应进入长期记忆：

- 密码、验证码、token、银行卡、身份证、住址、精确联系方式等高敏感信息。
- 一次性任务、短期寒暄、调试消息、失败回复、模型内部 prompt 或系统 trace。
- 未经用户明确表达的猜测、诊断、标签化判断。
- 医疗、法律、财务、政治、宗教、性取向等高敏感信息的细节；除非用户明确要求长期记住，也默认跳过自动应用，只保留后台 debug 记录和原因。

## 3. 触发场景

### 3.1 每日 4 点 Dreaming

目标：

- 结束旧 active session。
- 用 LLM 压缩旧 session 和业务日 daily notes。
- 生成新 session 的 `carryover_summary`。
- 生成长期记忆片段，并自动应用符合规则的片段。

流程：

```text
business-day boundary reached
-> load active session visible messages
-> load business-day daily notes raw archive
-> load current MEMORY.md / USER.md
-> LLM dreaming compaction
-> write session_summary and carryover_summary
-> write dreaming memory items for debug
-> auto-apply eligible memory items
-> close old session
-> create new __account_active__ session with LLM carryover_summary
```

### 3.2 500 轮 session 压缩

目标：

- 避免 active session 过长导致上下文窗口和成本失控。
- 用 LLM 生成 `session_summary` 和 `carryover_summary`。
- 开启新 session，并让新 session 能承接旧上下文。

策略：

- 500 轮压缩可以生成长期记忆片段，但不应激进应用。
- 默认只自动应用 `importance=high`、`confidence>=0.85`、`sensitivity=normal` 的片段。
- 其他片段保留为 `skipped` debug 记录，不进入 `MEMORY.md`。

流程：

```text
active session turn_count >= configured max_turns
-> LLM session compaction
-> write session_summary and carryover_summary
-> write generated memory items for debug
-> auto-apply only eligible high-confidence items
-> close old session
-> create new __account_active__ session with LLM carryover_summary
```

### 3.3 手动 Dreaming

Admin 或工程调试可手动触发某账号、某 session 或某业务日 Dreaming。

手动触发必须记录：

- actor_type：`admin | system`
- actor_id
- source_session_id
- source_business_day
- prompt_version
- llm_model

## 4. 输入材料

LLM 输入按账号隔离，只能读取同一个 `ai4all_account_id` 的数据。

输入来源：

- `sessions`：当前要压缩的 session metadata、`turn_count`、`business_day`。
- `messages`：该 session 下用户可见的 user/assistant messages。
- `memory/YYYY-MM-DD.md`：业务日 raw daily notes。
- `MEMORY.md`：当前精选长期记忆。
- `USER.md`：当前用户稳定资料和偏好。
- 可选：近期 Dreaming memory items，用于去重和避免重复应用。

不进入 LLM 输入：

- hidden commitment prompt。
- debug trace 全文。
- raw payload 中非必要字段。
- failed outbound、cancelled outbound、未发送给用户的消息。
- 管理员明文查看日志、系统内部 token、密钥或配置。

## 5. 输出模型

LLM 输出必须是可解析 JSON。不得依赖自由文本解析。

推荐 schema：

```json
{
  "session_summary": {
    "rough_summary": "旧 session 的粗略摘要，用于归档和后续压缩输入。",
    "carryover_summary": "新 session 起始上下文，帮助后续聊天自然承接。",
    "open_threads": [
      {
        "text": "用户还在进行或可能继续聊的事项",
        "priority": "high"
      }
    ],
    "tone_notes": "后续回复时需要保持的语气或互动注意点"
  },
  "long_term_memory_items": [
    {
      "operation": "add",
      "target_file": "MEMORY.md",
      "category": "work_project",
      "memory_text": "用户最近正在负责某个持续性的工作项目。",
      "importance": "high",
      "confidence": 0.86,
      "sensitivity": "normal",
      "reason": "这会影响后续多轮聊天中的上下文理解和陪伴。",
      "source_message_ids": ["msg_..."],
      "source_daily_note_dates": ["2026-05-28"]
    }
  ],
  "excluded_sensitive_items": [
    {
      "category": "credential",
      "reason": "高敏感信息，不进入长期记忆"
    }
  ]
}
```

字段约束：

- `rough_summary`：可以稍粗，服务于 session 归档和后续 Dreaming 输入。
- `carryover_summary`：必须短、可直接注入新 session prompt，避免包含敏感细节。
- `long_term_memory_items`：只写用户方面、长期有用的信息。
- `memory_text`：应该是可直接进入 `MEMORY.md` 或 `USER.md` 的简洁条目。
- `source_message_ids`：尽量填充，用于审计和回溯；没有 message id 时用 daily note date。
- `confidence`：0 到 1。
- `sensitivity`：`normal | sensitive | highly_sensitive`；非 `normal` 默认不自动应用，只保留 debug 记录。

## 6. Prompt 设计

### 6.1 System Prompt

```text
你是 AI4ALL 的 Dreaming 记忆整理器。

你的任务不是聊天，而是基于已发生的用户可见对话，为个人 AI 生成可审计的 session 压缩结果和长期记忆片段。

背景：
- AI4ALL 是面向真实用户的个人 AI 陪伴与生活助理。
- 每个账号可能长期陪伴同一个用户，因此需要记住现实中一个好朋友应当记住的信息。
- 长期记忆必须克制、准确、可追溯、可回滚，并且便于后台 debug 调优。

原则：
- 不要编造。只基于输入材料。
- 不要保存高敏感信息、凭证、精确地址、身份证件、银行卡、验证码、token。
- 不要把一次性任务、寒暄、调试消息或失败回复当作长期记忆。
- 对用户明确纠正的信息，要优先用于删除、降权或更新旧记忆。
- 对医疗、法律、财务、政治、宗教、性取向等敏感内容要极其保守；除非用户明确要求长期记住，否则不要作为长期记忆片段。
- 输出必须是合法 JSON，不要输出解释，不要输出 Markdown。
```

### 6.2 User Prompt 模板

```text
请根据以下材料完成 Dreaming 压缩。

触发类型：
{source_type}

目标：
1. 生成旧 session 的粗略摘要 rough_summary。
2. 生成新 session 可直接承接使用的 carryover_summary，类似 compact 后的上下文摘要。
3. 排除敏感信息后，提取需要长期记住的用户方面信息，作为 long_term_memory_items。

需要长期记住的信息范围：
- profile 类信息，例如称呼、沟通偏好、稳定身份、长期偏好和禁忌。
- 人际关系，包括但不限于家人、朋友、同事、伴侣、重要合作对象。
- 用户个人生活和工作中正在负责、经历、计划或持续关注的事情。
- 用户对 AI 的明确要求、纠正和边界。
- 整体标准是：现实中一个人对好朋友要记住的信息。

输出要求：
- 只输出 JSON。
- rough_summary 可以相对粗略，但必须忠于材料。
- carryover_summary 要适合后续聊天直接注入 prompt，短而有用。
- long_term_memory_items 必须克制、去重、可追溯。
- 如果没有长期记忆片段，输出空数组。

当前 MEMORY.md：
{current_memory}

当前 USER.md：
{current_user_profile}

旧 session metadata：
{session_metadata}

旧 session 可见对话：
{session_messages}

业务日 daily notes：
{daily_notes}

请按以下 JSON schema 输出：
{json_schema}
```

### 6.3 Prompt Versioning

每次 Dreaming run 必须记录：

- `prompt_version`
- `llm_model`
- `input_hash`
- `output_json`
- `token_usage`
- `source_session_id`
- `source_business_day`

Prompt 修改必须升版本，避免记忆质量变化无法追踪。

## 7. Token 与长 session 处理

如果一个 session 或 daily notes 超过模型上下文预算，使用 map-reduce：

```text
visible messages / daily notes
-> chunk by chronological order
-> LLM chunk_compaction
-> merge chunk summaries + existing MEMORY/USER
-> LLM final_dreaming
```

Chunk compaction 输出：

- chunk rough summary
- extracted facts
- open threads
- sensitivity exclusions
- source id range

Final dreaming 再负责：

- 去重。
- 合并相似记忆片段。
- 生成最终 `carryover_summary`。
- 生成最终 `long_term_memory_items`。

## 8. 数据模型

遵循“如无必要，勿增实体”，本机制只新增为了自动应用、debug 调优和回滚不可缺少的实体。

### 8.1 `dreaming_runs`

用于记录一次 Dreaming/压缩任务。

```text
dreaming_runs
- id
- account_id
- source_type: daily_dreaming | max_turns_compression | manual_admin
- source_session_id
- source_business_day
- status: queued | running | succeeded | failed | partial
- prompt_version
- llm_model
- input_hash
- output_json
- error
- token_input
- token_output
- actor_type: system | admin
- actor_id
- started_at
- completed_at
- created_at
- updated_at
```

### 8.2 `sessions` 扩展字段

已存在并继续使用：

```text
sessions
- session_summary
- carryover_summary
- summary_model
- summary_prompt_version
- close_reason: daily_dreaming | max_turns
- ended_at
```

当前代码已有 `carryover_summary`、`close_reason`、`ended_at` 等字段；后续补 `session_summary` 和 summary metadata。

### 8.3 `dreaming_memory_items`

用于保存 Dreaming 生成的长期记忆片段及其自动应用结果。它不是人工审核队列。

```text
dreaming_memory_items
- id
- account_id
- dreaming_run_id
- source_type: daily_dreaming | max_turns_compression | user_correction | manual_admin
- source_session_id
- source_daily_note_date
- operation: add | update | delete | downgrade
- target_file: MEMORY.md | USER.md | SOUL.md | IDENTITY.md
- category: profile | relationship | preference | life_event | work_project | ai_instruction | correction | other
- memory_text
- base_text_hash
- diff_json
- importance: high | medium | low
- confidence
- sensitivity: normal | sensitive | highly_sensitive
- apply_status: applied | skipped | failed | rolled_back
- skip_reason
- reason
- metadata_json
- created_at
- applied_at
```

### 8.4 `memory_events`

用于自动应用、跳过、失败和回滚审计。

```text
memory_events
- id
- account_id
- memory_item_id
- event_type: generated | applied | skipped | failed | rollback
- actor_type: system | admin | user
- actor_id
- before_text
- after_text
- diff_text
- metadata_json
- created_at
```

## 9. 自动应用策略

```text
Dreaming run
queued -> running -> succeeded
                  -> partial
                  -> failed

Memory item
generated -> applied
          -> skipped
          -> failed
          -> rolled_back
```

默认自动应用规则：

- `sensitivity != normal`：跳过，只保留 debug 记录。
- `confidence < 0.75`：跳过，只保留 debug 记录。
- `importance = low`：跳过，只保留 debug 记录。
- `source_type = max_turns_compression` 且非 `importance=high`：跳过。
- `operation in (delete, downgrade)`：可以自动执行，但必须来自用户明确纠正或高置信 Dreaming 输出；否则跳过。
- 通过规则的 item 自动应用到目标 Context File，并记录 before/after/diff。

这些阈值是调优参数，后续可以按 Dreaming debug 质量评估调整。

## 10. Apply 与 Rollback

Apply 步骤：

1. 读取 memory item。
2. 执行自动应用规则。
3. 读取目标文件当前内容。
4. 校验 `base_text_hash`，发现目标文件已变更则重新生成 merge 或跳过。
5. 应用 diff 或追加条目。
6. 写回目标文件。
7. 记录 `memory_events.applied`，保存 before/after/diff。
8. item 置为 `applied`。

Rollback 只用于纠错和事故恢复，不是前置审核流程。

Rollback 步骤：

1. 读取 `memory_events.applied` 中的 before/after/diff。
2. 校验目标文件当前内容仍匹配 after。
3. 恢复 before。
4. 记录 `memory_events.rollback`。
5. item 置为 `rolled_back`。

## 11. Orchestrator 集成

当前 Conversation Orchestrator 已能在业务日或轮次边界懒切换 session。Dreaming 接入后调整为：

```text
before creating new active session
-> close old active session
-> run/enqueue dreaming compaction
-> if LLM compaction succeeds:
      use LLM carryover_summary for new session
      auto-apply eligible memory items
   else:
      use deterministic fallback carryover_summary
-> create new __account_active__ session
```

失败策略：

- Dreaming 失败不能阻塞用户继续聊天。
- 如果同步压缩超时，使用 deterministic fallback，并把 dreaming_run 标记为 `failed` 或 `partial`。
- 后台 worker 可重试 Dreaming，并在成功后更新 closed session 的 `session_summary`、生成 memory items、自动应用合格 item。

## 12. Admin 与隐私

Admin/Debug 的目标是观测 Dreaming 效果、调试 prompt 和调整阈值，不承载人工审核流程。

后台可展示：

- dreaming run status。
- source metadata。
- generated/applied/skipped item 数量。
- item category、importance、confidence、sensitivity、apply_status。
- skip_reason 和 diff 摘要。
- 脱敏后的 memory_text 摘要。

明文查看来源材料、daily notes 正文、session messages 或完整 prompt/output，必须遵守 [隐私与后台访问控制](privacy_admin_access_control_design.md)。

## 13. 开发切分

建议按以下顺序实现：

1. DB schema：新增 `dreaming_runs`、`dreaming_memory_items`、`memory_events`，补 `sessions.session_summary` 和 summary metadata。
2. LLM prompt：实现 `dreaming_prompt_version=v1`、JSON schema 校验和 fake LLM 测试。
3. `app/dreaming.py` 改造：从直接覆盖 `MEMORY.md` 改为 LLM compaction + memory items + auto apply。
4. 自动应用规则：实现 sensitivity/confidence/importance/source_type 阈值、skip reason 和 rollback 事件。
5. Session close 集成：daily boundary 和 max turns 使用 LLM carryover，失败时 deterministic fallback。
6. 4 点调度器：独立扫描 active sessions，触发 daily Dreaming。
7. Debug API：只读展示 run、item、apply_status、skip_reason 和脱敏摘要。
8. Admin UI：默认脱敏展示 Dreaming run 和 memory item 状态，用于调优。

## 14. 验收点

- 每次 Dreaming 都有 `dreaming_run` 记录、prompt version、model 和 source metadata。
- session 压缩和 carryover 由 LLM 生成；LLM 失败时才使用 deterministic fallback。
- 新 session prompt 使用 LLM `carryover_summary`。
- Dreaming 不再等待人工审核。
- Dreaming 合格记忆片段会自动应用到目标 Context File。
- 被跳过的记忆片段保留 apply_status、skip_reason 和 debug metadata。
- 敏感信息不会进入自动应用的长期记忆。
- 每次自动应用都记录 before/after/diff。
- applied item 可以 rollback。
- 500 轮压缩不会激进扩大长期记忆，只自动应用高置信高重要普通敏感度片段。
- 不同 `ai4all_account_id` 的 Dreaming 输入、生成片段和事件不串线。
