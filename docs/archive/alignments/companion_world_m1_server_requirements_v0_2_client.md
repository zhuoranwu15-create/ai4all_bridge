# Companion World M1 服务端需求积压

> **归档说明（2026-07-26）**：本文是**客户端仓库**提出的服务端需求清单 V0.2，作为跨仓库对齐
> 材料冻结于此，不作为当前决策依据。服务端逐条评审结论与开发计划见
> [Companion World App M1 服务端计划](../deliveries/companion_world/companion_world_app_m1_server_plan.md)，
> 其中包含对本文若干条目的**改判**（COMPAT-001、NAME-001、CONTRACT-001）与
> **本文未覆盖的新增阻断**（BOOT-003、SEC-001、IDEM-001、ERR-002）。
> 正文中指向客户端仓库的相对链接已改为纯文本。

> 文档角色：客户端 M1 开发前的服务端需求清单与交接口径修正
> 状态：V0.2，待客户端/服务端联合评审
> 日期：2026-07-26
> 目标客户端里程碑：M0 架构底座 → M1 多居民闭环
> 输入文档：`app_api_handoff.md`、私人平行世界 PRD `ai_companion_universe_prd.md`、客户端架构 `mobile_client_architecture.md`（均为客户端仓库）
> 后端事实源：配套仓库正式路由、DTO、测试与 Companion World 3.0 ADR

## 0. 结论

生产 `/api/v1` 已可访问，2026-07-26 对 world bootstrap、Feed、mailbox、visits、notifications 和 human conversations 的无凭证探测均返回 401，说明请求已通过对应 feature gate 并进入鉴权门。现有世界初始化、4 位候选确认、居民列表、AI conversation 历史与 turn 已足以支撑 M1 主干。

但 M1 还不能只按 `app_api_handoff.md` 直接开工，存在 4 个需要先完成的服务端需求：

1. `/app/config` 没有公开 Companion World capability，客户端无法可靠区分“明确关闭”和“服务故障”。
2. 新用户 `account=null` 时，当前 `GET /me` 仍强制解析 legacy account，Session 恢复会得到 `account_not_ready`。
3. 已确认保留完整自建角色，但当前创建 DTO 只有 `name + persona_hint`，无法持久化头像、关系定位、性格风格与确认摘要。
4. 初始候选缺少服务端建议实例姓名、稳定 persona 标识和命名状态，客户端无法实现“服务端优先、本地小姓名池兜底”的确定性策略。

前三项是硬发布阻断；命名字段需要在正式联调前交付，但字段缺失、为空或命名子流程失败时，客户端允许走兼容兜底，不能让单次命名失败拖垮整个 bootstrap。会话时间/未读语义、候选 preview、OpenAPI snapshot 和统一错误信封也应在 M1 联调前收口。Feed 删除/隐藏和媒体能力进入后续积压。

## 1. 已确认产品口径

- 首个正式开发里程碑为 M0 → M1；P2–P5 分开发布。
- 初始候选固定 4 位，至少保留 1 位；确认前可恢复选择，确认后首阶段不允许用户主动移除 active AI 居民。
- 不设置主角色；所有 AI 会话使用显式 `conversation_id`。
- M1 初始居民姓名默认采用服务端为该 world/candidate 固定的建议实例姓名；字段缺失、为空或命名子流程失败时，客户端使用每个 persona 3–5 个名字的版本化本地姓名池稳定兜底。
- 无论姓名来自服务端还是本地兜底，客户端确认时都通过 `selections[].display_name` 提交，确认后服务端 resident DTO 的 `name` 是唯一权威值。
- 自建角色保留名称、头像、关系定位、性格风格和摘要预览。
- 具体真人复刻、已故亲友纪念和未成年人形象后置；M1 只允许抽象关系原型。
- 世界来访码不进入认证；auth DTO 中的历史 `invite_code` 是 referral 字段，不是 Companion World visit code。

## 2. 对 `app_api_handoff.md` 的接入口径修正

