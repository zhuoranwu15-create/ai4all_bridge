# 后续规划

> 本文描述产品和工程阶段路线。总体架构以 `docs/architecture_overview.md` 为准；Phase 1 详细设计、身份模型和技术分层以 `docs/phase1_technical_design.md` 为准。

## 总方向

项目要从一个已验证的微信 AI 陪伴 Demo，演进成一个普通用户可低门槛接入的个人 AI 陪伴与轻量生活助理服务。

Phase 1 的终点是可正式发布的内测版本，而不是只完成技术 POC。除接入、聊天、提醒、搜索、语音和内容推送外，Phase 1 还需要补齐权益代币/点数、用量扣减、拉新激励和基础客服支撑。支付和用户购买正式后置，不进入 Phase 1 内测首发范围。

当前核心方向是：

```text
普通用户
-> Web/H5 手机号验证
-> 扫码接入微信 OpenClawBot 通道
-> AI4ALL Backend 创建或复用独立 AI4ALL Account
-> 用户在微信私聊中获得陪伴式对话和轻量助理能力
```

OpenClaw 和 `openclaw-weixin` 只作为微信登录、收消息、发消息的通道层。官方插件虽然支持多微信账号连接，但 OpenClaw 原生 Soul 和长期记忆偏实例级共享，因此需要 AI4ALL Backend 做账号级隔离，并承载用户注册、扫码绑定、Soul、记忆、提醒、主动消息和运营管理。

当前不采用“一个服务微信号服务多个外部微信用户”的客服号模型。

## 产品原则

- 私聊优先。
- 聊天陪伴优先，轻量生活助理跟进。
- 每个普通用户都应拥有独立的 AI4ALL Account、Soul、记忆和配置。
- 早期不展开心理咨询方向。
- 普通用户接入流程要尽量简单；复杂配置先由运营侧管理。
- 提醒、搜索、新闻、搞笑内容推送等助理能力必须有清晰限频、拒绝反馈和退出机制；新闻/搞笑等垂类内容可低频试探，用户明确拒绝后进入硬性冷却。
- 权益代币/点数、用量扣减和拉新激励必须有清晰流水、审计和客服处理入口；支付/购买如开启，也必须纳入同一审计链路。
- 先稳定小系统，再扩展能力。

## Phase 1 主线：接入、隔离与内测发布

目标：普通用户可以通过手机号验证和微信扫码接入服务，并由 AI4ALL Backend 为每个账号提供独立 AI 陪伴/助理能力。

计划：

- 已验证 `openclaw-weixin` 多账号同时在线能力。
- 已跑通 Web onboarding、手机号 OTP、扫码绑定和真实微信首条消息链路。
- 已配置 OpenClaw session 隔离策略：`session.dmScope=per-account-channel-peer`。
- 已确认 AI4ALL 业务账号 ID 当前可从 OpenClaw `sessionKey` 派生。
- 已修正 Bridge payload，避免仅传 `openclaw-weixin` provider。
- Backend 已按 AI4ALL 业务账号隔离会话、消息、Profile 和 Prompt。
- 已支持 `data/user_profiles/<account_id>/user_profile.md` 作为账号级 Soul 和简化长期记忆；这里的 `<account_id>` 是当前代码历史命名，语义上对应 AI4ALL 业务账号 ID。
- 将账号级配置作为核心管理对象。
- 保留现有 `contacts` API 作为临时兼容，后续重命名或弱化。
- 已增加 raw payload 查看能力，便于排查多账号字段。
- Phase 1 后续必须补齐 P1 能力：语音、搜索、新闻/搞笑内容推送、明确提醒、主动消息、hidden commitment。
- Phase 1 后续必须补齐内测权益能力：权益代币/点数、用量扣减、拉新奖励和基础客服处理。
- 支付、用户购买、拉新、客服逻辑是否同仓库实现待架构对齐；无论同仓库还是拆服务，都要先明确 API、数据模型和集成边界。支付/购买是可选线，不阻塞内测发布。

验收标准：

- 同一个 OpenClaw 实例中至少两个微信账号同时在线。
- 两个微信账号都能收到 AI4ALL Backend 生成的回复。
- 两个微信账号返回不同的 AI4ALL 业务账号 ID。
- 不同微信账号之间上下文不串线。
- 不同微信账号可以使用不同 `user_profile.md`。
- 重启 Backend 后账号、会话和消息记录不丢。
- 出问题时可以从日志和 Admin API 定位到具体账号。
- 用户可获得内测权益，LLM/ASR/搜索等资源消耗能形成权益扣减流水；如果开启支付，用户购买权益也能生效到账号。
- 邀请新用户完成注册/绑定后能按规则发放拉新奖励。
- 运营可以处理额度补发、误扣排查、邀请关系排查和基础客服问题；如果开启支付，也能处理支付异常。
- 满足正式内测发布条件。

