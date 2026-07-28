# Companion World 客户端后续服务端需求清单（M2–M5）

> 文档角色：客户端 M2–M5 联调后确认的服务端增量需求与测试数据门，供后续统一向服务端提需求
> 状态：V0.3，M2 管理接口、M3 联调数据及 M4–M5 契约/双账号联调条件待服务端评审与排期
> 日期：2026-07-28
> 目标客户端里程碑：M2 世界动态完整管理闭环 → M3 信箱事务 → M4 来访 → M5 真人聊天
> 后端事实源：配套仓库正式 `/api/v1` 路由、OpenAPI 与测试

## 0. 结论

M2 客户端已经接入主人世界 Feed 的读取、游标分页、刷新、文字发布和居民动态进入私聊。M3 信箱/生命周期以及 M4/M5 非阻塞客户端功能也已经实现并通过本地类型、单测和契约检查。当前服务端仍缺少两个正式 App API，因此客户端不能提供真实的动态管理操作，也不能把 M2 或包含访客 Feed 的 M4 标记为完整发布闭环：

1. 主人删除自己发布的动态。
2. 主人隐藏 AI 居民发布的动态。

客户端不会用本地过滤或缓存删除冒充服务端成功。接口补齐前，管理入口保持隐藏；已加载内容继续以服务端 Feed 为准。

## 1. FEED-MGMT-001：主人删除自己的动态

优先级：M2 发布闭环 Blocker。

建议正式路由：

```http
DELETE /api/v1/worlds/home/feed/posts/{post_id}
Authorization: Bearer <app-session-token>
```

要求：

- 只能操作当前 Session 用户拥有的 home world，不能接受 `world_id`、`owner_id`、`account_id` 等注入字段。
- 只允许删除 `author.type=human` 且作者为当前主人的动态；AI 动态必须走隐藏语义，避免把系统生成记录与用户内容混为一类。
- 重复删除必须幂等，不产生 5xx；响应明确本次是否为重放。
- 不存在、跨 owner 和不可见资源统一返回 owner-scoped `post_not_found`，避免资源枚举。
- 删除后主人和所有有效访客再次读取 Feed 时必须立即不可见；服务端分页游标仍可继续使用或返回稳定 `invalid_cursor`，不能重复/错页。
- 与正文相关的后续媒体、审核记录和审计保留策略由服务端统一处理；客户端不直接操作对象存储。
- 返回 `Cache-Control: no-store`。

建议成功信封：

```json
{
  "code": "ok",
  "request_id": "req_...",
  "server_time": "2026-07-27T12:00:00+08:00",
  "data": {
    "post_id": "post_...",
    "status": "deleted",
    "replayed": false
  }
}
```

## 2. FEED-MGMT-002：主人隐藏 AI 居民动态

优先级：M2 发布闭环 Blocker。

建议正式路由：

```http
POST /api/v1/worlds/home/feed/posts/{post_id}/hide
Authorization: Bearer <app-session-token>
Content-Type: application/json

{}
```

要求：

- 只能由 home world 主人操作该世界内 `author.type=resident` 的动态。
- 隐藏是服务端权威状态，不是单设备 UI 偏好；主人和所有有效访客后续都不能再读到该动态。
- 操作幂等，重复隐藏返回同一业务结果并标明 `replayed`。
- 不接受客户端提供隐藏原因、居民状态、作者 ID 或世界 ID；内部审计原因由服务端记录。
- 不因隐藏动态修改居民生命周期、会话、记忆或离开状态。
- 对 `farewell/departure` 动态是否允许隐藏，应由服务端和产品共同冻结并补测试；客户端不自行猜测。
- M2 不要求“取消隐藏”。若服务端未来提供恢复能力，应作为独立受审计接口和产品决策，不依赖本地撤销。
- 返回 `Cache-Control: no-store`。

建议成功信封：

```json
{
  "code": "ok",
  "request_id": "req_...",
  "server_time": "2026-07-27T12:00:00+08:00",
  "data": {
    "post_id": "post_...",
    "status": "hidden",
    "replayed": false
  }
}
```

## 3. CONTRACT-M2-001：补齐机器可读响应与错误码

优先级：与上述两个路由同时交付。

- OpenAPI 为 Feed 读取、发布、删除和隐藏提供具体 response schema，不继续使用泛型 `additionalProperties: true`。
- 冻结 `post_not_found`、`post_not_owner` 或等价的稳定错误码；若安全上统一为 `post_not_found`，在测试中明确。
- 为删除自己的动态、拒绝删除 AI 动态、隐藏 AI 动态、跨 owner、重复请求、主人/访客立即不可见、游标边界增加 SQLite 与 PostgreSQL 测试。
- 更新客户端契约快照并提升 `client_contract_version`；feature capability 仍通过 `/app/config` 动态读取，不新增内部 flag 暴露。

