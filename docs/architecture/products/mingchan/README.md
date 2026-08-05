# 鸣蝉产品架构

更新时间：2026-08-04

归属：`product:mingchan`。鸣蝉是 Native App / Companion World 产品，不是朝夕的一种渠道。

固定边界：

- 产品代码位于 `app/products/mingchan/`，固定 `app_id=mingchan`、允许渠道 `native`；
- universe、resident、relationship、Feed、App inbox、mailbox、lifecycle、visit、human chat 和 wish
  均由鸣蝉拥有；
- API 由 `app/products/mingchan/manifest.py` 组合到 `/api/v1/products/mingchan/*`，客户端不能通过
  Header 动态选择产品；
- `MingchanTurnServices` 与 `MINGCHAN_TOOL_POLICY` 通过中性端口接入 Agent Runtime；鸣蝉不得 import
  朝夕，Runtime/Platform 也不得反向 import 产品；
- `platform_user` 可跨产品共享，但 membership/session/account/World/媒体/通知访问必须验证鸣蝉作用域。

主要代码组织：

- `api/`：身份、ME、World onboarding、会话、Feed、通知、信箱、访问、真人聊天与媒体 HTTP 契约；
- `application/`：身份、turn、World/会话/记忆、主动投递及各业务用例编排；
- `domain/companion_world/`：World、居民、会话、Feed、通知、生命周期等领域模型与端口；
- `infrastructure/`：鸣蝉账号、World repository、App inbox、通知偏好与双数据库 persistence；
- `tools/`：鸣蝉居民允许使用的工具定义、handler 与 `MINGCHAN_TOOL_POLICY`；
- `jobs/`：World content、World lifecycle 与媒体审核；独立入口位于 `scripts/run_mingchan_*`；
- `lifecycle.py` / `manifest.py`：产品启停、路由与任务组合边界。

Schema 已为 `universes`、`character_templates`、App notifications/preferences 增加产品锚；运行时读写默认且
强制使用 `MINGCHAN_APP_ID`。历史 `legacy_primary_account_id` 列仅作为受控清理识别信息保留，不进入
鸣蝉领域模型、主动投递或居民 bootstrap。详细决策见
[Companion World 3.0 ADR](companion_world_3_0_refactor_design.md)。

当前生产注册仍为 disabled。开发机双后端回归已完成；生产启用、清理和真机验收见
[拆分计划](../../../plans/shared/zhaoxi_mingchan_product_split_plan.md)与
[首次启用检查单](../../../ops/products/mingchan/production_first_enablement.md)。
