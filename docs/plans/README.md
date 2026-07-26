# 当前执行计划

这里只保留仍有未完成项的计划；按 shared、agent-runtime、product owner 分类。完成或被取代后必须移入 `../archive/`。

| 计划 | 当前未完成范围 |
| --- | --- |
| [多产品模块化单体](shared/multi_product_modular_monolith_implementation_plan.md) | MP-07A～F 已完成本地开发、待分批评审；真实第二产品等待 PRD |
| [Agent Runtime 对齐](agent-runtime/agent_runtime对齐.md) | 当前消息 typed envelope 等剩余核查 |
| [主动消息送达窗口](products/zhaoxi/主动消息送达窗口对齐.md) | 过期告知机制与 OpenClaw 补丁固化 |
| [Analytics 工程实施](shared/analytics_foundation_implementation_plan.md) | Nearline facts/marts 与 ETL 建设 |
| [基线优化](products/zhaoxi/baseline_optimization_alignment.md) | 尚未排期的 P1/P2 项 |
| [App 账号收敛与渠道人设](products/zhaoxi/app_account_convergence_and_channel_persona.md) | 以文首状态与验收表为准 |
| [Companion World App M1 服务端](products/zhaoxi/companion_world_app_m1_server_plan.md) | 客户端 M1 服务端需求评审结论与 S1–S5 批次；S1/S2 已交付，S3–S5 待开工 |

计划完成时应把稳定结论回写 `../architecture/` 或 `../products/<app_id>/`，再归档计划本身。
