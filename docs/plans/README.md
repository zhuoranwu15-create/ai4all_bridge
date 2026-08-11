# 当前执行计划

这里只保留仍有未完成项的计划；按 shared、agent-runtime、product owner 分类。完成或被取代后必须移入 `../archive/`。

| 计划 | 当前未完成范围 |
| --- | --- |
| [朝夕相伴 / 鸣蝉产品域拆分](shared/zhaoxi_mingchan_product_split_plan.md) | 将微信业务保留为 `zhaoxi`，把原 Native App / Companion World 拆为全新 `mingchan` 产品；不迁移旧 App 测试数据，完成后受控清理 |
| [文档事实收敛与信息架构整理](shared/documentation_reconciliation_plan.md) | 当前事实裁决、高风险过期文档回写、产品/架构入口整理与持续治理门禁 |
| [多产品模块化单体](shared/multi_product_modular_monolith_implementation_plan.md) | MP-07A～F 已完成本地开发、待分批评审；真实第二产品接入由朝夕/鸣蝉拆分计划承接 |
| [Agent Runtime 对齐](agent-runtime/agent_runtime对齐.md) | 当前消息 typed envelope 等剩余核查 |
| [主动消息送达窗口](products/zhaoxi/主动消息送达窗口对齐.md) | 过期告知机制与 OpenClaw 补丁固化 |
| [Analytics 工程实施](shared/analytics_foundation_implementation_plan.md) | Nearline facts/marts 与 ETL 建设 |
| [基线优化](products/zhaoxi/baseline_optimization_alignment.md) | 尚未排期的 P1/P2 项 |
| [App 账号收敛与渠道人设](products/zhaoxi/app_account_convergence_and_channel_persona.md) | 以文首状态与验收表为准 |
| [用户自建角色模板与邀请链接](products/zhaoxi/creator_role_template_referral_link_implementation_plan.md) | P0 模板、审核、组合注册链接、真实新账号实例化、聚合统计与后台管理；不含试玩 |
| [Plum 注册登录与用户创建角色开发准备](products/plum/registration_login_character_create_readiness.md) | 冻结海外身份接入后的 Plum 会话编排，以及用户创建 Character 的审核、幂等和原子发布契约 |

计划完成时应把稳定结论回写 `../architecture/` 或 `../products/<app_id>/`，再归档计划本身。
尚未进入实施排期的产品事项统一从
[`../backlog/products/zhaoxi/`](../backlog/products/zhaoxi/README.md) 查找，不用已完成计划继续追踪。
