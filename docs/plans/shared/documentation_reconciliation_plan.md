# 文档事实收敛与信息架构整理计划

更新时间：2026-07-31

状态：**进行中。P0–P3 已完成；P4 持续治理门禁待继续。**

> **2026-08-04 产品边界更新：** DOC-003/DOC-004 中“朝夕同时拥有 Native App/Companion World”
> 与“App 规范入口属于 zhaoxi”的结论已被朝夕 / 鸣蝉拆分决策取代。当前事实是
> `zhaoxi=微信/OpenClaw+Web/H5`、`mingchan=Native App+Companion World`，鸣蝉规范入口为
> `/api/v1/products/mingchan/*`；详见
> [拆分计划](zhaoxi_mingchan_product_split_plan.md)。下表保留原裁决作为文档治理历史。

## 1. 目标与边界

本计划负责把项目从“微信单形态 AI 陪伴 Bot”演进到“一个产品可有多种形态、后端可承载多个
聊天型产品”后的文档事实收敛下来。它是一次性执行工作台，不是新的永久事实中心。

稳定结论必须回写对应 owner 的权威文档：

| 事实类型 | 永久维护位置 |
| --- | --- |
| 当前重点、在途事项与已知缺口 | `docs/STATUS.md` |
| 产品定位、范围与验收 | `docs/products/<app_id>/prd.md` 与 `capabilities/` |
| 产品注册状态、渠道与兼容入口 | `docs/products/<app_id>/README.md` |
| 当前系统分层、依赖与生产拓扑 | `docs/architecture/overview.md` |
| 身份、状态作用域与数据模型 | `docs/architecture/system_design.md` 或对应专题设计 |
| 部署、备份、排障与值守 | `docs/ops/` |
| 客户端机器契约 | 提交到仓库并由 CI 校验的 OpenAPI snapshot |
| 未完成的实施过程 | `docs/plans/` |

本计划完成后，先确认所有稳定结论均已回写，再归档到 `docs/archive/deliveries/`。

## 2. 裁决方法

同一事实出现冲突时，按以下证据顺序核验：

1. 生产只读实测与已登记的生产配置；
2. 当前主干代码、部署单元和自动化契约；
3. 当前架构/产品权威文档；
4. 未完成计划、旧设计与历史归档。

产品目标不能仅凭当前代码反推；涉及产品范围、体验或命名的冲突必须由产品负责人裁决。
生产配置只记录非敏感结论，禁止把连接串、密钥、手机号或用户数据写入本文。

## 3. 当前事实裁决表

| ID | 事实主题 | 已发现的冲突 | 核验依据 | 裁决结果 | 永久权威文档 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| DOC-001 | 生产数据库与退路 | 部分部署/运行文档仍称生产使用 SQLite，或把 SQLite 写成生产回滚通道 | `AGENTS.md`；`README.md`；PG 备份/部署记录；`DATABASE_URL` 双后端代码 | 生产自 2026-06-21 起以 PostgreSQL 为唯一真实后端；SQLite 只保留 dev/test，不能作为生产回落路径 | `architecture/overview.md`、`ops/production_runbook.md` | 已回写 |
| DOC-002 | central/node 生产拓扑 | 总览仍描述瘦 node 不碰 DB、turn 全转发中心；厚节点 Runbook 与当前代码描述 node 本地 turn、直连中心 PG | `app/bootstrap/lifecycle.py`；`deploy/systemd/aliyun2-system/`；`ops/platform/p5_production_upgrade_runbook.md`；部署差异实测 | aliyun1 为 `central,node`，aliyun2 为厚 `node`；两端本地处理各自微信 turn 并读写同一中心 PG；只有 central 执行 DDL migration 和 central-only scheduler | `architecture/overview.md`、`ops/platform/aliyun1_aliyun2_deployment_diff.md` | 已回写 |
| DOC-003 | 产品、形态与渠道 | 早期文档把项目、微信 Bot、朝夕产品与 App 形态混用 | 产品注册表；朝夕 manifest；`ProductTurnServices` 注入边界 | AI4ALL 是多产品后端；`zhaoxi` 是当前唯一启用产品；微信、Web/H5、Native App 是朝夕的入口/形态；Companion World 是朝夕领域，不是共享模型 | `products/README.md`、`products/zhaoxi/README.md`、`architecture/core-model.md` | 已回写 |
| DOC-004 | App API 规范入口 | `/api/v1/` 与 `/api/v1/products/zhaoxi/` 在交接文档中均被描述为首选 | `app/products/zhaoxi/manifest.py`；产品 manifest；现有兼容路由 | 新客户端和新文档使用 `/api/v1/products/zhaoxi/`；既有 `/api/v1/` 继续兼容并固定为朝夕 audience | `products/zhaoxi/README.md`、App quickstart | 已回写 |
| DOC-005 | App OpenAPI 覆盖 | quickstart 手写“10 个”，完整交接手写“24 个”，容易继续漂移 | `docs/products/mingchan/openapi/app_v1.json`；`tests/test_app_openapi_contract.py` | snapshot 与契约测试是机器事实；quickstart 不再重复数量，完整交接仅在需要解释覆盖边界时维护当前集合 | OpenAPI snapshot、`app_api_handoff.md` | 已回写 |
| DOC-006 | App 生产能力位 | 设计/计划仍有 default-off 或待开量描述，交接文档称已开启 | 2026-07-31 生产 `GET /api/v1/app/config` 只读核验 | 当前生产返回 `voice_input`、Companion World 主能力及 v1.5 四个能力位均为 `true`；客户端仍必须按配置渲染 | `STATUS.md`、`app_client_brief.md`、`app_api_handoff.md` | 已回写 |
| DOC-007 | 朝夕用户说明范围 | `user_guide.md` 仍只描述微信，并声称没有正式登录态/用户中心 | 当前 Web/App 路由与客户端契约；产品负责人 2026-07-31 决策 | 微信与 App 使用说明分开；旧文档改为微信端说明，App 用户指南在正式发布口径冻结后另建 | `products/zhaoxi/experiences/` | 已回写 |
| DOC-008 | 完成计划状态 | 多份 M1/v1.5 计划仍写“待合入 main”，但相关提交已在当前主干 | Git 主干、计划验收表、当前实现 | M1、M2、v1.5 稳定事实已回写并归档；真实剩余项集中到朝夕 App backlog，不再借完成计划跟踪 | `plans/README.md`、`backlog/products/zhaoxi/`、`archive/deliveries/companion_world/` | 已回写 |
| DOC-009 | 旧文档路径 | 代码、脚本和测试注释仍引用已不存在的 `docs/tech_design/` | 仓库 `rg` 结果 | 更新为现行 owner 路径，并增加不允许新增旧路径的文档门禁 | 代码注释、测试门禁 | 已回写 |

