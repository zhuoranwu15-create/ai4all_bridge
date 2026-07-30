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
| [Companion World App M1 服务端](products/zhaoxi/companion_world_app_m1_server_plan.md) | 客户端 M1 服务端需求评审结论与 S1–S6 批次；S1–S6 已全部交付（2026-07-26），联调 ready，待合入 main |
| [Companion World App M2 主人 Feed 管理](products/zhaoxi/companion_world_app_m2_feed_management_plan.md) | 客户端 M2–M5 需求评审；P0（删除/隐藏动态 + 契约）与 P1（举报原因契约、真人会话读模型）已落地，P2/运营项见文末 §8 |
| [Companion World App v1.5 服务端](products/zhaoxi/companion_world_app_v1_5_server_plan.md) | 客户端 v1.5 需求评审已完成、方案已定稿；S0–S5 已全部交付（2026-07-30），联调 ready，待合入 main。撤回与任务章节已移出，见文末 §8 |

计划完成时应把稳定结论回写 `../architecture/` 或 `../products/<app_id>/`，再归档计划本身。
