# 后续规划

## 总方向

项目要从一个已验证的微信 AI 陪伴 Demo，演进成一个可扩展的个人 AI 陪伴与生活助理服务。

当前核心方向是：

```text
一个 OpenClaw 实例
-> 多个个人微信账号接入
-> 每个账号拥有独立 Soul、会话、记忆和配置
-> AI4ALL Backend 负责账号隔离和 AI 业务逻辑
```

OpenClaw 和 `openclaw-weixin` 只作为微信登录、收消息、发消息的通道层。官方插件虽然支持多微信账号连接，但 OpenClaw 原生 Soul 和长期记忆偏实例级共享，因此需要 AI4ALL Backend 做账号级隔离。

当前不采用“一个服务微信号服务多个外部微信用户”的客服号模型。

## 产品原则

- 私聊优先。
- 情感陪伴和生活助理优先。
- 每个接入微信账号都应拥有独立的 Soul、记忆和配置。
- 早期不展开心理咨询方向。
- 用户配置先由运营侧管理，不急于开放复杂自助配置。
- 先稳定小系统，再扩展能力。

## Phase 1：多微信账号接入与账号级隔离

目标：一个 OpenClaw 实例可以同时连接多个个人微信账号，并由 AI4ALL Backend 为每个账号提供独立 AI 陪伴/助理能力。

计划：

- 已验证 `openclaw-weixin` 多账号同时在线能力。
- 已配置 OpenClaw session 隔离策略：`session.dmScope=per-account-channel-peer`。
- 已确认微信账号级 `account_id` 可从 OpenClaw `sessionKey` 解析。
- 已修正 Bridge payload，传入真实账号 ID，而不是仅传 `openclaw-weixin` provider。
- Backend 已按 `account_id` 隔离会话、消息、Profile 和 Prompt。
- 已支持 `data/user_profiles/<account_id>/user_profile.md` 作为账号级 Soul 和简化长期记忆。
- 将账号级配置作为核心管理对象。
- 保留现有 `contacts` API 作为临时兼容，后续重命名或弱化。
- 已增加 raw payload 查看能力，便于排查多账号字段。

验收标准：

- 同一个 OpenClaw 实例中至少两个微信账号同时在线。
- 两个微信账号都能收到 AI4ALL Backend 生成的回复。
- 两个微信账号返回不同的真实 `account_id`。
- 不同微信账号之间上下文不串线。
- 不同微信账号可以使用不同 `user_profile.md`。
- 重启 Backend 后账号、会话和消息记录不丢。
- 出问题时可以从日志和 Admin API 定位到具体账号。

## Phase 2：账号级管理能力

目标：运营人员可以管理每个接入微信账号的状态、配置和会话。

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

## Phase 3：语音支持

语音仍在第一阶段产品范围内，但应在文本和多账号隔离稳定后实现。

计划：

- 明确 `openclaw-weixin` 实际传递的语音 payload。
- 决定 ASR 方案：
  - 本地 `ffmpeg` 转码后 ASR。
  - 或后端/云服务直接处理媒体 URL 或二进制。
- 支持 `message_type=voice`。
- 存储语音元数据。
- 先返回文本回复。
- 后续再考虑语音回复/TTS。

验收标准：

- 用户可以发送微信语音。
- Backend 能转写文本。
- LLM 基于转写文本回复。

## Phase 4：部署

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

## Phase 5：提升对话质量

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
- 完整公开 SaaS onboarding。
- 支付。
- 群聊 bot。
- 图片/多模态。
- 长期记忆产品化。
- 心理咨询工作流。
- 多 Agent 工作流。
