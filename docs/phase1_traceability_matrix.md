# Phase 1 需求追踪矩阵

更新时间：2026-05-31

## 1. 文档定位

本文用于把 Phase 1 产品需求、技术设计、当前代码实现和后续开发缺口串起来。它不替代 PRD 或技术设计，而是作为阶段推进时的对齐索引。

文档关系：

- 产品总口径见 [PRD](prd.md)。
- 单项产品需求见 [产品专题 PRD](product/README.md)。
- 总体架构见 [总体架构 / 框架设计](architecture_overview.md)。
- 技术专题设计统一放在 [tech_design](tech_design/)。

状态说明：

| 状态 | 含义 |
| --- | --- |
| 已可复用 | 当前代码或文档可作为 Phase 1 基础继续迭代 |
| 部分实现 | 已有局部能力，但与最新 PRD 仍有明显差距 |
| 待实现 | 尚未形成可用技术底座或产品闭环 |
| 需重构 | 当前实现或文档口径与最新 PRD 冲突，需要先调整 |

## 2. 产品到技术追踪

| 产品专题 | Phase 1 范围 | 对应技术设计 | 当前代码/模块 | 当前状态 | 主要缺口 |
| --- | --- | --- | --- | --- | --- |
| [注册与扫码接入](product/onboarding_prd.md) | 手机号 OTP、首次扫码绑定、默认 AI4ALL Account、解绑；重复绑定策略后置 | [身份模型与微信绑定](tech_design/identity_model_and_wechat_binding.md)、[OpenClaw Bridge](tech_design/openclaw_bridge_design.md)、[QR 补丁](tech_design/openclaw_weixin_gateway_qr_patch.md) | `app/main.py`、`app/db.py`、`app/identity.py`、`app/openclaw_gateway.py`、`app/static/onboarding.html` | 部分实现 | 当前先确保首次绑定后稳定使用；仍需 `/web/config`、正式解绑验收、绑定失败排障状态和 OpenClaw Gateway 补丁校验；重复绑定 / already_connected / 多微信多手机号策略后置 |
| [陪伴式聊天](product/companion_chat_prd.md) | 微信私聊文本、默认 Soul、首次聊天 onboarding、动态 prompt/context、安全围栏 | [Conversation Orchestrator 主对话场景技术设计](tech_design/conversation_orchestrator_design.md)、[Agent Context Files](tech_design/agent_context_files.md) | `app/turn_service.py`、`app/prompt_builder.py`、`app/llm.py`、`app/user_profiles.py` | 部分实现 | 首次聊天 onboarding、用户自然语言修改人设/称呼、token/cost 落库、Safety 优先级与红线测试、陪伴质量回归集 |
| [记忆与上下文](product/memory_prd.md) | active session、daily notes、Dreaming、`MEMORY.md`、记忆管理；检索式记忆可选 | [Agent Context Files](tech_design/agent_context_files.md)、[Dreaming 记忆压缩与长期记忆](tech_design/dreaming_memory_design.md) | `app/user_profiles.py`、`app/memory_writer.py`、`app/dreaming.py`、`app/session_lifecycle.py`、`app/dreaming_scheduler.py` | 已可复用，暂缓扩展 | daily notes raw append、普通聊天不注入 daily notes、active session 懒切换、LLM Dreaming、LLM carryover、memory item 自动应用/跳过、结构化记忆事件、rollback、4 点 scan/scheduler 和 Admin 脱敏摘要已落地；因真实聊天样本不足，暂不继续扩展检索式记忆或更复杂 Dreaming |
| [主动消息与提醒](product/proactive_prd.md) | 用户提醒、陪伴跟进、内容邀请、三类频控、6 小时避让、自然语言取消/更新 | [主动消息与提醒设计](tech_design/proactive_messaging_design.md) | `app/proactive/*`、`app/reminder_parser.py`、`scripts/run_proactive_scheduler.py`、`outbound_messages`、`proactive_account_state`、`proactive_commitments`、`content_invitations` | 部分实现，观察期 | 类型化 outbound policy、用户提醒 bypass quiet hours/日上限、6 小时避让、hidden commitment、账号主动检查、内容邀请、scheduler run-once 和独立 scheduler 已落地；仍需周期提醒、自然语言取消/更新确认、多实例 scheduler lease、更多真实微信端到端观察和效果调参 |
| [Web Search 同步工具调用](product/search_and_async_tasks_prd.md) | `web_search` 同步工具调用、DuckDuckGo/Bing RSS/Aliyun IQS/Baidu AI Search、失败/不支持说明、成本明细、搜索 5 贝壳扣减 | [Web Search 同步工具调用技术设计](tech_design/search_async_tasks_design.md)、[Conversation Orchestrator 主对话场景技术设计](tech_design/conversation_orchestrator_design.md) | `app/tools/*`、`app/web_search.py`、`app/tools/web_search_handlers.py`、`tool_invocations`、`search_provider_runs` | 部分实现 | 同步 search tool、provider adapter 和 trace 已有；仍需商业搜索成功后固定 5 贝壳扣减、引用格式收口、失败/不支持话术和真实 provider E2E |
| [语音输入](product/voice_prd.md) | 微信语音上游转写文本进入普通文本链路；后端 ASR fallback 后置 | [语音输入技术设计](tech_design/voice_input_design.md)、[语音输入追踪](tech_design/voice_input_asr_tracking.md) | `openclaw-bridge/index.js`、`app/turn_service.py` 文本链路 | 已可复用 | 当前依赖 `openclaw-weixin` 的 `voice_item.text`；不做豆包 ASR、媒体下载、60s 校验或 ASR 成本事件；后续仅观察上游稳定性 |
| [权益、增长与支付后置](product/entitlement_growth_prd.md) | 贝壳、新用户赠送、扣减、邀请奖励、支付后置 | [贝壳、增长与支付后置技术设计](tech_design/entitlement_growth_design.md)、[Phase 1 详细技术设计](phase1_technical_design.md) | `entitlement_wallets`、`entitlement_ledger`、`cost_events`、`app/db.py`、`app/turn_service.py`、wallet API | 部分实现 | wallet/ledger、新用户赠送、聊天估算扣减已落地；仍需真实 provider usage、模型倍率配置、搜索 5 贝壳扣减、运营补发/冲正入口、邀请码和 3 条有意义消息判断；支付正式后置 |
| [运营与后台](product/admin_ops_prd.md) | Admin、客服、观测、隐私红线、角色分级、2 小时临时明文权限、操作日志 | [隐私与后台访问控制](tech_design/privacy_admin_access_control_design.md)、[Phase 1 详细技术设计](phase1_technical_design.md) | `app/main.py` Admin/Debug API、`app/static/admin.js`、`app/static/account.html` | 需重构 | 技术设计已按简化权限模型新增；代码仍需 Admin/Debug 默认脱敏、admin/staff 角色、临时明文权限、明文查看日志、客服记录、权益/拉新视图 |