| 原交接口径 | 核对结论 | M1 正确用法 |
| --- | --- | --- |
| `auth/session.invite_code` 是世界访问邀请码 | 与当前代码不符；该字段进入历史 referral 注册逻辑 | 世界来访码以后只提交到认证后的 `POST /visits/redeem`；M1 auth 不提交该字段 |
| `account == null` 进入世界引导，`account != null` 直接进主聊天 | 只能兼容旧客户端，不能代表权威 world onboarding 状态 | capability 开启时所有已登录用户都调用幂等 home bootstrap；以 `world.onboarding_state` 决定引导/主页 |
| 登录恢复调用 `/me` | 新用户未确认居民时会因没有 account 返回 `account_not_ready` | 完成 `BOOT-001` 前，客户端不能把 `/me` 作为新世界 Session 恢复的唯一接口 |
| `/app/config` 提供功能开关 | 生产响应当前只有 `voice_input` | 完成 `CAP-001`，公开世界能力；404 不能再兼任“功能关闭”和“资源不存在” |
| 候选的 `name` 就是最终居民名 | 产品区分模板名、建议实例名和已确认实例名 | 默认使用 `suggested_display_name`；命名不可用时才走本地小姓名池，最终值由客户端提交并从 resident DTO 回读 |
| 世界类能力已全量可用 | 生产探测说明当前已启用，但运行值是动态的 | 客户端仍只信公共 capability/权威响应，不把 2026-07-26 状态硬编码进包 |
| 精简入口 `app_client_brief.md` | 当前客户端仓库中没有该文件 | 以本文件和正式 OpenAPI snapshot 作为 M1 交接入口 |

## 3. M1 权威启动流程

```text
GET /app/config
  ├─ resident_world 明确关闭 → legacy 单 Agent
  ├─ 请求失败/响应异常 → 可恢复错误，不切换关系模型
  └─ resident_world 开启
       ↓
OTP → POST /auth/session
       ↓
读取可在 account=null 时工作的 Session/bootstrap 摘要
       ↓
POST /worlds/home/bootstrap（所有 world-capable 用户均调用，幂等）
  ├─ onboarding_state=selecting → 候选选择/可选自建/确认
  └─ onboarding_state=confirmed → residents + conversations → 三 Tab
```

客户端不得长期使用 `is_new_user` 或 `account == null` 判断 world onboarding。它们只用于兼容旧认证响应；世界状态以服务端 home world 为准。

## 4. M1 发布阻断需求

### CAP-001：公开 Companion World capability

优先级：M1 Blocker。

扩展 `GET /api/v1/app/config`，至少公开：

```json
{
  "features": {
    "voice_input": false,
    "resident_world": true,
    "world_feed": true,
    "app_notifications": true,
    "resident_lifecycle": true,
    "mailbox": true,
    "world_visits": true,
    "human_chat": true
  },
  "client_contract_version": "2026-07-26",
  "server_time": "2026-07-26T12:00:00+08:00"
}
```

要求：

- 字段只加不改，旧客户端继续读取 `voice_input`。
- capability 表示当前公网 App API 可用性，不直接泄露内部 scheduler、阈值或安全策略开关。
- `resident_world=false` 时服务端保持 legacy auth 行为；`true` 时客户端走权威 world bootstrap。
- 网络失败、5xx、非法响应与 `false` 是不同状态。
- 为各 capability 增加契约测试，避免生产开关已变而公共 config 未同步。

### BOOT-001：未完成居民确认时也能恢复 Session

优先级：M1 Blocker。

当前问题：`GET /me` 调用 `_account_for_user`，新用户 `account=null` 时返回 `account_not_ready`。App 被杀进程、切后台或跨设备恢复时无法可靠回到未完成的世界引导。

建议二选一：

1. 扩展 `/me`：`account` 允许为 `null`，同时返回 `world.onboarding_state`；或
2. 新增 `/app/bootstrap`：返回 platform user、nullable legacy account、home world/onboarding 摘要、capabilities 和 `server_time`。

无论选择哪种，必须满足：

- 只凭有效 Session 即可调用，不要求已有 runtime account。
- `selecting/confirmed` 可跨重启恢复。
- 401 只表示 Session 失效；`account_not_ready` 不再被客户端误判为登录过期。
- 响应 `Cache-Control: no-store`。

### COMPAT-001：旧客户端与 P1 auth 行为兼容

优先级：存在已分发旧包时为 M1 Blocker。

P1 开启后，新用户的 `auth/session.account` 从默认对象变为 `null`。若旧客户端假定登录成功后必有 account，会出现白屏、循环登录或错误进入 legacy chat。

要求：

