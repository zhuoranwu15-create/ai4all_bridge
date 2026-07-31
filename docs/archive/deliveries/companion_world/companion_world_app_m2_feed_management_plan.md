# Companion World App M2 主人 Feed 管理 服务端开发计划（P0 + P1）

更新时间：2026-07-31
状态：**已完成并归档。P0 + P1（M5-REPORT-001 / M5-CONV-001）已通过 PR #57 合入主干并上线。**
当前能力以 [`companion_world_app_prd.md`](../../../products/zhaoxi/capabilities/companion_world_app_prd.md)
和 [`app_api_handoff.md`](../../../products/zhaoxi/app_api_handoff.md) 为准；§8 中仍成立的事项已转入
[`companion_world_app_followups.md`](../../../backlog/products/zhaoxi/companion_world_app_followups.md)。

> 归属：`product:zhaoxi`。
> 输入：客户端仓库《Companion World 客户端后续服务端需求清单（M2–M5）V0.3》
> （原件冻结在 [`archive/alignments/`](../../alignments/companion_world_m2_m5_server_requirements_v0_3_client.md)）。
> 产品口径：[朝夕相伴 App 端 PRD](../../../products/zhaoxi/capabilities/companion_world_app_prd.md)。
> 技术权威：[Companion World 3.0 ADR](../../../architecture/products/zhaoxi/companion_world_3_0_refactor_design.md)。
> 前序计划：[Companion World App M1 服务端](companion_world_app_m1_server_plan.md)（S1–S6 已交付）。
> 评审基线：`main@84475fd`，当前最大迁移号 `m0051`。

---

## 1. 范围与结论（TL;DR）

本计划**只覆盖 P0**：客户端 `FEED-MGMT-001`、`FEED-MGMT-002`、`CONTRACT-M2-001`
——即 M2 发布闭环的两个 Blocker 及其配套契约。`M4-RELEASE-001` 是这两条的复述，随本批次
自动关闭。其余条目（M3/M4/M5 的联调数据、展示摘要、会话读模型、举报枚举、通知口径）见 §8。

**评审结论：两个 Blocker 成立，但底座早已存在，本质是「接线 + 修幂等 + 冻结契约」。**

| 客户端条目 | 结论 | 说明 |
| --- | --- | --- |
| FEED-MGMT-001 主人删除自己的动态 | ✅ 成立 | domain/repository/persistence 三层齐全，只差 HTTP 路由；但**重放会 500**，必须先修 |
| FEED-MGMT-002 主人隐藏 AI 动态 | ✅ 成立，方案收敛 | 不新增状态机、不新增迁移，复用既有终态写路径 |
| CONTRACT-M2-001 补响应 schema | ✅ 成立，低成本 | S5 已建好 `response_model` + snapshot 门禁，补模型即可 |
| farewell 是否可隐藏 | ⚠️ 服务端反提案 | 客户端问对了：**允许隐藏会让整个 Feed 变 409**，见 D-4 |

**服务端自查发现 2 个客户端清单未覆盖的问题**，均在本批次一并修：

- **IDEM-002**：删除重放返回 500（详见 §2.1），直接违反客户端「重复删除必须幂等，不产生 5xx」。
- **ERR-003**：`post_not_publishable` 会泄漏「post 存在但状态不对」，与防枚举口径冲突。

**不需要数据库迁移。**

---

## 2. 现状核查（证据）

### 2.1 删除链路已存在，但重放路径是 500（IDEM-002）

三层都在，只是 M3 刻意没暴露路由
（`domain/companion_world/feed.py:79` 注释：「保留未来内容治理下架接缝；M3 不暴露审核/管理路由」）：

| 层 | 位置 |
| --- | --- |
| domain | `app/products/zhaoxi/domain/companion_world/feed.py:79` `delete_post` |
| port | `app/products/zhaoxi/domain/companion_world/contracts.py:292` `FeedRepository.delete_post` |
| repository | `app/products/zhaoxi/infrastructure/repositories/companion_world.py:270` |
| persistence | `app/products/zhaoxi/infrastructure/persistence/companion_world.py:1934` `delete_feed_post_with_outbox` |