## 4. 客户端接入门

服务端交付后，客户端按以下顺序开放：

1. 更新 `openapi/app_v1.json` 并通过契约检查。
2. 增加运行时 DTO 校验和错误码测试。
3. 仅在主人自己的文字动态显示“删除”；仅在 AI 居民动态显示“隐藏”。
4. 操作成功后失效 home Feed，并以服务端回读结果移除内容。
5. 生产 iOS/Android 联调验证重放、跨设备和有效访客可见性。

在上述验收完成前，M2 保持“主体可用、管理闭环待服务端”的状态。

## 5. M3-QA-001：提供可控的信箱事务联调数据

优先级：M3 生产验收门，不是新增公开 App API。

2026-07-28 已在生产 iOS 模拟器验证：`mailbox=true`、入口/鉴权/列表请求和空状态正常，当前测试账号没有来信。以下链路仍缺真实数据才能验收：详情、自动已读、稍后处理、婉拒，以及接受后原子创建 resident/conversation。

服务端/运营需要提供：

- 优先在隔离的联调环境或专用一次性账号投递一封运营审核过的测试来信，覆盖 `unread → read/deferred/declined`。
- 接受链路使用可丢弃的专用测试账号验证；不要要求个人生产账号接受测试居民，因为接受后会建立真实 active resident，当前没有用户侧移除/回滚接口。
- 测试来信继续遵守 owner 隔离、容量重校验、固定 TTL 和 `Cache-Control: no-store`；不得通过客户端本地 fixture 冒充生产成功。
- 不为此新增可被正式 App 调用的 debug/admin 注入端点；使用既有运营目录、调度或受控后台能力。
- 服务端提供投递时间、测试账号和预期 letter 状态即可，客户端不需要数据库 ID、内部模板 seed 或 runtime account。

## 6. M4 来访：主链路可开发，发布门与契约债务

当前正式路由已经覆盖邀请码创建/查看/撤销、兑换、pending 接受/拒绝/取消、active 离开/撤销，以及访客只读 Feed。客户端已完成这些非阻塞功能，不需要新增状态迁移接口；剩余工作是下列发布门和双账号联调。

### M4-RELEASE-001：先关闭主人 Feed 管理缺口

优先级：M4 广泛发布门。

有效访客会读取主人选择发布的世界 Feed。若主人仍不能通过正式 API 删除自己的动态或隐藏 AI 动态，访客可见面会先于主人内容控制完成。M2 的 `FEED-MGMT-001/002` 因此同时是 M4 发布门；在服务端补齐并联调前，客户端可以完成来访读取与状态机，但不宣称访客 Feed 已具备完整的主人控制闭环。

### M4-CONTRACT-001：为 invites/visits/visitor Feed 补具体响应 schema

优先级：正式发布前 Required。

- 当前 OpenAPI 中 M4 多数成功响应仍为 `additionalProperties: true`；请为 `WorldInvite`、`WorldVisit`、接受结果和 visitor Feed page 提供具体 schema。
- `WorldVisit` 冻结 `role/status/counterpart_display_name/pending_expires_at/expires_at/terminal_at/terminal_reason` 的 nullable 与枚举语义。
- 每个响应继续返回带时区的 `server_time`，供客户端显示相对剩余时间；授权仍以服务端每次请求实时判断为准。
- 为 invite 明文只返回一次、大小写敏感、各角色非法 action、精确到期边界和 `no-store` 补 OpenAPI 示例与契约测试。

### M4-PRESENTATION-001：补充可选的公开展示摘要

优先级：不阻塞 M4 主链路，但限制好友世界的辨识体验。

当前 visit 只返回对方展示名，visitor Feed 也不返回世界简介或居民公开摘要。若产品希望好友世界入口具备更清楚的辨识信息，建议在 owner 审核过的公开投影中增加可空 `counterpart_avatar_ref`、`world_display_name/world_intro`；不要返回居民私聊、AI 记忆、内部 world/user/runtime ID。客户端在字段交付前只显示文字头像和对方名称，不自行拼装私人资料。

### M4-QA-001：双账号端到端联调条件

优先级：生产验收门。

服务端/运营需要准备两个已完成 world onboarding 的 App 测试账号，允许验证完整的 `24h invite → 7d pending → 30d active`：

- A 创建并分享一次性 code，B 登录后兑换；登录 API 不携带 code。
- pending 阶段 B 读取 Feed/发送均被拒绝；A 接受后才原子创建真人会话。
- A/B 分别覆盖 reject/cancel/revoke/leave；终态后 visitor Feed 缓存清除、双方发送关闭、历史只读。
- 验证世界三 slot、访客跨世界三 open visit 和重放/并发边界。

