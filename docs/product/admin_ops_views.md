# 运营后台页面规划

更新时间：2026-05-30

本文承接 [运营与后台 PRD](admin_ops_prd.md)，只描述运营视角下的后台页面、字段、动作和优先级。技术 Debug 页面和命令统一收口到 [调试指南](../debugging.md)，不在这里展开。

## 1. 定位

运营后台的核心目标是让内部团队基于账号、绑定、会话元数据、任务状态、用量和权益流水处理内测问题。

后台不是查看用户私聊内容的工具。默认页面只展示状态、元数据、统计、摘要和脱敏结果。明文访问属于例外流程，必须走授权和审计。

## 2. 页面原则

- 以 AI4ALL Account 为核心入口，而不是以 OpenClaw payload、微信 sender 或历史 `contacts` 表为核心。
- 所有排障页面都应能回答三个问题：这个账号是谁、当前通道是否可达、最近一次失败卡在哪个环节。
- 运营页面只承载稳定业务流程；状态跳转、reset、模拟消息、直接改 DB 等能力归入技术 Debug。
- 页面先聚合既有 Admin API，不急着引入复杂 BI、工单系统或独立权限后台。
- 每个高风险操作必须留下操作记录：禁用账号、恢复账号、补发权益、取消任务、查看明文、审批明文权限。

## 3. 总入口

当前 `/ui/index.html` 是后台总入口，分为两个区域：

- 运营后台：账号列表、账号详情、绑定排障、明文授权、访问审计、后续权益/贝壳与客服记录。
- 技术 Debug：Onboarding Debug、Reminder Debug、Swagger `/docs`、调试指南链接。

短期保留现有账号列表、明文授权和访问审计在首页下半部分，不需要立刻新增复杂导航系统。

## 4. Phase 1 页面清单

| 页面 | 目标 | 默认字段 | 允许动作 | 隐私边界 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| 后台首页 | 给运营一个统一入口和异常概览 | 账号总数、今日消息量、失败任务数、待审批明文申请、最近明文访问 | 跳转到具体页面 | 不展示正文 | P0 |
| 账号列表 | 快速定位用户和账号状态 | account id、手机号后四位或脱敏手机号、状态、通道、今日用量、最近活跃、备注 | 搜索、筛选、进入详情、禁用/启用 | 不展示聊天正文 | P0 |
| 账号详情 | 单账号排障和客服处理主页面 | 基本资料、绑定状态、会话元数据、用量、限流、备注、profile 元数据 | 修改备注/显示名/限流、禁用/启用、进入会话/提醒/权益视图 | profile 正文默认脱敏 | P0 |
| 绑定排障 | 处理扫码、误绑、重复绑定、解绑异常 | platform user、account owner binding、binding intent、channel binding、last seen、错误 | 重新生成绑定意图、查看绑定状态、按规则解绑/重绑 | raw payload 正文不展示 | P0 |
| 会话与消息元数据 | 追踪一次对话链路 | session id、message id、时间、角色、类型、状态、耗时、错误、token、cost、trace id | 展开元数据、申请明文、重置会话 | 消息正文和模型回复默认脱敏 | P0 |
| 提醒与主动消息 | 排查 reminder、commitment、outbound 和 scheduler | reminder 状态、due_at、attempts、outbound id、失败原因、route、scheduler 状态 | 取消异常任务、手动 run-once、查看 proactive state | reminder 文本按正文类敏感信息处理，默认可只展示摘要/长度 | P1 |
| 权益与贝壳 | 处理误扣、补发、邀请奖励 | 余额、ledger、变动原因、关联 message/task/order、referral 关系 | 补发、冲正、记录原因 | 不展示聊天正文 | P1 |
| 明文授权 | 管理临时明文权限 | 申请人、原因、账号范围、资源范围、时间范围、状态、过期时间 | 申请、批准、拒绝、撤销 | 页面本身不展示明文 | P0 |
| 访问审计 | 追踪高风险访问和操作 | 操作人、时间、动作、资源、账号、是否明文、原因 | 搜索、筛选 | 不展示被访问正文 | P0 |
| 客服记录 | 留存基础处理记录 | 账号、问题类型、处理人、处理结论、关联资源 | 新增/更新记录 | 用户主动提供材料也应最小化保存 | P2 |
| 支付订单 | 支付开启后排查订单 | order id、状态、金额、provider、回调状态、权益发放状态 | 查询、补发、标记处理 | 不展示敏感支付凭据 | P3 |

