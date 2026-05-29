# Phase 1 需求追踪矩阵

更新时间：2026-05-28

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
| [注册与扫码接入](product/onboarding_prd.md) | 手机号 OTP、扫码绑定、默认 AI4ALL Account、重复绑定、解绑 | [身份模型与微信绑定](tech_design/identity_model_and_wechat_binding.md)、[OpenClaw Bridge](tech_design/openclaw_bridge_design.md)、[QR 补丁](tech_design/openclaw_weixin_gateway_qr_patch.md) | `app/main.py`、`app/db.py`、`app/identity.py`、`app/openclaw_gateway.py`、`app/static/onboarding.html` | 部分实现 | 重复绑定策略、正式解绑、`already_connected`/失败状态、`/web/config`、普通入口禁止多 account |
| [陪伴式聊天](product/companion_chat_prd.md) | 微信私聊文本、默认 Soul、首次聊天 onboarding、动态 prompt/context、安全围栏 | [Conversation Orchestrator 主对话场景技术设计](tech_design/conversation_orchestrator_design.md)、[Agent Context Files](tech_design/agent_context_files.md) | `app/turn_service.py`、`app/prompt_builder.py`、`app/llm.py`、`app/user_profiles.py` | 部分实现 | 首次聊天 onboarding、用户自然语言修改人设/称呼、token/cost 落库、Safety 优先级与红线测试、陪伴质量回归集 |
| [记忆与上下文](product/memory_prd.md) | active session、daily notes、Dreaming、`MEMORY.md`、记忆管理；检索式记忆可选 | [Agent Context Files](tech_design/agent_context_files.md)、[Dreaming 记忆压缩与长期记忆](tech_design/dreaming_memory_design.md) | `app/user_profiles.py`、`app/memory_writer.py`、`app/dreaming.py`、`app/session_lifecycle.py`、`app/dreaming_scheduler.py` | 已可复用，暂缓扩展 | daily notes raw append、普通聊天不注入 daily notes、active session 懒切换、LLM Dreaming、LLM carryover、memory item 自动应用/跳过、结构化记忆事件、rollback、4 点 scan/scheduler 和 Admin 脱敏摘要已落地；因真实聊天样本不足，暂不继续扩展检索式记忆或更复杂 Dreaming |
| [主动消息与提醒](product/proactive_prd.md) | 用户提醒、陪伴跟进、内容推送、三类频控、6 小时避让、自然语言取消/更新 | [主动消息与提醒设计](tech_design/proactive_messaging_design.md) | `app/proactive/*`、`app/reminder_parser.py`、`scripts/run_proactive_scheduler.py` | 需重构 | 技术设计已按三类消息重写；代码仍需类型化 outbound policy，修正用户提醒 quiet hours/日上限影响，并补周期提醒、取消/更新确认、6 小时避让、内容推送、真实微信端到端联调 |
| [搜索与异步任务](product/search_and_async_tasks_prd.md) | DuckDuckGo/Tavily/Kimi Search、默认异步、任务补发、成本明细 | [搜索与异步任务技术设计](tech_design/search_async_tasks_design.md)、[Conversation Orchestrator 主对话场景技术设计](tech_design/conversation_orchestrator_design.md) | 暂无正式模块 | 待实现 | `tasks`/`task_runs`、搜索 provider adapter、任务 worker、补发幂等、引用格式、失败扣减规则、贝壳成本事件 |
| [语音输入](product/voice_prd.md) | 微信语音 payload、豆包 ASR、60s 上限、转写进文本链路 | [语音输入技术设计](tech_design/voice_input_design.md)、[OpenClaw Bridge](tech_design/openclaw_bridge_design.md) | `app/schemas.py` 有 media schema；`app/turn_service.py` 仍是占位 | 待实现 | OpenClaw 真实 voice payload、媒体获取、豆包 ASR adapter、60s 校验、ASR 失败体验、ASR 成本事件 |
| [权益、增长与可选支付](product/entitlement_growth_prd.md) | 贝壳、新用户赠送、扣减、邀请奖励、可选购买 | [贝壳、增长与可选支付技术设计](tech_design/entitlement_growth_design.md)、[Phase 1 详细技术设计](phase1_technical_design.md) | `daily_usage` 仅记录消息次数；暂无 wallet/ledger | 待实现 | wallet/ledger、注册赠送、token 计量、模型倍率、主动触达首条平台成本、邀请码、3 条有意义消息判断、反作弊、可选订单 |
| [运营与后台](product/admin_ops_prd.md) | Admin、客服、观测、隐私红线、角色分级、2 小时临时明文权限、操作日志 | [隐私与后台访问控制](tech_design/privacy_admin_access_control_design.md)、[Phase 1 详细技术设计](phase1_technical_design.md) | `app/main.py` Admin/Debug API、`app/static/admin.js`、`app/static/account.html` | 需重构 | 技术设计已按简化权限模型新增；代码仍需 Admin/Debug 默认脱敏、admin/staff 角色、临时明文权限、明文查看日志、客服记录、权益/任务/拉新视图 |

## 3. 跨领域追踪