- 盘点当前所有已分发 iOS/Android 版本对 `account=null` 的行为。
- `/app/config` 公布能理解 world onboarding 的最低客户端版本；必要时按平台分别配置。
- 不兼容客户端在认证前得到明确升级门，不能先创建 Session 再崩溃。
- 如果服务端做版本/灰度分流，客户端版本 Header 和分流规则必须进入 OpenAPI、日志与回归测试。
- 未分发任何旧包时可记录为“不适用”，但需保留证据，不凭印象跳过。

### CUSTOM-001：结构化自建角色预览与创建

优先级：M1 Blocker。

当前 `CreateResidentPayload` 只有 `template_id` 或 `name + persona_hint`。M1 至少需要服务端权威承载：

- `name`；
- `avatar_key` 或受控 `avatar_ref`；
- `relationship_type`；
- `relationship_label`（仅“其他”使用）；
- 受控 `personality_traits`；
- 用户确认过的 `persona_summary`；
- `client_request_id`，用于重复点击/断网重试幂等。

推荐采用“两步式”契约：

1. `POST /worlds/home/resident-drafts/preview`：校验结构化字段，返回规范化摘要、AI 身份声明、安全结果和短期 `draft_token`。
2. `POST /worlds/home/residents`：只接受 `draft_token + client_request_id`，在事务中创建 candidate 或 active resident。

若服务端选择单接口，也必须支持 `preview_only` 或等价机制，保证预览与最终持久化使用同一套规范化/安全逻辑。

安全要求：

- M1 不接受“这是某个具体真人/已故亲友”的身份声明。
- 未成年人形象、监护与恋爱混合等高风险组合 fail closed。
- 关系称谓不自动赋予真人身份、监护权或现实伴侣声明。
- 不把未经校验的客户端 `persona_hint` 直接写入 Soul。

### NAME-001：服务端建议实例姓名与可降级命名状态

优先级：M1 Required；正式联调前交付。它不是单点强失败依赖：旧服务端缺少字段或单个候选命名失败时，客户端可使用本地小姓名池兜底。

在 world bootstrap 的 candidate DTO 中新增：

- `suggested_display_name: string | null`：服务端为当前 world 中该 candidate 建议的实例展示名；
- `naming_version: string | null`：生成该建议名的规则/配置版本；
- `persona_key: string`：跨模板版本稳定的 persona 标识，供客户端映射兜底姓名池；
- `naming_status: "ready" | "unavailable"`：明确建议名是否可用；如需错误原因，只返回稳定、非敏感的 `naming_failure_code`，不得泄露内部 provider、prompt 或策略细节。

契约要求：

- 同一 world/candidate 的服务端建议名一旦生成即固定；重复 bootstrap、换设备和重装 App 都返回同一值，不随命名算法升级静默变化。
- `naming_status=ready` 时 `suggested_display_name` 必须是去除首尾空白后的非空字符串；`unavailable` 时允许为 `null`，客户端进入本地兜底。
- 命名子流程失败不得让 world bootstrap 整体失败；仍返回真实 candidate、稳定身份和 `naming_status=unavailable`。
- 对未包含这些新增字段的旧响应，客户端按“命名能力不可用”兼容处理，而不是把整个世界判为损坏。
- 客户端确认时始终提交最终选择的 `selections[].display_name`；服务端在确认事务中持久化，之后 resident、conversation 和 Feed author 统一返回该实例名。
- 已确认居民的 `name` 不因服务端命名规则或客户端姓名池升级而改变。
- 服务端必须校验最终 `display_name` 的长度、字符与安全规则；不得假设客户端提交值天然可信。

## 5. M1 联调前应收口的契约

### BOOT-002：home bootstrap 对所有用户幂等

现有端点基本具备该能力，需要补 OpenAPI 说明与回归用例：

- 新用户第一次调用创建 world 与 4 位候选。
- 新用户重复调用不重复创建 candidate。
- 老用户/legacy 用户返回 backfill 后的 world/residents，不要求客户端根据 `account` 自行猜分支。
- confirmed 用户调用不会退回 selecting。
- 候选目录未就绪返回稳定 `preset_catalog_not_ready`，客户端展示维护态而不是空世界。

### CAND-001：候选稳定身份与公开 preview

候选 DTO 必须公开稳定、跨模板版本不变的 `persona_key`，并继续提供 `template_id + template_version`，使客户端能可靠映射本地兜底姓名池、识别运营模板升级并观测契约不匹配。