**缺陷**：`companion_world.py:1968-2001` 用**本次请求**的 `deleted_at` 重算 `payload_json`，
再与已存在的 outbox 行比对。重放时 outbox 行保留的是**首次**的 `deleted_at`，两者必然不等
→ `ValueError("outbox idempotency conflict")` → repository 无白名单原样透传
（`repositories/companion_world.py:286-287`）→ `_ERROR_STATUS` 无此 code → 默认 **500**
（`api/companion_world.py:171`）。

现有存储测试之所以没抓到，是因为它重放时传了**同一个** `deleted_at`
（`tests/test_companion_world_m3_storage.py:280-300`），而真实 HTTP 调用走 `beijing_now()`，
跨秒即触发。

### 2.2 隐藏 AI 动态：仓库已有先例，不需要新状态

`persistence/companion_world_lifecycle.py:551-556`：admin correction 隐藏 farewell 用的就是
`status='deleted' + terminal_reason='admin_correction'`。终态是**一个** status，用
`terminal_reason` 区分来源，已经是本仓的既定惯例。

### 2.3 访客可见性、游标、AI 补生成三件事是「免费」的

- **访客 Feed 复用主人查询**：`application/companion_world_visits.py:451` 直接调
  `list_published_feed_posts_for_owner`，只筛 `status='published'`
  （`persistence/companion_world.py:1924`）。终态一写，主人与所有有效访客同时不可见，**零额外改动**。
- **游标天然稳定**：cursor 是 keyset `(published_at, id)`（`api/companion_world.py:456`），
  删行不移位，不会重复/错页 —— 客户端这条要求零成本满足。
- **不会触发 AI 重新生成**：slot 唯一索引行仍在，`claim_ai_feed_slot` 只对
  `status='generating'` 重领（`persistence/companion_world.py:1662-1673`）。

### 2.4 契约门禁已就位

`scripts/export_openapi.py` 导出 → `tests/test_app_openapi_contract.py` 三条门禁
（snapshot 过期 / 退回裸 dict / 静默丢字段）。Feed 的 GET 与 POST **当前都没有
`response_model`**（`api/companion_world.py:732,763`），所以 snapshot 里是
`additionalProperties: true` —— 客户端点名的正是这个。

---

## 3. 设计决策

### D-1 持久化单一终态，API 层投影两个语义

```
universe_posts.status        = 'deleted'                     ← 唯一终态，沿用现状
universe_posts.terminal_reason ∈ {owner_deleted, owner_hidden, admin_correction}
API 投影：terminal_reason == 'owner_hidden' → status:"hidden"，其余 → status:"deleted"
```

**理由**：`terminal_reason` 是 TEXT 无枚举约束 → **零迁移**；不必给新 status 再排查一遍
所有 `status='published'` 过滤点；访客 Feed、游标、AI slot 三件事按 §2.3 自动正确；
与 §2.2 的既有 admin 先例一致。

代价是「隐藏」在库里叫 deleted。可接受：对外语义由 API 投影负责，审计靠 `terminal_reason`
+ outbox 事件，而客户端 M2 明确不要求「取消隐藏」。若将来产品要恢复能力，再按独立受审计
接口设计，届时如需拆 status 也是加性迁移。

### D-2 两条路由，内部单一写入口

采纳客户端提的路径形状：

```http
DELETE /api/v1/worlds/home/feed/posts/{post_id}          → 主人删除自己的文字动态
POST   /api/v1/worlds/home/feed/posts/{post_id}/hide     → 主人隐藏 AI 居民动态
```

权限主体不同（自己的内容 vs 别人的内容），分两条更利于审计与埋点，也让客户端 UI 分支
（只在自己动态显示「删除」、只在居民动态显示「隐藏」）与后端权限一一对应。

内部收敛为一个 `retire_post(mode)`，`mode ∈ {'delete','hide'}`。**不做** 合并成
`POST /posts/{id}/retire {mode}` 的单路由：那要客户端传语义参数，与「不接受客户端提供
隐藏原因/作者/世界 ID」的精神冲突。

`reason_code` 由服务端按 mode 硬编码，**请求体不接受任何字段**（hide 用 `extra="forbid"`
的空 payload，DELETE 无 body）。

