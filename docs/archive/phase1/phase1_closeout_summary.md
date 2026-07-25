# Phase 1 收尾总结（已冻结快照）

更新时间：2026-06-07

> **已冻结（2026-06-15 归档）：** 本文是 Phase 1 收尾时（全量 424 passed）的状态快照与基线交接，现已归档**不再更新**。它记录"Phase 1 交付了什么、当时还剩什么"。**当前现状、缺口与近期队列以 [`STATUS.md`](../../STATUS.md) 为准。**

## 0. 文档定位

本文是 Phase 1 阶段性收尾的**状态快照与基线交接**，用于回答三个问题：

1. Phase 1 各工作包当前真实落到什么程度（done / partial / not-done，以代码为准）。
2. 哪些能力超出原计划提前交付了。
3. 进入下一阶段前，还剩哪些大功能和收口项。

权威设计仍以 [PRD](../../products/zhaoxi/prd.md)、[总体架构](../../architecture/overview.md)、[Phase 1 详细技术设计](../../architecture/system_design.md) 和各专题技术设计为准；逐项需求-代码映射见 [Phase 1 需求追踪矩阵](phase1_traceability_matrix.md)。本文不重复字段级设计。

测试基线：截至本次收尾，`.venv/bin/pytest tests/` 全量 **424 passed**。

## 1. 工作包状态总览

工作包定义见 [Phase 1 详细技术设计 §7](../../architecture/system_design.md)。状态以当前代码为准。

| 工作包 | 状态 | 已落地 | 主要剩余 |
| --- | --- | --- | --- |
| WP0 隐私与后台访问控制 | 部分实现 | Admin/Debug 默认脱敏、admin/staff token、2 小时临时明文授权、明文访问日志、账号/钱包/主动消息视图 | 客服记录、邀请关系视图、Admin UI 完整性 |
| WP1 注册、扫码与通道绑定硬化 | 部分实现 | `/web/config`、注册+binding intent、首次扫码绑定主路径、用户登录态、wallet 查询、解绑 API | 真实解绑验收、Binding Intent 异常状态、补丁产品化校验、重复绑定策略（后置） |
| WP2 对话主链路与 Orchestrator | 部分实现 | 首次聊天 onboarding、消息去重、跨 session context、同步 tool use、聊天估算扣减 | 自然语言改人设/称呼、Safety 红线测试、陪伴质量回归集 |
| WP3 发送和成本底座 | 已落地 | `outbound_messages` 分类、幂等、`cost_events`、tool/provider trace | 随后续能力持续接入 |
| WP4 记忆与上下文产品化 | 已可复用（暂缓扩展） | daily notes 原始材料、4 点 scan、500 轮 LLM 压缩、carryover、memory item、自动应用/跳过、回滚、Admin 脱敏摘要 | 等真实样本做质量评估；不启动检索式记忆 |
| WP5 主动消息与提醒闭环 | 部分实现（观察期） | 类型化 outbound policy、一次性提醒、显式提醒识别、hidden commitment、账号主动检查、内容邀请、独立 scheduler、Proactive Debug 后台 | 周期提醒、自然语言取消/更新确认、多实例 scheduler lease、真实端到端调参 |
| WP6 搜索与语音输入 | 部分实现 | 同步 `web_search` tool、provider adapter（DuckDuckGo/Bing RSS/Aliyun IQS/Baidu）、tool invocation trace；语音依赖上游转写文本 | 商业搜索成功固定 5 贝壳扣减、引用格式收口、真实 provider E2E |
| WP7 贝壳、增长与客服 | 部分实现 | wallet/ledger、`cost_events`、新用户注册赠送 1000 贝壳、聊天按 token 扣减 | **拉新（见 §3）、模型价格倍率、搜索 5 贝壳扣减、运营补发/冲正入口、客服记录**；支付正式后置 |
| WP8 内测部署与观测 | 部分实现 | 阿里云单机部署文档、健康检查、运行健康清单、**自动化数据备份 + 备份陈旧告警**（见 §2） | PostgreSQL/Redis 迁移、独立 scheduler worker 生产化、结构化日志/trace id/告警体系 |

## 2. 超出原 Phase 1 范围的已交付能力

原 [PRD §4.4](../../products/zhaoxi/prd.md) 和 [roadmap 暂不做](../../roadmap.md) 把"图片/多模态"列为暂不做。Phase 1 收尾期实际提前交付了以下能力：

### 2.1 微信图片理解（VL 多维描述 → 文本对话链路）

- 代码基线（commit `6354814`）：
  - `app/image_understanding.py`：`describe_image()` 调 DashScope `qwen3-vl-plus`，读本机 `image_inbound_dir` 内文件（带 realpath 防穿越、大小上限）转 base64，超时/失败返回 `None`。
  - `app/turn_service.py`：`message_type=="image"` 分支，消费 `payload.media`，把描述合成 `"{caption}\n[用户发来一张图片：{描述}]"` 进历史；VL 失败走兜底话术、**不调主模型瞎猜**；`modality="image"` 入 `write_memory`。
  - 计费：`app/db.py::record_image_understanding_charge()`，独立 `cost_type=image_understanding`，固定扣 5 贝壳（`image_understanding_cost_shell_micros`），幂等、账号隔离。
  - 配置：`app/config.py` + `.env.example` 新增 `image_understanding_*` 全套（默认 `enabled=false`）。
  - bridge：`openclaw-bridge/index.js` 解析 `[media attached: ...]` 标记，转发 `message_type=image` + `media{path,format}`。
  - 自测：`scripts/send_mock_turn.py` 加 `--image-path/--image-url`；`tests/test_image_turn.py` 9 用例（A/B/C 场景、失败兜底、开关、账号隔离、modality、计费幂等）全过。
