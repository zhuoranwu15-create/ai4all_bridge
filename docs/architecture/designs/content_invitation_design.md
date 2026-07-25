# 内容邀请技术设计

更新时间：2026-06-02

本文承接 [主动消息与提醒技术设计](proactive_messaging_design.md) 和 [主动消息与提醒 PRD](../../product/proactive_prd.md)，定义内容邀请的详细技术方案。内容邀请不是订阅型内容推送，也不是到点自动发日报；主动阶段只能发送朋友式询问，用户正向确认后才在当前入站回合返回标题列表。

> 用户**明确要求**的到点自动内容推送是另一条独立能力——例行简报（`create_reminder(fulfillment=dynamic)`），不走内容邀请，见 [动态提醒 / 例行简报设计](dynamic_reminder_scheduled_content_design.md)。

## 1. 设计原则

- 内容邀请必须采用 LLM tool use，不使用关键词、正则或 emoji 白名单作为确认、拒绝或触发主路径。
- 后端只做确定性校验：账号隔离、状态机、频控、避让、冷却、格式、安全和幂等。
- 主动邀请只能是询问式文本，不能包含标题列表、URL、长摘要或完整日报。
- 用户确认后的标题列表只包含标题，不包含 URL，不包含长摘要。
- URL、来源和搜索 provider trace 可保存在 DB 中用于审计和后续追问，但默认不直接展示给用户。

## 2. 两阶段状态机

```text
candidate
-> invited
-> accepted -> titles_sent
-> declined
-> expired
-> cancelled / rejected_by_policy
```

状态说明：

- `candidate`：后台 LLM 认为某 topic 和若干标题可能适合邀请，但尚未发送。
- `invited`：朋友式邀请已通过 outbound 发送给用户，等待用户回应。
- `accepted`：用户正向确认，LLM 调用确认工具，后端校验通过。
- `titles_sent`：当前入站回合已发送标题列表。
- `declined`：用户拒绝、退订或表达不想看。
- `expired`：用户长时间没有回应，邀请过期，不再把后续“嗯/好”关联到这次邀请。
- `rejected_by_policy`：发送前被 quiet hours、日上限、避让、冷却或 route 策略拦截。

## 3. 后台生成链路

后台生成也使用 LLM tool use，不用内容关键词规则。

```text
scheduler account check
-> build per-account content invitation context
-> LLM with tools: web_search, create_content_invitation_candidate, skip_content_invitation
-> optional web_search for current content
-> LLM calls create_content_invitation_candidate or skip_content_invitation
-> backend validates topic, titles, sources, account scope and safety
-> policy check for content_invitation
-> outbound invitation text
-> status invited
```

上下文输入：

- 单个 `account_id` 的称谓、用户关注点、最近稳定 topic、拒绝/冷却状态。
- 最近已发送的 reminder、companion followup、content invitation 统计。
- 本次账号主动检查 lookahead window 内已有或待发送的消息，避免重复触达。
- 可选内容源或 `web_search` 结果。外部内容必须作为不可信内容包装，不得变成 prompt 指令。

工具建议：

```json
{
  "name": "create_content_invitation_candidate",
  "description": "为当前账号创建一条朋友式内容邀请候选。只能创建邀请，不发送标题列表。",
  "parameters": {
    "type": "object",
    "properties": {
      "topic": {"type": "string"},
      "invitation_text": {"type": "string"},
      "title_items": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "title": {"type": "string"},
            "source_name": {"type": "string"},
            "url": {"type": "string"},
            "published_at": {"type": "string"}
          },
          "required": ["title"]
        },
        "minItems": 3,
        "maxItems": 10
      },
      "reason": {"type": "string"}
    },
    "required": ["topic", "invitation_text", "title_items"]
  }
}
```

后端校验：

- `topic` 必须非空，且应能追溯到用户显式关注点、长期画像、近期稳定 topic 或用户主动请求。
- `invitation_text` 只能是询问式邀请，不能包含标题列表、URL、长摘要或订阅号式表达。
- `title_items` 可以在 DB 中保存 URL 和来源用于审计，但后续发给用户时只输出 title。
- 候选生成、发送和搜索 provider run 都必须按 `account_id` 隔离。

### 3.1 当前实现与测试结论

第一轮实现已接入 scheduler 内部账号主动检查和 Proactive Debug 后台。手动点击 `Run Proactive Check` 时，如果 LLM 生成了候选，后台会展示 invitation 文本和标题；如果未生成，会展示 `reason` 和 detail，例如 `llm_no_content_invitation`。