## 3. 跨领域追踪

| 领域 | 当前结论 | 涉及文档 | 技术落点 |
| --- | --- | --- | --- |
| 业务隔离主键 | `ai4all_account_id` 是业务隔离主键；当前代码 `account_id` 暂作兼容别名 | [PRD](prd.md)、[身份模型与微信绑定](tech_design/identity_model_and_wechat_binding.md) | 新增代码优先使用 `ai4all_account_id` 命名；旧 DB/API 渐进迁移 |
| OpenClaw 边界 | OpenClaw 是通道层，不持有 AI4ALL 业务状态 source of truth | [总体架构](architecture_overview.md)、[OpenClaw Bridge](tech_design/openclaw_bridge_design.md) | Bridge/Gateway 只做收发和 hook；业务状态留在 Backend |
| 主动触达扣费 | 首条主动触达不扣用户贝壳；用户回复后的后续 AI 回复或同步搜索开始扣 | [权益 PRD](product/entitlement_growth_prd.md)、[主动消息 PRD](product/proactive_prd.md) | outbound ledger 记录平台成本；entitlement ledger 只扣用户后续链路 |
| 隐私红线 | 默认不能看正文类内容；管理员可看明文；普通后台用户需管理员审批的 2 小时临时明文权限；所有明文查看记录操作日志 | [运营后台 PRD](product/admin_ops_prd.md)、[记忆 PRD](product/memory_prd.md)、[隐私与后台访问控制](tech_design/privacy_admin_access_control_design.md) | Admin/Debug API 默认脱敏；新增角色分级、临时明文权限和明文查看日志 |
| 复杂后台请求 | 普通 Web Search 默认同步 tool use；长耗时搜索、复杂整理、后台报告和“整理好后再发”在 Phase 1 当前回合返回失败/不支持说明。语音当前依赖上游转写，不进入后端 ASR task | [搜索 PRD](product/search_and_async_tasks_prd.md)、[语音 PRD](product/voice_prd.md)、[搜索技术设计](tech_design/search_async_tasks_design.md)、[语音技术设计](tech_design/voice_input_design.md)、[Conversation Orchestrator](tech_design/conversation_orchestrator_design.md) | tool invocation trace、provider adapter、search provider runs、失败/不支持话术 |
| Dreaming | 学习 OpenClaw Dreaming，但写入要账号隔离、可追溯、可回滚；本版本 session 压缩和 carryover 明确走 LLM 调用，记忆片段无人工审核，按策略自动应用或跳过 | [记忆 PRD](product/memory_prd.md)、[Agent Context Files](tech_design/agent_context_files.md)、[Dreaming 记忆压缩与长期记忆](tech_design/dreaming_memory_design.md) | daily notes、LLM session compression、LLM carryover、dreaming_memory_items、auto apply/skip、debug 调优、rollback；核心代码基线已落地，当前等待真实聊天样本做质量评估，暂缓继续扩展 |

