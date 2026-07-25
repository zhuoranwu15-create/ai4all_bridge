# AI4ALL 文档导航

更新时间：2026-07-25

文档按“需求、架构、执行、运维、历史”分层。当前事实只在需求、架构和状态文档维护；
`archive/` 只提供历史背景，不参与当前决策。

## 当前事实入口

| 文档 | 回答的问题 |
| --- | --- |
| [项目状态](STATUS.md) | 现在运行到哪里、近期在做什么、有哪些已知缺口 |
| [总 PRD](prd.md) | 产品定位、总范围与验收口径 |
| [产品专题](product/README.md) | 单项产品能力的详细需求 |
| [总体架构](architecture/overview.md) | 系统分层、依赖方向、核心链路与部署形态 |
| [技术总平面](architecture/system_design.md) | 状态所有权、数据模型与技术平面 |
| [多产品模块化单体 ADR](architecture/designs/multi_product_modular_monolith_design.md) | 产品边界、身份/资产隔离与第二产品接入原则 |
| [路线图](roadmap.md) | 稳定愿景、原则和暂不做边界 |

## 目录职责

| 目录 | 内容 | 生命周期 |
| --- | --- | --- |
| [`product/`](product/README.md) | 当前产品专题 PRD | 持续维护 |
| [`architecture/`](architecture/README.md) | 总体架构、技术总平面、专题设计 | 持续维护 |
| [`plans/`](plans/README.md) | 仍有未完成项的执行计划 | 完成后归档 |
| [`guides/`](guides/debugging.md) | 用户、Admin、开发调试指南 | 随行为更新 |
| [`ops/`](ops/production_runbook.md) | 生产部署、运行、备份与节点 runbook | 随生产更新 |
| [`troubleshooting/`](troubleshooting/weixin_duplicate_replies.md) | 可复用故障定位记录 | 根因变化时更新 |
| [`backlog/`](backlog/BACKLOG.md) | 尚未立项的工程改进 | 立项后移入 plans |
| [`archive/`](archive/README.md) | 已完成交付、被取代方案和历史快照 | 冻结，只修失效链接 |

## 维护规则

- 易变进度只写入 `STATUS.md`；长期结论回写 PRD 或 `architecture/`。
- `plans/` 只允许保留确有未完成项、责任边界和验收条件的计划。
- 已完成计划移入 `archive/deliveries/`；已被取代的方案移入
  `archive/alignments/`，并在开头明确替代文档。
- Runbook 不放在架构设计目录；部署操作统一进入 `ops/` 或 `guides/`。
- 不建立 `tmp/` 作为可引用文档层。本地草稿可以存在，但当前文档不得引用它。
- 移动或删除文档时必须同步修复全部仓库内链接；`tests/test_documentation_links.py`
  会执行自动检查。
