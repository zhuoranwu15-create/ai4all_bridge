# 朝夕相伴接入与契约文档

这里为客户端和通道集成方提供接入入口。产品目标与用户体验回到上级产品 PRD/体验文档，机器字段
以自动校验契约为准。

## Native App

- [客户端快速接入](../app_client_brief.md)：Base URL、登录、关键分叉和必须先知道的约定。
- [完整 API 交接](../app_api_handoff.md)：端点流程、错误码、媒体与联调口径。
- [OpenAPI snapshot](../openapi/app_v1.json)：由 CI 校验的机器契约。

职责约定：quickstart 不复制完整端点/字段清单；完整交接解释流程和语义；OpenAPI 负责路径、请求/
响应 schema 和 breaking-change 检查。

## 微信/OpenClaw

- [OpenClaw Bridge 设计](../../../architecture/shared/access/openclaw_bridge_design.md)
- [微信端调试指南](../../../ops/products/zhaoxi/debugging.md)
- [OpenClaw 补丁维护](../../../architecture/shared/access/openclaw_patches_maintenance.md)