### D-3 权限判定与错误码归一（含 ERR-003）

判定**在写事务内**做（`_m3_write_tx` 已持锁，`persistence/companion_world.py:1943`），
不做「先查后写」，避免并发窗口：

| 场景 | 结果 |
| --- | --- |
| delete：`author_type='human'` 且 `author_platform_user_id` = 当前主人 | ✅ |
| delete：目标是 `author_type='resident'` | ❌ `post_not_found` (404) |
| hide：`author_type='resident'` | ✅ |
| hide：目标是 `author_type='human'` | ❌ `post_not_found` (404) |
| 不存在 / 跨 owner / `status ∈ {generating, skipped}` | ❌ `post_not_found` (404) |
| `post_id` 格式非法 | ❌ `post_not_found` (404)，不用 422 |
| hide 目标 `post_type='farewell'` | ❌ `post_not_hideable` (409)，见 D-4 |

统一 404 而非 `post_not_owner`(403)：客户端已在需求里接受这个口径，安全上也不给资源枚举
信号。格式非法同样返回 404，不给攻击者「ID 格式对不对」的区分信号。

新增两个稳定错误码入 `_ERROR_STATUS`：`post_not_found`(404)、`post_not_hideable`(409)。
`post_not_publishable` 不再对外出现（ERR-003）。

### D-4 farewell 动态 M2 不允许隐藏 —— 服务端反提案

客户端把这条挂起等服务端冻结，问对了。**允许隐藏会自伤**：

`list_published_feed_posts_for_owner` 的 world readiness 判定是
`onboarding_state='confirmed' AND (has_active_resident OR has_published_farewell)`
（`persistence/companion_world.py:1876-1904`）。若最后一位居民已离开、farewell 是唯一
readiness 信号，主人一旦隐藏它，**整个 Feed 立刻变 `world_not_ready`(409)** —— 主人和访客
都读不到任何东西，一个隐藏动作打掉整个页面。

**M2 决策：拒绝隐藏 `post_type='farewell'`，返回 `post_not_hideable`(409)。**
客户端据此不在离别动态上显示「隐藏」入口。

若产品坚持要允许（见 Q1），前置条件是先把 readiness 判定与 farewell 的 published 状态
解耦（改看 `resident_lifecycle_events` 是否有 committed 记录），那是独立改动，不进本批次。

**顺带记录一笔债（不在本批次修）**：admin correction 路径
（`persistence/companion_world_lifecycle.py:525-556`）今天就能触发同一个自伤。运营隐藏
最后一条 farewell 会让该世界 Feed 整体 409。已知，待 readiness 解耦时一并处理。

### D-5 幂等语义：首次写入者胜出

- `replayed` 的判据是 **post 行**，不是 outbox：UPDATE 前 `status` 已是 `deleted` → `replayed=true`。
- 重放时 payload 一致性比对改用**已落库的** `deleted_at`/`terminal_reason` 重算，而不是本次
  请求时间（修 IDEM-002）。
- 重放请求携带的 `reason_code` 被忽略，返回**首次**的终态。即「先删后隐藏」这种理论上
  不可能发生的组合（author_type 互斥）也不会 409，而是回放原结果。
- 存储层测试 `tests/test_companion_world_m3_storage.py:280-300` 的断言随之更新：重放用
  **不同** 时间戳必须成功且 `changed=False`；不再断言「换 reason 就 conflict」。

响应形状按客户端建议冻结：

```json
{
  "code": "ok",
  "request_id": "req_...",
  "server_time": "2026-07-28T12:00:00+08:00",
  "data": {"post_id": "post_...", "status": "deleted", "replayed": false}
}
```

`Cache-Control: no-store`（复用 `_no_store`）。首次操作与重放**都返回 200**：本操作没有
「创建资源」语义，不套用发帖的 201/200 区分，`replayed` 已足够客户端分辨。

### D-6 门控沿用 `world_feed`，不新增开关

两条路由挂现有 `_require_feed_session`（`api/companion_world.py:322`，即
`companion_world_p1_enabled AND companion_world_feed_enabled`）。
`/app/config` 的 capability 不新增字段 —— 客户端在需求里也明确「不新增内部 flag 暴露」。
关闭时统一 404 `feature_disabled`，与既有口径一致。