若 M1 保留角色详情预览，候选 DTO 还需提供经内容审核的 `long_summary` 与可选 `sample_dialogue`；不返回 `persona_seed_json`、内部 resident id 或 runtime account id。

未知 `persona_key` 的客户端回退规则：先使用合法的 `suggested_display_name`；若建议名也不可用，则使用候选模板 `name`，不随机猜测姓名，不阻塞至少保留一位的主流程，并记录不含私人内容的契约不匹配事件。

### CONV-001：会话列表时间、排序与分页

当前后端按 `updated_at DESC, conversation_id DESC` 排序，但公开 DTO 没有时间。M1 会话项需要增加：

- `last_message_at`（无消息时为 `null`）；
- `updated_at` 或明确 `sort_time`；
- `can_send` 与稳定 `read_only_reason`；
- 明确 cursor 基于的排序键。

服务端顺序仍为权威；客户端不按本地时间重新排序。

### CONV-002：AI 会话未读语义

当前 `ConversationSummary.unread` 在 P1 恒为 0，但 PRD 需要未读状态。联合评审时必须二选一：

- M1 明确不展示 AI 未读，服务端返回 capability/文档说明；或
- 增加 per-user read cursor 与标记已读接口，公开 `unread_count`。

不得长期保留“字段存在但永远为 0”而让客户端误认为未读已实现。

### TURN-001：`no_reply` 与幂等重放响应定型

当前 turn 在 `no_reply=true` 时仍返回 `reply` 对象，其 `text` 可能为空；幂等命中时 `reply.message_id` 可能为 `null`。M1 需要冻结一种无歧义结构：

- 推荐 `no_reply=true` 时返回 `reply: null`；否则 `reply.text` 必须为非空字符串。
- 幂等重放尽量返回原持久化 `reply.message_id`；若暂时做不到，OpenAPI 明确其 nullable 语义。
- `deduplicated=true` 不改变原业务结果，客户端不得新建第二个 AI 气泡。
- 同一 conversation 内 `client_message_id` 唯一，跨 conversation 可复用；服务端继续使用 conversation 锚定内部幂等键。

### TIME-001：公开时间统一为带时区 ISO 8601

交接文档声明所有时间带 `+08:00`，但 AI message 当前直接透传存储层 `created_at` 字符串，未在序列化层统一补时区。M1 要求：

- 所有公开 `created_at/updated_at/last_message_at/expires_at` 使用带偏移 ISO 8601。
- `server_time` 继续由每个世界信封返回，客户端只用它校准显示，不作为授权来源。
- cursor 继续保持 opaque，客户端不解析其中时间。
- 在 OpenAPI 中明确字段是否 nullable，禁止同一字段混用 naive SQL 时间和 ISO 时间。

### ERROR-001：稳定错误信封

- 世界端点的领域错误、Pydantic 422、限流和未知异常使用一致的 `{code, request_id, server_time, data/message}` 形状。
- `not_found` 不同时表达 capability 关闭与资源不存在；客户端应在请求前通过 capability 判断。
- 为 M1 主链路冻结错误码：`preset_catalog_not_ready`、`resident_capacity_empty`、`resident_capacity_exceeded`、`resident_selection_invalid`、`conversation_read_only`、`turn_in_progress`、`rate_limited`。

### CONTRACT-001：交付 OpenAPI snapshot

- 从当前后端导出 `/api/v1` OpenAPI JSON，提交到双方约定位置。
- 客户端 CI 对 breaking changes 做检查；生成 DTO 不直接替代客户端领域模型。
- `app_api_handoff.md` 继续作为说明文档，但不替代机器可校验契约。

## 6. 服务端优先与本地姓名池兜底

服务端建议名是 M1 默认来源；本地姓名池只用于新增字段缺失、建议名为空或 `naming_status=unavailable`。姓名池每个 persona 仅维护 3–5 个经审核名字，不承担角色、世界或候选数据的本地构造。

姓名来源优先级固定为：

1. 已确认 resident DTO 的 `name`；
2. candidate DTO 的合法 `suggested_display_name`；
3. 按稳定算法选择的本地姓名池名字；
4. candidate 模板 `name`。

只有已经成功取得服务端真实 candidate 时才允许使用第 3、4 级。认证失败、bootstrap 网络失败、5xx、非法信封或候选集合整体缺失时，客户端展示可恢复错误，不得用姓名池或原型数据伪造本地居民。

### 6.1 稳定选择

