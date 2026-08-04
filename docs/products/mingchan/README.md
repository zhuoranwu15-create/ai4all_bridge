# 鸣蝉产品文档

更新时间：2026-08-04

| 项目 | 当前事实 |
| --- | --- |
| 产品名 / `app_id` | 鸣蝉 / `mingchan` |
| 发布状态 | 仓库开发及开发机验证已完成；生产注册保持禁用，首次发布待 MC-07 |
| 代码命名空间 | `app/products/mingchan/` |
| 产品渠道 | Native App（`native`） |
| 核心业务 | Companion World、居民会话、Feed、通知、信箱、生命周期、访问、真人会话与许愿 |
| 规范产品入口 | `/api/v1/products/mingchan/*` |
| 反代剥离兼容入口 | `/v1/products/mingchan/*` |

[总 PRD](prd.md) 定义产品定位与隔离边界；[Companion World App PRD](capabilities/companion_world_app_prd.md)
描述产品能力；[App 接入交接](app_api_handoff.md)、[客户端简要说明](app_client_brief.md)和
[OpenAPI snapshot](openapi/app_v1.json)共同构成客户端契约入口。产品专属架构见
[`architecture/products/mingchan/`](../../architecture/products/mingchan/README.md)。

鸣蝉与朝夕共享 `platform_user`、Agent Runtime 和 Platform，但独立拥有 membership、session、runtime
account、钱包/配额/referral 作用域、API、工具策略、生命周期、scheduler 与产品资源。鸣蝉代码不得
import `app.products.zhaoxi`；World/resident 查询必须同时校验 `universes.app_id=mingchan` 与居民 runtime
account 的 `app_id=mingchan`。

仓库已经具备固定 audience 的 OTP→session→`/me`→注销、World onboarding、居民 turn、会话、Feed、
通知、信箱、访问、真人聊天、媒体与许愿链路；World lifecycle worker 同时承接鸣蝉通知清理、孤儿媒体
回收和媒体审核维护。旧朝夕 App/World 数据不迁移，启用前只允许按
[首次启用检查单](../../ops/products/mingchan/production_first_enablement.md)执行受控 precheck/cleanup。

开发机聚焦门禁、双后端全量回归及隔离临时库 cleanup 演练已完成。生产启用、外部 Native App 仓库
切换、线上 cleanup 与真机验证尚未执行。详见
[拆分计划](../../plans/shared/zhaoxi_mingchan_product_split_plan.md)。
