# 鸣蝉产品需求文档

更新时间：2026-08-04

> 归属：`product:mingchan`。本文先冻结产品边界与拆分验收；现有 Companion World 详细需求在
> 文档迁移完成前暂沿用 legacy 文件
> [`capabilities/companion_world_app_prd.md`](capabilities/companion_world_app_prd.md)，
> 其中产品名和 `app_id` 以本文为准。

## 1. 产品定位

鸣蝉是面向 Native App 的独立 AI 陪伴产品。用户拥有一个私人 Companion World，可与多位独立
居民建立关系，并使用世界动态、通知、信箱、居民生命周期、限时访问和真人会话等 App 原生能力。

鸣蝉不是朝夕相伴的客户端渠道。两者可复用同一真人 `platform_user`、Agent Runtime 和 Platform，
但业务目标、产品规则、API、数据作用域和演进节奏相互独立。

## 2. 产品边界

鸣蝉负责：

- Native App 登录、产品 membership 与 session；
- universe、resident、relationship、Feed、通知、信箱、生命周期、访问和真人聊天；
- 居民对应的 runtime account 创建与产品内隔离；
- 鸣蝉独立的 `ProductTurnServices`、`ToolPolicy`、lifecycle、功能开关和产品文案；
- 鸣蝉客户端契约、OpenAPI、Admin/运营入口和发布节奏。

鸣蝉不负责微信扫码、OpenClaw bridge、微信主动触达或朝夕相伴的 onboarding/profile/tool policy。

## 3. 身份与隔离原则

- 产品标识固定为 `app_id=mingchan`，不接受客户端 Header 动态选择产品。
- `platform_user` 可跨产品复用；`product_membership`、session、runtime account、钱包、配额、邀请
  和产品资产必须按 `mingchan` 作用域创建和校验。
- 每位居民映射到独立 runtime account；所有 Soul、上下文、会话和记忆继续按 `account_id` 隔离。
- 鸣蝉代码不得 import 朝夕产品实现；共享能力只能通过 Platform 或形态无关 Agent Runtime 使用。

## 4. 拆分与数据策略

鸣蝉当前没有真实用户，按全新产品建立，不迁移旧 App/World 用户或业务数据。旧数据仅作为拆分
期间的临时验证样本；代码与双后端验证完成后先备份、dry-run，再受控清理。默认保留共享
`platform_users`，不得批量删除朝夕账号、微信绑定、消息、钱包或主动任务。

## 5. 启用门槛

- App/World 代码、路由、任务、工具和资源均归入 `app/products/mingchan/`；
- 登录、World bootstrap、居民 turn 全链路只创建/接受 `app_id=mingchan`；
- 朝夕微信/Web 回归通过，两个产品不存在相互 import 或跨产品资产访问；
- SQLite 聚焦测试与 PostgreSQL 持久化/并发回归通过；
- legacy App/World 数据完成受控清理，且朝夕关键数据计数无非预期变化；
- 客户端切换到鸣蝉 namespace 后，才启用生产注册项和产品路由。

执行进度与逐项验收以[朝夕相伴 / 鸣蝉产品域拆分计划](../../plans/shared/zhaoxi_mingchan_product_split_plan.md)
为准。