---

## 4. 改动清单（diff 级）

无迁移。共 8 个源文件 + 2 个测试文件 + 2 份文档。

| # | 文件 | 改动 |
| --- | --- | --- |
| 1 | `infrastructure/persistence/companion_world.py:1934` | `delete_feed_post_with_outbox` → `retire_feed_post_with_outbox`：新增 `expected_author_type`、`forbid_post_types` 参数并**在事务内**执行断言；返回 `(row, changed)`；按 D-5 修重放 payload 比对；`__all__`(:87) 同步 |
| 2 | `domain/companion_world/contracts.py:292` | `FeedRepository.delete_post` → `retire_post`，签名带 `mode`，返回 `Tuple[UniversePostRecord, bool]` |
| 3 | `domain/companion_world/feed.py:79` | `delete_post` → `retire_post(platform_user_id, *, post_id, mode, retired_at)`：校验 `post_id`/`mode`，把 mode 映射为 `(reason_code, expected_author_type, forbid_post_types)`。**领域层是 reason_code 的唯一来源** |
| 4 | `infrastructure/repositories/companion_world.py:270` | 跟随改名；ValueError **白名单化**为 `{post_not_found, post_not_hideable}`，其余原样抛（修 IDEM-002 的放大面） |
| 5 | `api/companion_world.py:76` | `_ERROR_STATUS` 新增 `post_not_found`(404)、`post_not_hideable`(409) |
| 6 | `api/companion_world.py:763` 后 | 新增两条路由 + `_feed_retire_data(post, replayed)`（按 D-1 投影 status）；给 Feed 的 GET/POST 与两条新路由补 `response_model` + `WORLD_ERROR_RESPONSES` |
| 7 | `api/contracts.py:215` 区块后 | 新增 `FeedAuthor`/`FeedContent`/`FeedItem`/`FeedListData`/`FeedPostData`/`FeedRetireData` + 4 个 `WorldEnvelope[...]` 别名；`__all__` 同步。字段按 `_feed_item_data`(:439) 现有形状**照抄，不顺手改契约** |
| 8 | `api/app.py:88` | `CLIENT_CONTRACT_VERSION` → `"2026-07-28"` |
| 9 | `docs/products/zhaoxi/openapi/app_v1.json` | 重新导出（`.venv/bin/python scripts/export_openapi.py`） |
| 10 | `tests/test_app_openapi_contract.py:20` | `MAIN_CHAIN_OPERATIONS` 增 4 项（feed GET/POST、delete、hide） |
| 11 | `tests/test_companion_world_feed_api.py` | 新增 §5 用例 |
| 12 | `tests/test_companion_world_m3_storage.py:280-300` | 按 D-5 更新重放断言 |
| 13 | `docs/products/zhaoxi/app_api_handoff.md:516,590` | 补两条路由、错误码与 `replayed` 语义（该文件当前有未提交改动，需先确认基线） |

**注意 `app_api_handoff.md` 工作区当前是 modified 状态**，动手前先确认那份改动的去留，
避免和本批次的文档改动互相覆盖。

---

## 5. 测试方案

按 AGENTS.md：本批次触及请求路由与跨模块契约 → **跑全量，且 SQLite + PostgreSQL 双档**
（`AI4ALL_TEST_DB=postgres`）。PG 档是唯一阻塞门禁。

`tests/test_companion_world_feed_api.py` 新增（客户端 CONTRACT-M2-001 逐条对应）：