| 领域 | 当前结论 | 涉及文档 | 技术落点 |
| --- | --- | --- | --- |
| 业务隔离主键 | `ai4all_account_id` 是业务隔离主键；当前代码 `account_id` 暂作兼容别名 | [PRD](prd.md)、[身份模型与微信绑定](tech_design/identity_model_and_wechat_binding.md) | 新增代码优先使用 `ai4all_account_id` 命名；旧 DB/API 渐进迁移 |
| OpenClaw 边界 | OpenClaw 是通道层，不持有 AI4ALL 业务状态 source of truth | [总体架构](architecture_overview.md)、[OpenClaw Bridge](tech_design/openclaw_bridge_design.md) | Bridge/Gateway 只做收发和 hook；业务状态留在 Backend |
| 主动触达扣费 | 首条主动触达不扣用户贝壳；用户回复后的后续 AI 回复或任务执行开始扣 | [权益 PRD](product/entitlement_growth_prd.md)、[主动消息 PRD](product/proactive_prd.md) | outbound ledger 记录平台成本；entitlement ledger 只扣用户后续链路 |
| 隐私红线 | 默认不能看正文类内容；管理员可看明文；普通后台用户需管理员审批的 2 小时临时明文权限；所有明文查看记录操作日志 | [运营后台 PRD](product/admin_ops_prd.md)、[记忆 PRD](product/memory_prd.md)、[隐私与后台访问控制](tech_design/privacy_admin_access_control_design.md) | Admin/Debug API 默认脱敏；新增角色分级、临时明文权限和明文查看日志 |
| 长任务体验 | Web Search 等高耗时任务默认异步，先确认再补发；语音可按耗时选择同步 ASR 或异步转写 | [搜索 PRD](product/search_and_async_tasks_prd.md)、[语音 PRD](product/voice_prd.md)、[搜索技术设计](tech_design/search_async_tasks_design.md)、[语音技术设计](tech_design/voice_input_design.md)、[Conversation Orchestrator](tech_design/conversation_orchestrator_design.md) | `tasks`/`task_runs`、worker、provider adapter、outbound result delivery |
| Dreaming | 学习 OpenClaw Dreaming，但写入要账号隔离、可追溯、可回滚；本版本 session 压缩和 carryover 明确走 LLM 调用，记忆片段无人工审核，按策略自动应用或跳过 | [记忆 PRD](product/memory_prd.md)、[Agent Context Files](tech_design/agent_context_files.md)、[Dreaming 记忆压缩与长期记忆](tech_design/dreaming_memory_design.md) | daily notes、LLM session compression、LLM carryover、dreaming_memory_items、auto apply/skip、debug 调优、rollback；核心代码基线已落地，当前等待真实聊天样本做质量评估，暂缓继续扩展 |

## 4. 技术文档重构顺序

建议先按以下顺序重构或新增技术文档：

1. [主动消息与提醒设计](tech_design/proactive_messaging_design.md)：已按三类消息、6 小时避让、类型化 outbound policy、周期提醒和取消/更新确认完成重写；后续进入代码重构。
2. [Agent Context Files 与记忆机制](tech_design/agent_context_files.md)：已按 daily notes 原始材料、session 生命周期、记忆文件边界、正文访问红线完成重写；Dreaming 细节拆到独立文档。
3. [Dreaming 记忆压缩与长期记忆](tech_design/dreaming_memory_design.md)：核心代码基线已完成 LLM session 压缩、LLM carryover、memory item、自动应用/跳过、debug 调优和事后回滚；因真实聊天样本不足，暂缓继续扩展，后续以质量评估和用户纠错链路为主。
4. [隐私与后台访问控制](tech_design/privacy_admin_access_control_design.md)：已按 Admin/Debug 默认脱敏、角色分级、2 小时临时明文权限和操作日志新增；后续进入代码重构。
5. [搜索与异步任务技术设计](tech_design/search_async_tasks_design.md)：已按 Web Search、provider 回退、异步任务、结果补发和成本事件新增；后续进入代码实现。
6. [语音输入技术设计](tech_design/voice_input_design.md)：已按微信语音、豆包 ASR、60 秒限制、转写文本和失败体验新增；后续进入代码实现。
7. [贝壳、增长与可选支付技术设计](tech_design/entitlement_growth_design.md)：已按 wallet/ledger、成本事件、注册赠送、邀请奖励、客服补发和可选支付新增；后续进入代码实现。
8. 回头更新 [Phase 1 详细技术设计](phase1_technical_design.md)：只保留总技术平面、目标数据模型和工作包摘要，把细节下沉到专题设计。

## 5. 开发推进顺序

建议技术文档重构后，按依赖关系推进开发：

1. 隐私红线和 Admin/Debug 脱敏先行，避免内测前形成错误后台能力。
2. 身份/绑定硬化：重复绑定、解绑、异常状态、禁止普通入口多 account。
3. 对话主链路：prompt 优先级、首次聊天 onboarding、usage/token/cost 记录。
4. 记忆/Dreaming：当前先 hold 复杂扩展；保留真实样本质量评估、用户纠错链路和必要 bugfix，不启动检索式记忆或更激进的长期记忆自动化。
5. 主动消息/提醒：类型化 outbound policy、周期提醒、取消/更新确认、6 小时避让、内容推送。
6. 异步任务：`tasks`/`task_runs`、Web Search provider、ASR provider、结果补发。
7. 贝壳/增长：wallet/ledger、注册赠送、消耗扣减、邀请奖励、客服补发。
8. 内测发布：PostgreSQL/Redis/worker、日志告警、端到端验收和隐私合规补充。
