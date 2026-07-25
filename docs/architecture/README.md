# 架构文档

本目录只承载当前有效的技术事实。

- [总体架构](overview.md)：系统边界、依赖方向、关键链路和部署拓扑。
- [技术总平面](system_design.md)：状态所有权、数据模型和横切技术平面。
- [`designs/`](designs/)：单一技术领域的详细设计与 ADR。

专题设计与总平面冲突时，应修改专题设计或显式记录新 ADR，不能依赖聊天记录解释。
已完成版本的 build spec、迁移计划和验收记录归档到 `../archive/deliveries/`。

## 当前关键专题

- [多产品模块化单体](designs/multi_product_modular_monolith_design.md)
- [系统 3.0 / Companion World](designs/companion_world_3_0_refactor_design.md)
- [身份模型与微信绑定](designs/identity_model_and_wechat_binding.md)
- [Conversation Orchestrator](designs/conversation_orchestrator_design.md)
- [主动消息与提醒](designs/proactive_messaging_design.md)
- [内容审核](designs/content_moderation_design.md)
- [PostgreSQL 厚节点](designs/thick_node_postgres_refactor.md)
