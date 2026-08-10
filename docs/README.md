# AI4ALL 文档导航

更新时间：2026-08-04

文档同时按“生命周期”和“所有权”组织：需求、架构、执行、运维、历史描述生命周期；
`products/<app_id>/`、`architecture/shared/` 与 `architecture/agent-runtime/` 描述所有权。
当前事实只在非归档文档维护；`archive/` 只提供历史背景，不参与当前决策。

## 当前事实入口

| 文档 | 回答的问题 |
| --- | --- |
| [项目状态](STATUS.md) | 现在运行到哪里、近期在做什么、有哪些已知缺口 |
| [产品目录](products/README.md) | 已注册产品、`app_id`、状态和产品文档入口 |
| [朝夕总 PRD](products/zhaoxi/prd.md) | 朝夕产品定位、范围与验收口径 |
| [鸣蝉总 PRD](products/mingchan/prd.md) | Native App / Companion World 产品边界与启用门槛 |
| [总体架构](architecture/overview.md) | 系统分层、依赖方向、核心链路与部署形态 |
| [核心模型与术语](architecture/core-model.md) | 平台、产品、形态、渠道、身份作用域和代码所有权如何区分 |
| [技术总平面](architecture/system_design.md) | 状态所有权、数据模型与技术平面 |
| [多产品模块化单体 ADR](architecture/shared/data/multi_product_modular_monolith_design.md) | 产品边界、身份/资产隔离与第二产品接入原则 |
| [新增产品开发清单](guides/adding-product.md) | 真实新产品开工时必须落实的代码、隔离与验收步骤 |
| [回归测试指南](guides/testing.md) | 按产品、平台和共享模块运行 PostgreSQL 测试 |
| [路线图](roadmap.md) | 稳定愿景、原则和暂不做边界 |

## 目录职责

| 目录 | 内容 | 生命周期 |
| --- | --- | --- |
| [`products/`](products/README.md) | 按 `app_id` 维护 PRD、能力、用户/API 交接 | 持续维护 |
| [`architecture/`](architecture/README.md) | 总览，以及 shared / agent-runtime / product 架构 | 持续维护 |
| [`plans/`](plans/README.md) | 按 shared / agent-runtime / product 划分的未完成计划 | 完成后归档 |
| [`ops/`](ops/README.md) | 平台与产品运行、部署、备份、排障与调试 | 随生产更新 |
| [`backlog/`](backlog/BACKLOG.md) | 按 shared / product 划分的未立项工程改进 | 立项后移入 plans |
| [`guides/`](guides/adding-product.md) | 跨 owner 的开发操作清单 | 随架构契约更新 |
| [`archive/`](archive/README.md) | 已完成交付、被取代方案和历史快照 | 冻结，只修失效链接 |

## 按角色阅读

| 读者 | 建议顺序 |
| --- | --- |
| 产品/项目负责人 | [项目状态](STATUS.md) → [路线图](roadmap.md) → [产品目录](products/README.md) → 对应产品总 PRD |
| 后端开发 | [核心模型与术语](architecture/core-model.md) → [总体架构](architecture/overview.md) → 对应 shared / agent-runtime / product 设计 → [新增产品清单](guides/adding-product.md) |
| App/客户端开发 | [鸣蝉产品 manifest](products/mingchan/README.md) → 迁移期 legacy App quickstart / 完整交接 → OpenAPI snapshot；客户端 namespace 切换由拆分计划跟踪 |
| 运维/值守 | [生产 Runbook](ops/production_runbook.md) → [平台运行入口](ops/platform/README.md) → 对应产品运行文档 |

## 维护规则

- 易变进度只写入 `STATUS.md`；长期结论回写对应产品 PRD 或对应 owner 的架构文档。
- 共享能力不写入产品目录；Runtime 不写入 `shared/` 或产品目录；产品特有语义不下沉到共享目录。
- 新产品先建立 `products/<app_id>/README.md` manifest 与总 PRD，再在有真实需求后补齐能力、架构和计划；禁止空目录占位。
- `plans/` 只允许保留确有未完成项、责任边界和验收条件的计划，并按 owner 归档。
- 已完成计划移入 `archive/deliveries/`；已被取代的方案移入
  `archive/alignments/`，并在开头明确替代文档。
- Runbook 不放在架构设计目录；部署与排障统一进入 `ops/` 的 platform 或 product owner 目录。
- 不建立 `tmp/` 作为可引用文档层。本地草稿可以存在，但当前文档不得引用它。
- 移动或删除文档时必须同步修复全部仓库内链接；`tests/test_documentation_links.py`
  会执行自动检查。
