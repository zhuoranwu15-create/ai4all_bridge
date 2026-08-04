# 朝夕相伴产品文档

更新时间：2026-08-04

## 产品 manifest

| 项目 | 当前事实 |
| --- | --- |
| `app_id` | `zhaoxi` |
| 状态 | 生产注册表已启用 |
| 代码命名空间 | `app/products/zhaoxi/` |
| 当前渠道 | 微信/OpenClaw；Web/H5 用于注册、扫码和用户入口 |
| 规范产品入口 | `/api/v1/products/zhaoxi/*`；反代剥离兼容 `/v1/products/zhaoxi/*` |
| 兼容入口 | 既有 `/web/*`、`/v1/*` 保持可用并固定为朝夕 audience |
| 共享依赖 | [平台架构](../../architecture/shared/README.md)、[Agent Runtime](../../architecture/agent-runtime/README.md) |

[总 PRD](prd.md) 定义朝夕的产品定位、Phase 1 历史范围、优先级和总体验收；能力文档用于展开用户流程、策略规则、话术边界和专题验收。历史“Phase 1”是朝夕已完成里程碑，不是未来产品的默认范围。

代码由 `app/products/zhaoxi/manifest.py` 组合产品 API 与 lifecycle；turn 产品实现位于
`app/products/zhaoxi/application/turn_services.py`，专属工具策略位于
`app/products/zhaoxi/tools/registry.py`。共享 Runtime 与 Platform 不反向依赖这些实现。

## 体验与接入文档

| 类型 | 回答的问题 | 入口 |
| --- | --- | --- |
| 产品体验 | 用户从哪个入口进入、该形态能做什么、有哪些限制 | [体验文档](experiences/README.md) |
| 客户端/通道接入 | Base URL、鉴权、协议、错误码和机器契约 | [接入与契约](integrations/README.md) |
| 产品能力 | 为什么做、具体业务规则、策略和验收标准 | 下方 `capabilities/` 专题 |

当前微信端用户说明已独立为[微信端使用说明](experiences/wechat.md)。原 Native App / Companion
World 已拆为独立产品[鸣蝉](../mingchan/README.md)，相关 PRD、客户端 handoff/brief 与 OpenAPI 已迁入
鸣蝉目录，不再属于朝夕产品边界。

未排期的朝夕产品事项统一进入[朝夕产品 Backlog](../../backlog/products/zhaoxi/README.md)；进入实施后
再转为 [`docs/plans/products/zhaoxi/`](../../plans/products/zhaoxi/README.md) 中带验收条件的计划。

## 能力专题

| 专题 PRD | 对应能力 | 对应技术文档 |
| --- | --- | --- |
| [注册与扫码接入](capabilities/onboarding_prd.md) | 手机号 OTP、首次扫码绑定、解绑、后续重复绑定策略 | [身份模型与微信绑定](../../architecture/shared/access/identity_model_and_wechat_binding.md) |
| [首次聊天 Onboarding](capabilities/first_chat_onboarding_prd.md) | 绑定后两步主动引导、用户称呼、AI 称呼与人设合并设置、留白养成、跳过容错 | [Conversation Orchestrator](../../architecture/agent-runtime/conversation_orchestrator_design.md)、[Agent Context Files](../../architecture/agent-runtime/agent_context_files.md) |
| [陪伴式聊天](capabilities/companion_chat_prd.md) | 微信私聊、陪伴体验、默认 Soul、安全边界 | [Conversation Orchestrator 主对话场景技术设计](../../architecture/agent-runtime/conversation_orchestrator_design.md) |
| [记忆与上下文](capabilities/memory_prd.md) | short-term context、daily notes、Dreaming、记忆管理 | [Agent Context Files](../../architecture/agent-runtime/agent_context_files.md) |
| [主动消息与提醒](capabilities/proactive_prd.md) | 用户提醒、陪伴跟进、内容推送 | [主动消息与提醒设计](../../architecture/products/zhaoxi/proactive_messaging_design.md) |
| [用户标签与元属性建设](capabilities/user_meta_attributes_prd.md) | 生命周期、活跃强度、陪伴类型、安全风险、主动触达反馈、权益增长画像 | 待补充 |
| [AI 自我：人格、使命、需求与关系成长](capabilities/agent_self_prd.md) | AI 自我模型：人格 × 使命组合、马斯洛三层需求、关系阶段成长、自我状态注入 prompt | [使命子系统 + 自我状态编排注入层](../../architecture/products/zhaoxi/agent_mission_and_orchestration_design.md) |
| [用户自建角色模板与邀请链接](capabilities/creator_role_template_referral_link_prd.md) | 用户创建名字/性格/使命模板，用一条链接同时完成拉新归因与新用户角色实例化；P0 无试玩，需用全新账号真实测试 | [技术设计](../../architecture/products/zhaoxi/creator_role_template_referral_link_technical_design.md) |
| [Web Search 同步工具调用](capabilities/search_and_async_tasks_prd.md) | Web Search 同步工具调用、失败和复杂任务不支持说明 | [Web Search 同步工具调用技术设计](../../architecture/agent-runtime/search_async_tasks_design.md) |
| [语音输入](capabilities/voice_prd.md) | 微信语音、上游转写后文本回复 | [语音输入技术设计](../../architecture/products/zhaoxi/voice_input_design.md) |
| [内容审核与人工复核](capabilities/content_moderation_prd.md) | 文本/图片审核、异步机器审核、人工复核、导出材料、角色权限 | [内容审核与人工复核技术设计](../../architecture/shared/platform/content_moderation_design.md) |
| [权益、增长与支付后置](capabilities/entitlement_growth_prd.md) | 贝壳、扣减、拉新、支付后置 | [贝壳、增长与支付后置技术设计](../../architecture/shared/platform/entitlement_growth_design.md) |
| [运营与后台](capabilities/admin_ops_prd.md) / [后台页面规划](capabilities/admin_ops_views.md) | Admin、客服支撑、观测、风控、审计、页面优先级 | [隐私与后台访问控制](../../architecture/shared/platform/privacy_admin_access_control_design.md) |

## 维护规则

- 总 PRD 只保留产品总目标、历史/当前范围、优先级、总验收和专题索引。
- 专题 PRD 负责具体流程、策略、话术边界、后台需求和专题验收。
- 若专题 PRD 与总 PRD 冲突，以总 PRD 为准，并同步修正专题。
- 若专题 PRD 与技术设计冲突，先回到产品目标判断，再更新技术设计。
- 产品专属架构见 [`../../architecture/products/zhaoxi/`](../../architecture/products/zhaoxi/README.md)；Admin 与调试操作见 [`../../ops/products/zhaoxi/`](../../ops/products/zhaoxi/README.md)。