## Phase 1 工作包：账号级管理能力

目标：运营人员可以管理每个 AI4ALL Account 的状态、绑定、配置、会话和基础权益。

计划：

- 完善 Admin API 的账号详情。
- 支持账号备注、启用/禁用、默认风格、默认 Prompt。
- 支持查看账号会话和消息。
- 支持重置某个账号的会话上下文。
- 增加账号级基础统计。
- 增加简单 Web 后台。

验收标准：

- 运营人员能看到所有接入微信账号。
- 运营人员能调整某个账号的 Soul / Prompt。
- 禁用某个账号不影响其他账号。
- 运营人员不用直接查数据库即可定位主要问题。

## 主动消息与提醒机制阶段状态

截至 2026-06-02，主动消息与提醒机制第一轮代码开发已完成。当前先暂停继续扩展该方向，进入真实数据观察和手动测试阶段；内容邀请和陪伴跟进需要更丰富、更稳定的聊天上下文后再评估效果。

已完成：

- 主动发送 Gateway 封装、outbound ledger、分类 policy、分类每日上限、quiet hours、6 小时避让第一版。
- one-shot reminder、显式提醒识别、due reminder scheduler。
- 账号级 proactive state 和系统级 scheduler。
- account check draft、人工确认、受控发送。
- hidden commitment 抽取、pending 存储、到期发送、Admin 查看/取消。
- 内容邀请候选生成、朋友式邀请发送、用户确认后标题列表回复、拒绝反馈/冷却。
- Proactive Debug 后台，用于手动运行 scheduler、单账号 proactive check、draft 调试和内容邀请生成原因展示。

观察期重点验证：

- 普通聊天产生 commitment 后，到期主动发微信。
- account check draft 人工确认后，scheduler 主动发微信。
- 内容邀请在稳定兴趣上下文下能生成合适候选，在稀疏/跳跃上下文下保持跳过。
- Gateway 重启和长时间无入站后的主动发送稳定性。

## Phase 1 工作包：语音输入

语音仍在第一阶段产品范围内。当前实现口径是依赖 `openclaw-weixin` 上游提供的转写文本进入普通文本链路，不在 Backend 侧接入 ASR fallback。

计划：

- 继续观察 `openclaw-weixin` 的 `voice_item.text` / `cleanedBody` 稳定性。
- 有上游转写正文时，按普通文本消息进入 `/openclaw/turn`。
- 无上游转写正文时，当前提示用户改用文字或重发。
- 不新增 ASR provider、媒体下载、转码、语音时长校验或 ASR 成本事件。
- 后续再考虑语音回复/TTS。

验收标准：

- 用户发送带上游转写正文的微信语音后，Backend 能按文本链路回复。
- 回复和扣费按普通聊天处理，不单独产生 ASR 成本。
- 后端 ASR fallback 后续单独评估。

## Phase 1 工作包：内测部署

目标：可以在阿里云上可复现部署。

计划：

- Backend Dockerfile。
- docker-compose。
- SQLite volume 或 PostgreSQL。
- 生产 `.env` 模板。
- 健康检查。
- 日志路径和保留策略。
- Bridge 后端 URL 和 Secret 配置。
- 明确 OpenClaw 运行方式：
  - 宿主机进程。
  - 或 Docker 容器并持久化状态目录。

验收标准：

- 新服务器可以按文档部署。
- Backend 重启后数据不丢。
- OpenClaw 多微信账号重新登录/重连流程有文档。
- 密钥不进入仓库。

## Phase 1 工作包：内测对话质量

目标：提升陪伴和生活助理体验。

计划：

- 优化默认 Prompt。
- 增加结构化 profile 字段。
- 增加风格预设。
- 初步用户偏好提取。
- 基于摘要的会话记忆。
- 针对医疗、法律、心理咨询等高风险话题建立边界。
- 建立测试对话集和评估方式。

## 暂不做

- 一个公共微信号服务大量外部用户的客服号模型。
- 完整公开商业化官网、复杂营销系统和多 bot 自助管理。
- 大规模客服系统。
- 群聊 bot。
- 图片/多模态。
- 心理咨询工作流。
- 多 Agent 工作流。