## 7. M5 真人聊天：可开发部分与服务端阻塞

当前正式路由已经覆盖会话列表、历史、幂等文字发送、标记已读、终态只读、当前用户隐藏、拉黑和举报。客户端已完成列表/历史/发送/标记已读/拉黑/终态隐藏；以下能力在契约补齐前继续降级。

### M5-CONV-001：补齐会话列表读模型

优先级：M5 完整列表体验 Blocker。

当前 `GET /human-conversations` 只有 `status/last_message_at/last_read_at`，不足以安全实现未读数和最后消息预览。建议增加：

- `last_preview: string | null`：经过与历史相同的 participant 投影和正文清洗。
- `unread_count: integer >= 0`：服务端按 participant read marker 计算。
- `can_send: boolean` 与稳定 `read_only_reason`；至少区分 `visit_ended` 与 `feature_disabled`。
- `expires_at: string | null`：active conversation 对应 visit 的绝对结束时间，避免客户端额外 join 两个列表。

在字段交付前，客户端只显示“真人来访/只读历史”和时间，不伪造未读 badge 或正文预览；发送仍由服务端最终门禁。

### M5-REPORT-001：提供举报原因公共契约

优先级：举报 UI Blocker；单独拉黑不受阻。

`POST /human-conversations/{id}/report` 要求 `reason_code`，但 App API 没有允许值、用户文案或版本。服务端需二选一：

1. 新增 authenticated public options endpoint，返回版本化 `reason_code + label + details_required`；或
2. 在 OpenAPI/冻结契约中给出长期稳定的受控枚举及中文展示口径。

客户端不会凭测试里的 `harassment` 单个示例自行发明举报分类。字段交付前保留“拉黑”安全动作，不开放会产生未知 reason code 的举报提交。

### M5-NOTIFY-001：冻结真人会话通知范围与关闭契约

优先级：不阻塞站内列表/历史/发送主链路，但阻塞 PRD `HUMAN-05` 的完整正式版口径。

当前冻结 M5 后端规格明确排除 Push，正式 App API 也没有按真人会话关闭通知的字段或端点。现有 `/notifications/preferences` 是 AI 主动通知节奏，客户端不能把它解释为真人会话静音。服务端与产品需二选一：

1. 明确将真人消息 Push/会话级静音后置，并在当前 M5 发布范围中移除“通知关闭已完成”的声明；或
2. 增加 participant-scoped 的会话通知偏好 DTO/更新接口和独立 capability，冻结默认值、终态行为、多设备同步与实际投递语义。

客户端在契约交付前不显示无效的“消息免打扰”开关，也不会用纯本地开关假装服务端或其他设备已经停止通知。

### M5-CONTRACT-001：补齐真人聊天具体响应 schema

优先级：正式发布前 Required。

- 为 conversation list、message page、send/read/hide/block/report 成功响应提供具体 schema；当前泛型响应由客户端运行时校验兜底，但不能长期替代机器契约。
- 冻结 message page 为 `sequence DESC`、opaque cursor，发送重放返回原 `message_id` 和 `created=false`。
- 明确 `human_chat_send=false` 只关闭写入；历史、隐藏、拉黑和举报仍可用。
- 所有响应保持 `Cache-Control: no-store`，终态历史只读但不落客户端持久数据库。

### M5-QA-001：真人聊天双账号验收

优先级：生产验收门。

- 复用 M4 已接受的 active visit，验证双方发送、同一 `client_message_id` 重放和正文冲突。
- 到期、revoke、leave 和任一方 block 后，双方立即收到只读状态；历史仍可分页读取。
- 隐藏只影响当前 participant，不能删除对方历史或举报证据。
- 服务端提供举报原因契约后，再验证 evidence snapshot 与可选 block；客户端不得读取或缓存 snapshot。

## 8. 当前客户端降级约定

- M3 无来信：显示安静空状态，不伪造测试信。
- M4 对方头像/世界摘要缺失：使用文字头像和展示名，不读取内部资料。
- M4 visit 终态：立即删除 `visited-feed/{visit_id}` 内存缓存，不把好友 Feed 持久化到设备。
- M5 会话列表缺未读/preview：不显示假 badge 或假摘要。
- M5 缺举报原因：暂不显示举报提交入口；拉黑和终态隐藏仍使用正式服务端接口。
- M5 缺真人会话通知偏好：不把 AI 主动通知节奏冒充真人会话静音，也不显示只在单设备生效的假开关。
- 所有 capability 明确关闭时隐藏新增入口；网络失败、5xx 或非法响应显示可恢复错误，不能解释成 capability 关闭。
