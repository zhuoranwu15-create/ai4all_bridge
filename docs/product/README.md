# 产品专题 PRD 导航

更新时间：2026-06-02

本文档索引 Phase 1 的产品专题 PRD。总 PRD 见 [../prd.md](../prd.md)，用于定义产品定位、Phase 1 范围、优先级和总体验收；专题 PRD 用于展开单个能力的用户流程、策略规则和验收细节。

## 专题列表

| 专题 PRD | 对应能力 | 对应技术文档 |
| --- | --- | --- |
| [注册与扫码接入](onboarding_prd.md) | 手机号 OTP、首次扫码绑定、解绑、后续重复绑定策略 | [身份模型与微信绑定](../tech_design/identity_model_and_wechat_binding.md) |
| [首次聊天 Onboarding](first_chat_onboarding_prd.md) | 绑定后主动引导、用户称呼、AI 称呼、人设预设、跳过容错 | [Conversation Orchestrator](../tech_design/conversation_orchestrator_design.md)、[Agent Context Files](../tech_design/agent_context_files.md) |
| [陪伴式聊天](companion_chat_prd.md) | 微信私聊、陪伴体验、默认 Soul、安全边界 | [Conversation Orchestrator 主对话场景技术设计](../tech_design/conversation_orchestrator_design.md) |
| [记忆与上下文](memory_prd.md) | short-term context、daily notes、Dreaming、记忆管理 | [Agent Context Files](../tech_design/agent_context_files.md) |
| [主动消息与提醒](proactive_prd.md) | 用户提醒、陪伴跟进、内容推送 | [主动消息与提醒设计](../tech_design/proactive_messaging_design.md) |
| [Web Search 同步工具调用](search_and_async_tasks_prd.md) | Web Search 同步工具调用、失败和复杂任务不支持说明 | [Web Search 同步工具调用技术设计](../tech_design/search_async_tasks_design.md) |
| [语音输入](voice_prd.md) | 微信语音、上游转写后文本回复 | [语音输入技术设计](../tech_design/voice_input_design.md) |
| [权益、增长与支付后置](entitlement_growth_prd.md) | 贝壳、扣减、拉新、支付后置 | [贝壳、增长与支付后置技术设计](../tech_design/entitlement_growth_design.md) |
| [运营与后台](admin_ops_prd.md) / [后台页面规划](admin_ops_views.md) | Admin、客服支撑、观测、风控、审计、页面优先级 | [隐私与后台访问控制](../tech_design/privacy_admin_access_control_design.md) |

## 维护规则

- 总 PRD 只保留 Phase 1 总目标、范围、优先级、总验收和专题索引。
- 专题 PRD 负责具体流程、策略、话术边界、后台需求和专题验收。
- 若专题 PRD 与总 PRD 冲突，以总 PRD 为准，并同步修正专题。
- 若专题 PRD 与技术设计冲突，先回到产品目标判断，再更新技术设计。