当前生成器刻意保守：当测试账号聊天内容较少、近期话题跳跃，或只有一次性问题而没有稳定兴趣时，应调用 `skip_content_invitation`，不应为了凑测试而生成内容邀请。内容邀请的有效评估需要更丰富的真实聊天数据，例如用户反复讨论某个主题、明确表达持续关注，或近期对某类内容有稳定兴趣。

## 4. 用户确认链路

用户回复内容邀请后，普通 `/openclaw/turn` 进入 tool-enabled LLM。后端不能用关键词或 emoji 白名单直接判断确认。

```text
user reply after invited message
-> build prompt with active invitation summary
-> inject content invitation response tools
-> LLM decides:
   - send_content_invitation_titles
   - record_content_invitation_feedback
   - no tool / ask clarification
-> backend validates invitation state
-> final reply in current turn
```

工具建议：

```json
{
  "name": "send_content_invitation_titles",
  "description": "当用户正向确认想看上一条内容邀请时，发送该邀请对应的标题列表。",
  "parameters": {
    "type": "object",
    "properties": {
      "invitation_id": {"type": "string"},
      "max_titles": {"type": "integer", "minimum": 1, "maximum": 10}
    },
    "required": ["invitation_id"]
  }
}
```

```json
{
  "name": "record_content_invitation_feedback",
  "description": "记录用户对内容邀请的拒绝、退订或偏好反馈。",
  "parameters": {
    "type": "object",
    "properties": {
      "invitation_id": {"type": "string"},
      "feedback_type": {"type": "string", "enum": ["decline", "block_topic", "less_like_this", "more_like_this"]},
      "topic": {"type": "string"},
      "note": {"type": "string"}
    },
    "required": ["feedback_type"]
  }
}
```

确认规则：

- “嗯”“好”“可以”“看看”“发我”“来”以及积极 emoji 是否代表确认，由 LLM 结合上一条主动邀请、当前会话和用户习惯判断。
- 如果用户回复模糊或转移话题，LLM 不应调用发送标题工具。
- 如果用户拒绝或退订，LLM 调用反馈工具；后端写入冷却或 blocked preference。
- 如果没有 active `invited` invitation，`send_content_invitation_titles` 必须失败，不能凭一句“嗯”发送内容。

标题列表输出规则：

- 标题列表作为当前用户入站回合的普通 assistant reply 返回，不通过主动 outbound ledger 另发一条。
- 回复只包含标题，不包含 URL、不包含长摘要。可以有一句极短自然承接，例如“这几条标题你可能会有兴趣：”。
- 后端返回给 LLM 的 tool result 应是已裁剪和已净化的标题数组，避免模型把 URL 或长摘要带入最终回复。
- 成功发送后 invitation 状态更新为 `titles_sent`，并记录 `accepted_at`、`titles_sent_at`、`trigger_message_id` 和 `tool_invocation_id`。

## 5. 数据模型

建议新增：

```text
content_invitations
- id
- account_id
- topic
- invitation_text
- title_items_json
- status: candidate | invited | accepted | titles_sent | declined | expired | cancelled | rejected_by_policy
- scheduled_at
- invited_at
- responded_at
- expires_at
- outbound_message_id
- trigger_message_id
- tool_invocation_id
- source_task_id
- policy_reason
- metadata_json

content_invitation_preferences
- account_id
- topic
- status: allowed | cooled_down | blocked
- cooldown_until
- last_feedback_at
- feedback_count
- metadata_json
```

`title_items_json` 可保存：

```json
[
  {
    "title": "string",
    "source_name": "string",
    "url": "string",
    "published_at": "string",
    "retrieved_at": "string"
  }
]
```

## 6. 策略规则

- 发送前走 `content_invitation` policy。
- 每账号内容邀请默认每日最多 1 条。
- 各 topic 默认试探频率每天最多 1 次，但受内容邀请总日上限约束。
- 用户明确拒绝某类内容后，至少 1 个月内不再邀请该类别。
- Phase 1 不实施 1 个月后的重新试探，只记录状态。
- 同一 invitation 的主动发送幂等键建议为 `content-invitation-{invitation_id}`。
- 同一 invitation 的标题发送只能成功一次，后续重复确认应返回已经发过或自然继续对话。