| 用例 | 断言 |
| --- | --- |
| 主人删除自己的文字动态 | 200；`status="deleted"`、`replayed=false`；Feed 立即不含该条 |
| **删除重放（跨秒）** | 200 且 `replayed=true`，**不是 5xx**（IDEM-002 回归） |
| 拒绝删除 AI 动态 | 404 `post_not_found` |
| 主人隐藏 AI 居民动态 | 200；`status="hidden"`；Feed 立即不含该条 |
| 隐藏重放 | 200 且 `replayed=true` |
| 拒绝隐藏自己的动态 | 404 `post_not_found` |
| 拒绝隐藏 farewell | 409 `post_not_hideable`（D-4） |
| 跨 owner 删除/隐藏 | 404 `post_not_found`，且目标世界 Feed 不受影响 |
| 不存在 / 格式非法 post_id | 404 `post_not_found` |
| 注入字段（body 带 `world_id`/`reason`/`owner_id`） | 422 错误信封 |
| **有效访客立即不可见** | 复用 M4 active visit：主人删/隐后，`GET /visits/{id}/feed` 立即不含该条 |
| 游标边界 | 翻页途中删除中间一条，续用旧 cursor 不重复、不错页、不 500 |
| flag 关闭 | 两条路由均 404 `feature_disabled` |
| `Cache-Control` | 两条路由响应均为 `no-store` |
| **隐藏不影响居民生命周期** | 隐藏 AI 动态后 resident status/conversation/记忆均不变（客户端明确要求） |

`tests/test_app_openapi_contract.py` 自动覆盖：snapshot 一致、4 条端点有真实 schema、
响应键集与模型逐层一致。

文档改动后跑 `tests/test_documentation_links.py`。

---

## 6. 上线与回滚

- `main` 受保护：走 PR，**必须等 PG 档全量 CI 全绿**才能合入。
- 无迁移 → 回滚即回滚代码，无数据形态变更。
- 已被 retire 的行不会因回滚复活（`status='deleted'` 是既有终态，旧代码同样只读
  `published`），所以回滚是安全的单向操作。
- 上线顺序：服务端合入并重启 → 客户端按其 §4「客户端接入门」四步开放入口。
  两条路由受 `world_feed` 门控，客户端未接入前不会有调用方。

| 风险 | 缓解 |
| --- | --- |
| 隐藏 farewell 打掉整个 Feed | D-4 直接在服务端拒绝；测试覆盖 |
| 重放 500 未被抓到 | 专门加「跨秒重放」用例，不复用同一时间戳 |
| `response_model` 漏字段导致静默丢数据 | S5 已有的键集比对门禁；模型照抄现有 `_feed_item_data` |
| 访客缓存看到已删内容 | 服务端每次请求实时判定 + `no-store`；客户端侧按其降级约定清理内存缓存 |
| 改名波及未知调用方 | 已 grep 确认 `delete_feed_post_with_outbox` 仅 2 处调用（repository + 存储测试） |

工作量估计 **2–3 人日**（含双档测试与 snapshot 重导出）。

---

## 7. 待拍板事项

| 编号 | 问题 | 服务端建议 |
| --- | --- | --- |
| **Q1** | farewell/departure 动态是否允许主人隐藏？ | **建议 M2 不允许**（D-4）。允许则需先解耦 world readiness，属独立批次 |
| **Q2** | 已隐藏 AI 动态是否需要保留「运营可见」的后台视图？ | 建议本批次不做。数据未删，`terminal_reason` 已可检索，等有运营诉求再开 admin 路由 |
| **Q3** | 未来是否提供「取消隐藏」？ | 建议明确后置。客户端 M2 已声明不要求，届时按独立受审计接口设计 |

Q1 未拍板不阻塞开工：先按「拒绝」实现，改判只是放开一个断言 + 一条测试。

---

## 8. 明确不在本批次（P1/P2 与运营项）

均已评估为**成立**，只是不进 P0；排期见下表。

