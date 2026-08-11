# Plum 产品文档

更新时间：2026-08-11

| 项目 | 当前事实 |
| --- | --- |
| 产品名 / `app_id` | Plum / `plum` |
| 发布状态 | 核心 PRD 已建立；专题需求、海外审核方案和生产启用状态待补齐 |
| 代码命名空间 | `app/products/plum/`（按现有平台接入逐步补齐） |
| 产品渠道 | 海外产品；具体渠道待产品方案确认 |
| 共享依赖 | 平台审核、账号、配额与 Agent Runtime；产品差异必须通过显式 product policy 注入 |

产品愿景、价值原则、核心体验和能力边界见 [Plum 产品需求文档](prd.md)；账号主体、Persona、
Character、Connection、Storyline 及资产归属的基础契约见
[账号、身份与资产关系](identity-and-assets.md)，目标表结构、关键事务和旧表迁移关系见
[Plum 总数据模型设计](plum-data-model.md)。当前 manifest 同时保留平台接入事实和风险记录，
不替代专题 PRD、技术设计或生产启用检查单。具体用户流程、渠道契约、数据边界和运营指标确认后，
应拆分为对应专题文档，并同步更新本 manifest。

## 当前专题文档

| 文档 | 状态 | 说明 |
| --- | --- | --- |
| [Plum 总数据模型设计](plum-data-model.md) | 骨架评审稿 / 基础批次已实现 | 迁移 72–76 已实现内容资产、Connection、Runtime 归属、Storyline / State、系统 Work ownership、唯一重开和 Character Create 幂等基础；记忆与 Turn Context 待实施 |
| [Work / Character 数据模型设计](character_work_data_model.md) | 团队评审稿 / Create 内核已实现 | 当前投影、Work、批准版本和 Tags 基础已由迁移 72 实现；迁移 76 与 Repository 已实现 Create 原子发布，正式审核/API 与 Edit 待实施 |
| [注册登录与用户创建角色开发准备](../../plans/products/plum/registration_login_character_create_readiness.md) | 实施中 | 2026-08-12 两项需求的身份边界、API、事务、错误语义、决策门和测试矩阵 |

## 当前重要事实

Plum 目前是“共享本地红线 + 不调用外部 provider”，并不等于已经具备完整的海外、多语言审核能力；后续仍需正式选定海外 provider 和规则集。

审核策略当前明确保证：

- Plum 不调用阿里云入站文本审核。
- Plum 不调用阿里云图片审核。
- Plum 不调用当前全局 LLM 审核 provider。
- 海外审核 provider 尚未落地前，继续使用共享本地确定性红线，避免空审核。
- Worker 按任务 `app_id` 执行产品策略，并复用 enqueue 时保存的规则快照。

## 待确认事项

- 正式的海外文本、图片和多模态审核 provider 及数据处理区域。
- 多语言本地规则集、红线等级、人工复核策略和申诉流程。
- Plum 的正式渠道、API audience、Storyline Context / 记忆实现方案和产品专属代码边界。
- 生产启用前的隐私、合规、数据留存和故障降级验收。

详细临时记录见 [项目临时笔记](project_notes.md)。跨产品能力与平台约束见 [`../../architecture/shared/`](../../architecture/shared/)。