## 4. 执行批次

### P0：事实冻结与高风险回写

- [x] 建立裁决规则和当前事实表。
- [x] 回写生产 PG/SQLite 边界。
- [x] 回写厚节点生产拓扑与 central-only migration/scheduler 边界。
- [x] 统一 App 规范 API 与 OpenAPI 事实来源。
- [x] 更新 `STATUS.md` 中已经由生产配置证实的能力状态。

### P1：平台叙事与阅读入口

- [x] 在架构目录补充核心模型与术语：product、experience/form、channel、platform user、
  product membership、runtime account、universe、channel binding。
- [x] 给 `docs/README.md` 增加产品、后端开发、客户端和运维四条阅读路径。
- [x] 明确朝夕微信形态、Native App/Companion World 与共享 Runtime 的边界。

### P2：朝夕产品文档收敛

- [x] 明确 `capabilities/`、渠道/形态体验文档与客户端 integration 文档的职责。
- [x] 将 App quickstart 收敛为最小接入入口，避免复制完整字段和状态清单。
- [x] 逐项审查聊天、Agent、自我、onboarding、记忆和主动消息文档的重复与冲突。
- [x] 与产品负责人确认 `user_guide.md` 的目标受众后更新或拆分。

首轮审查结论：

| 文档组 | 结论 |
| --- | --- |
| 注册 Onboarding / 首次聊天 Onboarding | 分别描述注册绑定和进入聊天后的初见阶段，边界有效，继续分开维护 |
| 陪伴聊天 / AI Agent / AI 自我 | 陪伴聊天保留总体验；Agent 设定降为早期基础框架；人格/使命/需求/关系的当前完整口径统一指向 AI 自我 PRD |
| 对话编排与动态加载子 PRD | 已明确不可参考且与实现漂移，移入 `archive/alignments/`；当前口径改指 Runtime 编排与关系状态设计 |
| 记忆与上下文 | 继续保留产品分层与体验规则；账号含义统一为隔离关系状态的 Runtime Account，补齐跨形态 session scope 和 Companion World 居民隔离；固定 500 轮轮转改为当前滚动摘要事实 |
| 主动消息与提醒 | 继续保留产品分类、频控与体验规则；微信送达技术探针下沉到 ops，只保留渠道约束，并补齐 App 通知收件箱投递面的边界 |

### P3：计划归档与历史边界

- [x] 核验 M1、M2、v1.5 计划的真实剩余项，并从已完成主体中拆出。
- [x] 已完成计划先回写稳定结论，再归档到 `archive/deliveries/companion_world/`。
- [x] 被取代设计在文首标明当前替代文档；历史执行记录不再冒充当前 Runbook。

### P4：持续治理门禁

- [x] 修复代码、脚本和测试中的旧 `docs/tech_design/` 引用。
- [x] 扩展文档测试，阻止旧目录路径重新出现。
- [ ] 为非归档活文档统一最小元信息：类型、owner、状态/适用范围、最后核验日期、权威实现。
- [x] 本批文档改动已运行 Markdown 链接与 OpenAPI 契约测试；后续继续作为变更门禁执行。

## 5. 验收条件

- `docs/README.md` 能在两跳内带读者到产品、架构、客户端契约或运维入口。
- 非归档文档不再把 SQLite 描述为生产后端或生产回落路径。
- 当前架构只描述厚节点生产拓扑；瘦节点一期方案明确标为历史阶段。
- App quickstart、完整交接和 OpenAPI snapshot 职责清晰且无相互冲突的手工统计。
- `plans/` 只保留有真实未完成项和验收条件的计划。
- Markdown 本地链接、App OpenAPI snapshot 与新增旧路径门禁全部通过。