| 优先级 | 条目 | 服务端结论摘要 |
| --- | --- | --- |
| ~~P1~~ 已交付 | **M5-REPORT-001** 举报原因契约 | 成立且最便宜：枚举早已硬编码在 `application/companion_world_human_chat.py:31`，只是没暴露。独立 `GET /human-conversations/report-options` 返回版本化 `{reason_code, label, details_required}`，客户端按 version 缓存；不放 `/app/config`（文案不常变，不值得所有客户端每次拉）。**实现见 §10** |
| ~~P1~~ 已交付 | **M5-CONV-001** 会话列表读模型 | 成立，数据齐备。但 `human_conversations` 只有 `owner/visitor_last_read_at` 时间戳（`app/db/_core.py:2873-2874`）—— 同秒并发会漂。加性迁移补 `owner/visitor_last_read_sequence`，unread 用 `sequence_no` 比较；`can_send`/`read_only_reason` 复用 AI 会话 DTO 现成口径（`api/companion_world.py:397-398`），`expires_at` 从 `universe_visits` join。**本行原文有两处判断错误，已在 §10 更正** |
| P2 | **M4-CONTRACT-001 / M5-CONTRACT-001** | 同属 `response_model` 契约债，与 P0 同机制，按批次补 |
| P2 | **M4-PRESENTATION-001** 公开展示摘要 | 需产品先定「什么算 owner 审核过的公开投影」。实现上不得顺手 join profile |
| 决策 | **M5-NOTIFY-001** 真人会话通知 | 建议选客户端给的**方案 1（明确后置）**：M5 后端规格已排除 Push，做会话级静音 DTO 却无投递通道 = 假契约，比不做更糟 |
| 运营 | **M3-QA-001 / M4-QA-001 / M5-QA-001** | 不需要新 API。信箱链路：`POST /admin/companion-world/mailbox/catalog` 导入一封审核过的信 → 跑一次 `scripts/run_world_lifecycle_scheduler.py` 的 mailbox maintenance（`application/companion_world_mailbox.py:136`，受 cooldown / active_limit≤8 / TTL 约束）。给客户端投递时间 + 测试账号 + 预期状态即可。**接受后无法回滚是真实的** —— 必须用一次性测试账号，不要在个人生产账号上跑 |

---

## 9. 实施记录（2026-07-28）

代码与文档已按 §4 落地在 `feat/companion-world-m2-feed-management`。与计划的两处偏差：

1. **多改了一处**：`UniversePostRecord` 增加 `terminal_reason` 字段（`domain/companion_world/contracts.py:213`
   区块）并在 `_feed_post` 回填。API 层要靠它把终态投影成 `deleted`/`hidden`，计划漏列。
   它是领域可见的终态语义，不是存储细节，放领域 DTO 里符合该 dataclass 的既有边界。
2. **注入字段的错误码分两种**（计划只写了 422）：`_validation_error_handler`
   （`api/companion_world.py:1091-1096`）对 `account_id`/`runtime_account_id`/`universe_id`
   走既有的 400 `account_id_not_accepted`，其余多余字段才是 422 `invalid_request`。
   测试与交接文档均按这个既有口径写，未改动该 handler。

**以下是合入前的中间执行记录，不是归档时的最终状态。** 当时曾记录“测试一次都没跑完整”，
随后已得到本节末尾所列的完整结果。执行顺序为：

```bash
.venv/bin/pytest tests/test_companion_world_feed_api.py tests/test_companion_world_m3_storage.py \
  tests/test_app_openapi_contract.py tests/test_documentation_links.py -q   # 聚焦
.venv/bin/pytest tests/ -q                                                  # SQLite 全量
AI4ALL_TEST_DB=postgres .venv/bin/pytest tests/ -q                          # PG 档（唯一阻塞门禁）
```

测试结果：聚焦档 45 passed；SQLite 全量 1716 passed / 2 failed；PG 档 **1748 passed、9 skipped、
0 failed**。SQLite 那 2 条（`test_m0036_repairs_collided_schema_and_is_idempotent`、
`test_m0038_backfills_existing_sessions_without_changing_count`）是本机 sqlite 3.26.0 不支持
`ALTER TABLE ... DROP COLUMN` 的既有基线失败，与本批改动无关；唯一阻塞门禁的 PG 档全绿。

---

## 10. P1 实施记录（2026-07-28，M5-REPORT-001 + M5-CONV-001）

### 10.1 对 §8 两条结论的更正

动手前复查代码，发现 §8 里两处判断有误，实现按更正后的事实走：

1. **`POST /human-conversations/{id}/read` 不收 `last_message_id`**（收 `last_message_id` 的是
   AI 会话那条路由）。真人会话的 `/read` 是空 body，服务端按「当下」写 `last_read_at`。因此
   同秒漂移的方向是**漏计**而不是多计：与标记已读同一秒到达的对方消息会被判成已读，永久不
   进未读数。这比「多算」严重，因为消息被静默吞掉。
