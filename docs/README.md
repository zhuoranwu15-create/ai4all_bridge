# AI4ALL 文档导航

更新时间：2026-06-07

## 当前权威文档

这些文档是当前产品和技术对齐的主要依据：

| 文档 | 作用 |
| --- | --- |
| [PRD](prd.md) | Phase 1 产品定位、范围、需求和验收标准 |
| [产品专题 PRD 导航](product/README.md) | 单项产品能力的详细 PRD 索引 |
| [总体架构 / 框架设计](architecture_overview.md) | 系统整体架构、分层、核心链路和状态所有权 |
| [Phase 1 详细技术设计](phase1/phase1_technical_design.md) | 技术平面、目标数据模型、核心链路和 Phase 1 工作包 |
| [Phase 1 需求追踪矩阵](phase1/phase1_traceability_matrix.md) | 产品需求、技术设计、当前代码和开发缺口的追踪索引 |
| [Phase 1 收尾总结](phase1/phase1_closeout_summary.md) | 工作包状态快照、超额交付能力、剩余大功能和基线交接 |
| [后续规划](roadmap.md) | 产品和工程阶段路线 |
| [下一步开发步骤](phase1/next_dev_steps.md) | 当前开发队列和近期联调重点 |

## 产品专题 PRD

专题 PRD 承载单一产品能力的详细需求、用户流程、策略规则和验收标准。若与总 PRD 冲突，以总 PRD 为准，并同步修正专题。

| 文档 | 作用 |
| --- | --- |
| [产品专题 PRD 导航](product/README.md) | Phase 1 产品专题索引和维护规则 |
| [注册与扫码接入](product/onboarding_prd.md) | 手机号 OTP、扫码绑定、重复绑定、解绑、找回 |
| [首次聊天 Onboarding](product/first_chat_onboarding_prd.md) | 绑定后首次微信对话引导、称呼、人设预设和跳过容错 |
| [陪伴式聊天](product/companion_chat_prd.md) | 微信私聊、陪伴体验、默认 Soul、安全边界 |
| [记忆与上下文](product/memory_prd.md) | short-term context、daily notes、Dreaming、记忆管理 |
| [主动消息与提醒](product/proactive_prd.md) | 用户提醒、陪伴跟进、内容推送 |
| [Web Search 同步工具调用](product/search_and_async_tasks_prd.md) | Web Search 同步工具调用、失败和复杂任务不支持说明 |
| [语音输入](product/voice_prd.md) | 微信语音、上游转写、文本回复 |
| [权益、增长与支付后置](product/entitlement_growth_prd.md) | 贝壳、扣减、拉新、支付后置 |
| [运营与后台](product/admin_ops_prd.md) | Admin、客服支撑、观测、风控、审计 |
| [运营后台页面规划](product/admin_ops_views.md) | 运营视角下的后台页面、字段、动作、隐私边界和推进顺序 |

## 技术设计

技术专题文档承载单一领域的详细设计。`tech_design/` 是当前唯一技术专题入口；不要再新增或引用旧的 `topics/` 目录。若技术专题与上面的权威文档冲突，以权威文档为准，并回到专题文档修正。

| 文档 | 作用 |
| --- | --- |
| [身份模型与微信绑定](tech_design/identity_model_and_wechat_binding.md) | 产品用户、AI4ALL Account、绑定流程和账号路由 |
| [主动消息与提醒设计](tech_design/proactive_messaging_design.md) | outbound ledger、用户提醒、陪伴跟进、内容邀请、账号主动检查和 scheduler |
| [内容邀请技术设计](tech_design/content_invitation_design.md) | 内容邀请两阶段状态机、LLM tool use、确认/拒绝工具和标题列表规则 |
| [Agent Context Files 与记忆机制](tech_design/agent_context_files.md) | AGENTS/SOUL/IDENTITY/USER/TOOLS/MEMORY、daily notes 和记忆边界 |
| [Dreaming 记忆压缩与长期记忆](tech_design/dreaming_memory_design.md) | LLM session 压缩、carryover、memory items、自动应用/跳过、debug 调优和回滚 |
| [Conversation Orchestrator 主对话场景技术设计](tech_design/conversation_orchestrator_design.md) | 主对话 turn、Session/Messages、Intent/Tool Use、Prompt、同步回复和主动消息衔接 |
| [隐私与后台访问控制](tech_design/privacy_admin_access_control_design.md) | Admin/Debug 默认脱敏、角色分级、2 小时临时明文权限和操作日志 |
| [Web Search 同步工具调用技术设计](tech_design/search_async_tasks_design.md) | Web Search 同步工具调用、provider 回退、失败体验和成本事件 |
| [语音输入技术设计](tech_design/voice_input_design.md) | 微信语音、上游转写文本、后端 ASR fallback 后置 |
| [贝壳、增长与支付后置技术设计](tech_design/entitlement_growth_design.md) | wallet/ledger、成本事件、邀请奖励、客服补发和支付后置 |
| [OpenClaw Bridge 设计](tech_design/openclaw_bridge_design.md) | Bridge hook、payload、接口和失败策略 |
| [OpenClaw 微信 QR 补丁](tech_design/openclaw_weixin_gateway_qr_patch.md) | `openclaw-weixin` Gateway QR login provider discovery 补丁说明 |
| [图片理解技术设计](tech_design/image_understanding_design.md) | 微信图片 VL 多维描述 → 文本对话链路（草稿/待联调） |
| [OpenClaw 补丁与部署机制](tech_design/openclaw_patches_maintenance.md) | 两个 OpenClaw patch 的用途、部署方式和升级回归清单（临时草稿） |

## 流程设计

| 文档 | 作用 |
| --- | --- |
| [解绑流程设计](design/unbind-flow.md) | 用户解绑、保留/清除记忆、OpenClaw 微信登录态清理和重新绑定后的状态口径 |

## 操作指南

| 文档 | 作用 |
| --- | --- |
| [用户使用说明](guides/user_guide.md) | 当前用户视角的使用方式和限制 |
| [后台管理说明](guides/admin_guide.md) | Admin API / UI、排查和主动消息管理 |
| [调试指南](guides/debugging.md) | 技术 Debug 页面、Debug API、开发期明文策略和常用排障命令 |

## 未来 backlog

非 Phase 1 范围、留待后续阶段评估的前瞻性草稿。不作为当前设计依据。

| 路径 | 内容 |
| --- | --- |
| `backlog/` | 100k DAU 扩展讨论稿与评审等未来扩展性草稿 |

## 历史归档

归档文档保留历史上下文，不作为当前设计依据。

| 路径 | 内容 |
| --- | --- |
| `archive/early/` | Day 1 方案、早期技术草案、Agent Context Files 落地计划 |
| `archive/superpowers/plans/` | 一次性 agent 实施计划 |
| `archive/superpowers/specs/` | 已落地功能的早期设计草案 |
| `archive/phase1/` | Phase 1 内已完成的一次性计划（如时区统一重构） |

## 维护规则

- 新增长期有效的产品或技术结论，优先更新 PRD、总体架构或 Phase 1 详细技术设计。
- 需求、技术、代码和验收状态的映射更新到 `phase1/phase1_traceability_matrix.md`。
- 新增单一产品能力的详细需求，放入 `product/`。
- 新增单一技术领域的深入设计，放入 `tech_design/`；`tech_design/` 是唯一技术专题入口，不再新增 `topics/`。
- 操作步骤、排障、Admin 使用说明，放入 `guides/`。
- 一次性计划、已完成的执行清单、历史草案，放入 `archive/`。
- 移动文档时同步更新 README、本文档和所有文档内链接。