## 4. 技术文档重构顺序

建议先按以下顺序重构或新增技术文档：

1. [主动消息与提醒设计](tech_design/proactive_messaging_design.md)：已按三类消息、6 小时避让、类型化 outbound policy、content invitation 和 scheduler 边界更新；代码已进入观察期，后续补周期提醒、自然语言取消/更新确认和多实例 scheduler lease。
2. [Agent Context Files 与记忆机制](tech_design/agent_context_files.md)：已按 daily notes 原始材料、session 生命周期、记忆文件边界、正文访问红线完成重写；Dreaming 细节拆到独立文档。
3. [Dreaming 记忆压缩与长期记忆](tech_design/dreaming_memory_design.md)：核心代码基线已完成 LLM session 压缩、LLM carryover、memory item、自动应用/跳过、debug 调优和事后回滚；因真实聊天样本不足，暂缓继续扩展，后续以质量评估和用户纠错链路为主。
4. [隐私与后台访问控制](tech_design/privacy_admin_access_control_design.md)：已按 Admin/Debug 默认脱敏、角色分级、2 小时临时明文权限和操作日志新增；后续进入代码重构。
5. [Web Search 同步工具调用技术设计](tech_design/search_async_tasks_design.md)：已按 Web Search 同步 tool use、provider 回退、失败体验和成本事件更新；后续进入代码实现。
6. [语音输入技术设计](tech_design/voice_input_design.md)：已改为依赖 `openclaw-weixin` 上游转写文本，后端 ASR fallback 后置。
7. [贝壳、增长与支付后置技术设计](tech_design/entitlement_growth_design.md)：已按 wallet/ledger、成本事件、注册赠送、搜索 5 贝壳扣减、邀请奖励、客服补发和支付后置更新。
8. 回头更新 [Phase 1 详细技术设计](phase1_technical_design.md)：只保留总技术平面、目标数据模型和工作包摘要，把细节下沉到专题设计。

## 5. 开发推进顺序

建议技术文档重构后，按依赖关系推进开发：

1. 隐私红线和 Admin/Debug 脱敏先行，避免内测前形成错误后台能力。
2. 身份/绑定硬化：首次绑定、解绑、异常排障状态、禁止普通入口多 account；重复绑定策略后置。
3. 对话主链路：prompt 优先级、首次聊天 onboarding、usage/token/cost 记录。
4. 记忆/Dreaming：当前先 hold 复杂扩展；保留真实样本质量评估、用户纠错链路和必要 bugfix，不启动检索式记忆或更激进的长期记忆自动化。
5. 主动消息/提醒：继续观察类型化 outbound policy、hidden commitment、账号主动检查和内容邀请；补周期提醒、取消/更新确认、多实例 scheduler lease。
6. 同步工具调用：`web_search` tool schema、provider adapter、tool invocation trace、搜索 5 贝壳扣减、失败/不支持话术；ASR provider 后置。
7. 贝壳/增长：wallet/ledger、注册赠送、消耗扣减、邀请奖励、客服补发。
8. 内测发布：PostgreSQL/Redis/scheduler worker、日志告警、端到端验收和隐私合规补充。