## 5. 账号详情聚合视图

账号详情是 Phase 1 最重要的页面，建议按以下区块组织：

1. 基本信息：`accounts`、`platform_users`、状态、备注、显示名、创建时间、最近活跃。
2. 通道绑定：`channel_bindings`、channel account、session key、sender/chat、last seen。
3. 绑定链路：最近 `binding_intents`、`account_owner_bindings`、失败原因。
4. 用量与限流：今日消息数、近 7 日消息数、daily/rpm limit、最近限流记录。
5. 会话与消息：session 列表、message metadata、trace id、错误、耗时。
6. 主动消息：reminders、commitments、outbound messages、proactive account state。
7. 权益与增长：wallet/entitlement ledger、referral relationship。
8. 隐私与审计：明文授权状态、该账号最近明文访问记录。

这样一个页面可以覆盖大多数客服和运营排障，不必一开始拆很多独立页。

## 6. 绑定排障视角

绑定问题是内测高频问题，应提供一条清晰链路：

```text
手机号 / platform_user
-> AI4ALL account
-> account_owner_binding
-> binding_intent
-> channel_binding
-> 最近 inbound message / session
```

页面应优先暴露：

- 当前手机号是否已有账号。
- 当前账号是否已有 owner binding。
- 最近 binding intent 是否 expired、completed、failed、cancelled。
- channel binding 是否 active，是否存在多个 active route。
- OpenClaw / WeChat route 最近是否收到消息。
- 失败时的错误码、时间和 trace id。

## 7. 提醒与主动消息视角

运营视角需要看“为什么没发、为什么重复发、发到哪里了”，而不是直接调试 parser。

建议字段：

- reminder/commitment/outbound id。
- source：reminder、companion_followup、content_push、content_invitation。
- account id、channel、route、to_user_id/session key。
- due_at、claimed_at、sent_at、cancelled_at、attempts。
- status、error、provider response metadata。
- idempotency key。
- scheduler 最近运行时间和 run-once 结果。

强状态操作要克制：运营后台可以取消异常任务、触发一次 scheduler run、查看失败原因；编辑 reminder 文本、模拟聊天创建 reminder 等能力保留在 Debug 页面。

## 8. 明文与脱敏策略

运营后台生产默认脱敏：

- messages、raw payload、debug trace、prompt/messages、daily notes、user profile 正文默认不可见。
- 普通后台用户查看明文必须有管理员审批后的临时权限。
- 管理员明文查看也要记录原因和审计事件。

开发阶段为了效率，可以在开发机通过环境变量开启明文 Debug；该策略只作为开发便利，不改变生产验收标准。具体约定见 [调试指南](../debugging.md#开发期明文策略)。

## 9. 最小推进顺序

1. 账号列表和账号详情补齐：账号、绑定、会话、用量、备注、禁用/启用。
2. 明文授权和访问审计闭环：申请、审批、明文接口、日志。
3. 绑定排障视图：串起 platform user、binding intent、channel binding。
4. 消息元数据和 trace 视图：能从一条消息定位到 session、provider 错误和 trace id。
5. 提醒/主动消息视图：reminders、commitments、outbound、scheduler。
6. 权益与贝壳视图：ledger、误扣排查、补发。
7. 客服记录和支付订单后置。

## 10. 待确认

- 账号搜索是否以手机号、account id、channel account id 三者作为 Phase 1 搜索入口。
- 运营是否需要独立“绑定排障页”，还是先聚合在账号详情中。
- 补发贝壳是否需要双人审批，Phase 1 可先不做。
- 测试账号白名单的命名、数量和上线切换流程。