需要兜底时，候选展示名由以下输入稳定计算：

```text
platform_user_id + template_id + template_version + name_pool_version
```

- 使用非安全用途的确定性 hash 选择姓名，不调用运行时随机数。
- 同一用户、同一候选、同一姓名池版本在刷新、重启和重新安装后得到同一结果。
- 按候选 rank 顺序处理同世界重名冲突，使用池内下一项；池内仍冲突时回退 candidate 模板 `name`。
- 用户确认时把结果写入 `selections[].display_name`；确认后只显示服务端 resident DTO 返回的 `name`。
- 已确认实例不随客户端姓名池升级而改名。

### 6.2 版本与运维约束

- 姓名池按稳定 `persona_key` 映射；兼容旧服务端时，可临时按已冻结的 `template_id + template_version` 映射。
- 运营新增或更换 persona/template 时，应同步服务端建议名配置；客户端姓名池未覆盖的新 persona 最终回退 candidate 模板 `name`。
- 埋点只记录 `persona_key/template_id/template_version/naming_version/name_pool_version/fallback_reason`，不记录姓名、手机号或完整 `platform_user_id`。

## 7. P2 及后续服务端积压

### FEED-201：主人删除/隐藏动态

优先级：P2 Blocker。

- 暴露主人删除自己动态、隐藏 AI 动态的正式 HTTP API。
- owner-scoped、幂等，并让主人 Feed 与有效访客 Feed 立即一致。
- 保留审计/举报证据的策略不能通过客户端猜测。

### MEDIA-201：世界动态媒体契约

优先级：P2+。

完成对象存储、上传凭证、MIME/大小校验、EXIF 清理、内容审核、受控 URL、删除联动和 visit 终态临时缓存清理后再开放。首个 Feed 版本保持纯文字。

### CONFIG-201：分平台最低版本与灰度

当前只有单个 `minimum_supported_version`。后续如需双端独立灰度，增加 iOS/Android 最低版本、推荐版本、维护态和分 capability 最低客户端版本，不通过中文文案控制程序分支。

## 8. 建议服务端实施顺序

1. `CAP-001` + `BOOT-001` + `COMPAT-001`：先让客户端能安全选择、恢复并兼容 M1 路径。
2. `NAME-001` + `BOOT-002` + `CAND-001`：冻结服务端建议名、可降级命名状态、onboarding 重放、候选身份与 preview。
3. `CUSTOM-001`：保住已确认的完整自建角色体验。
4. `CONV-001/002` + `TURN-001` + `TIME-001`：冻结会话列表、turn 和时间语义。
5. `ERROR-001` + `CONTRACT-001`：形成可持续联调与 CI 门禁。
6. P2 开工前完成 `FEED-201`，媒体继续后置。

## 9. M1 服务端验收最小集

- 新用户登录得到 `account=null` 后，杀 App/重启仍能凭 Session 恢复 selecting onboarding。
- 老用户不依据 `account!=null` 跳过 world bootstrap；legacy resident 正确恢复且无主角色标识。
- config 明确关闭、网络失败和客户端不兼容进入三种不同状态。
- bootstrap 重放只产生一个 world 和固定 4 位候选。
- 命名成功时，同一 world/candidate 的多次 bootstrap 始终返回同一 `suggested_display_name` 和 `naming_version`。
- 单个候选命名不可用、建议名为空或旧服务端没有命名字段时，bootstrap 仍返回真实候选，客户端稳定选出本地兜底姓名。
- auth/bootstrap 整体失败或候选集合缺失时，客户端只展示可恢复错误，不创建本地假居民。
- 无论最终姓名来自服务端建议还是本地兜底，经 `display_name` 确认后，resident/conversation/feed author 始终返回同一实例名。
- 自建角色的结构化设定经预览确认、安全校验和幂等创建后可跨设备恢复。
- 1–10 位容量、重复确认、断网重试和并发确认不产生重复 resident/runtime/conversation。
- 会话列表按服务端活动时间稳定排序，分页无重复/遗漏，只读状态不能发送。
- 同一 `client_message_id` 重试只执行一次 turn；`no_reply=true` 作为正常业务态处理。
- 所有公开时间带明确时区；客户端不解析 cursor 或以设备时间决定权限。
- 所有 M1 用户数据响应 `Cache-Control: no-store`，错误包含稳定 `code` 和 `request_id`。