2. **不需要发明设计**：`_migration_0050_ai_conversation_read_cursor` 已经给 AI 会话定了
   sequence 游标的形状，m0052 照抄即可，不是新方案。

另外推翻了 §8 自己提的「unread 封顶 99+」：`COUNT(*)` 的代价与是否封顶无关，封顶只是丢掉信息，
所以 `unread_count` 返回精确值，要不要显示成 99+ 由客户端决定。

### 10.2 落地内容

| 层 | 文件 | 改动 |
| --- | --- | --- |
| 领域 | `domain/companion_world/human_chat.py` | 新增 `HumanReportReason` / `HUMAN_REPORT_REASONS`(7 条) / `HUMAN_REPORT_REASONS_VERSION=1` / `human_report_reason()`，成为举报码的唯一事实源 |
| 应用 | `application/companion_world_human_chat.py` | 删除原硬编码 `_REPORT_REASONS` 集合，改用领域表；新增 `report_options()`；`report()` 强制 `details_required` 的码必须带正文（否则 422 `invalid_request`）；新增 `_read_only_reason()` / `_preview_text()`；会话 DTO 补 5 个字段 |
| 迁移 | `app/db/_core.py` | `_migration_0052_human_conversation_read_cursor`：纯加列 `owner/visitor_last_read_sequence`，无回填（既有会话为 NULL ≡「一条都没读过」） |
| 持久化 | `infrastructure/persistence/companion_world_human_chat.py` | 列表查询用相关子查询一次算出 `last_preview`/`unread_count`，并 join `universe_visits` 取 `visit_status`/`expires_at`，避免 N+1；预览在 SQL 层 `SUBSTR(...,1,120)` 截断；`mark_human_conversation_read` 在同一事务内快照 `MAX(sequence_no)` 并单调写入 |
| API | `api/companion_world_human_chat.py`、`api/contracts.py` | 新增 `GET /human-conversations/report-options`（路由必须排在 `/{conversation_id}/messages` **之前**，否则被路径参数吞掉）；两条 GET 补 `response_model` 与错误信封 |

**`read_only_reason` 的取值顺序是有意的**：终态（`counterpart_blocked` → `visit_ended` →
`conversation_ended`）一律排在可恢复的 `feature_disabled` 之前。反过来的话，一个早已结束的
visit 会在开关关闭期间显示成「功能未开放」，客户端会引导用户去等一个永远等不到的恢复。

**客户端零改动即可兼容**：`POST /read` 请求体不变，新字段全是 item 上的增量。

### 10.3 测试

新增 7 条测试（`tests/test_companion_world_m5_human_chat.py`）覆盖：版本化选项与实际受控码一致、
`details_required` 强制、发送开关关闭时仍可拉取选项、列表未读/预览/已读标记、
游标而非时间戳（同秒回归）、发送门控与过期投影、参与者隔离与预览长度上界。
`tests/test_companion_world_schema.py` 的 head 断言 51→52 并新增 m0052 幂等测试；
`tests/test_app_openapi_contract.py` 的主链路清单加入两条新端点。

**顺带修了 3 条与本批功能无关的假红**：`test_companion_world_m3_storage.py`、
`test_companion_world_m5_storage.py`、`test_multi_product_isolation.py` 各有一条
「重放历史迁移后 head 不得推进」的断言把版本号写死成 `51`，被 m0052 撞红。改为
`_MIGRATIONS[-1][0]`（沿用 `test_account_app_id_repair_m0036.py:15` 的既有写法），
语义不变且不再随迁移号漂移。`test_companion_world_schema.py` 那条 `== 52` 保持写死——
它是有意的 head pin，加迁移时本来就应当同步。

### 10.4 双档结果

| 档 | 结果 |
| --- | --- |
| SQLite 全量 | 1726 passed / 39 skipped / **2 failed**（`m0036`、`m0038` 两条已知基线，本机 sqlite 3.26.0 不支持 `ALTER TABLE ... DROP COLUMN`） |
| PG 全量（唯一阻塞门禁） | **1758 passed / 9 skipped / 0 failed** |