- 设计与运维文档：[图片理解技术设计](../../architecture/shared/platform/image_understanding_design.md)、[OpenClaw 补丁与部署机制](../../architecture/shared/access/openclaw_patches_maintenance.md)。
- **运维待跟进（关键）**：端到端需要 OpenClaw 核心 patch `patches/openclaw-before-agent-reply-media.patch` 生效。当前生产用 `scripts/deploy_image_understanding.sh` 手术式改哈希 dist；**OpenClaw 升级会静默退回成空文本**，且部署脚本写死了哈希文件名 `get-reply-9dLyvuw9.js`。升级后必查项见 [OpenClaw 补丁与部署机制 §3](../../architecture/shared/access/openclaw_patches_maintenance.md)。
- 两份文档当前标注为"草稿/待联调""临时草稿"，待生产 patch 部署 + 真图质量回归后转正。

### 2.2 自动化数据备份 + 备份陈旧告警

- 代码基线（commit `737fd8f`）：`scripts/backup_data.py`、`scripts/restore_data.py`、`deploy/systemd/ai4all-backup.{service,timer}`、`app/config.py` 备份配置、`scripts/monitor_health.py` 备份陈旧检查；`tests/test_backup_data.py`、`tests/test_runtime_health.py` 覆盖。
- 属于 WP8 内测部署与观测的提前补强。

## 3. 剩余的大功能：拉新送贝壳（未实现）

[权益 PRD §5](../../products/zhaoxi/capabilities/entitlement_growth_prd.md) 定义的拉新激励是 Phase 1 P1.5 范围内**尚未动工**的最大块功能。当前代码现状：

已就绪（拉新的底座）：

- 钱包 `entitlement_wallets`、流水 `entitlement_ledger`、成本事件 `cost_events`（`app/db.py`）。
- 新用户注册默认赠送 1000 贝壳（`grant_new_user_shells`，注册/登录入口已接）。
- 聊天按 token 扣减贝壳（`record_chat_usage_charge`）。

完全未实现：

- 邀请码生成/校验、带邀请码注册链接自动填充。
- 邀请人 ↔ 被邀请人关系记录（`referral_codes` / `referral_relationships` 在 [技术设计目标模型](../../architecture/system_design.md#5-状态所有权与数据模型摘要) 里规划，但表和逻辑均未建）。
- "新用户发 3 条有意义信息 → 后台 AI 判断 → 给邀请人发 1000 贝壳"的判定与发放链路。
- 反作弊（重复手机号/设备、刷量）。

顺带确认的其他权益扣减缺口（同属 WP7，独立于拉新）：

- 商业搜索成功固定扣 5 贝壳：**未接线**（`web_search` 当前不扣贝壳）。
- 模型价格倍率：`cost_events` 有 `model_price_multiplier_micros` 字段，但 turn 链路恒传 1.0x（未按模型折算）。
- 基准模型仍是硬编码 `gpt-4o-mini`，非 PRD 口径的 `deepseek v4-flash` 基准；`model_price_rules` 未落地。
- 运营补发/冲正入口、客服记录未做。

## 4. 本次文档清理记录

为下一阶段留干净基线，本次按"归档优先、只删真垃圾"原则整理：

- **删除**（仅编辑器 swap 垃圾，均未被 git 跟踪）：`docs/.debugging.md.swp`、`docs/product/.companion_chat_prd.md.swp`、`docs/superpowers/specs/.2026-05-29-...swp`。
- **归档**（`git mv` 至 `docs/archive/`，保留历史）：
  - `docs/superpowers/{plans,specs}/*`（2026-05-29/30 一次性实施计划与早期设计）→ `docs/archive/superpowers/`，空目录已移除。
  - `docs/architecture/designs/timezone_refactor_plan.md`（已完成的时区统一重构计划）→ `docs/archive/phase1/`。
- **移入 backlog**（未来非 Phase 1，未跟踪文件）：`100k_dau_scaling_discussion_draft.md`、`100k_dau_scaling_review.md` → `docs/backlog/`。
- **保留未动**（仍被多份 live 文档引用或仍具前瞻价值，仅在此标注）：
  - `docs/architecture/designs/voice_input_asr_tracking.md`：被 traceability、voice_prd、voice_input_design 引用，留原处。
  - `docs/architecture/designs/{agent_orchestration_roadmap,production_stability_prd}.md`、`docs/product/{website_prd,admin_ops_views}.md`：Phase 1 范围内前瞻/运营规划，进入下一阶段时再评估归档。
- **可清理 scratch（未处理，留待确认）**：仓库根目录 `test1.png`、`test2.jpg`、`test3.py` 是图片理解的一次性 POC，已被 `app/image_understanding.py` + `tests/test_image_turn.py` 取代，均未跟踪，可安全删除。

## 5. 下一阶段建议起点

1. **拉新闭环**（最大缺口）：建 `referral_codes` / `referral_relationships`，注册页邀请码填充与校验，"3 条有意义信息"判定，奖励发放走 `entitlement_ledger`，预留反作弊。
2. **权益扣减收口**：搜索 5 贝壳扣减接线、模型价格倍率 + `model_price_rules`、基准模型口径对齐、运营补发/冲正入口。
3. **图片理解转正**：生产 patch 部署稳定化（去掉写死哈希文件名、升级回归清单），真图多维描述质量回归，两份文档去掉"草稿"标注。
4. **主动消息观察期收口**：周期提醒、自然语言取消/更新确认、多实例 scheduler lease。
5. **内测部署硬化**：PostgreSQL/Redis 迁移、独立 scheduler worker 生产化、结构化日志与告警。
