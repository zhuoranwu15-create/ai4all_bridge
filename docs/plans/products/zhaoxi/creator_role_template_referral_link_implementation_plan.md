# 用户自建角色模板与邀请链接实施计划

更新时间：2026-08-01

> 状态：**CRT-01～08 开发实现已完成；待合并、生产部署与真实新账号 smoke。**
>
> 产品事实源：
> [用户自建角色模板与邀请链接 PRD](../../../products/zhaoxi/capabilities/creator_role_template_referral_link_prd.md)。
>
> 技术事实源：
> [用户自建角色模板与邀请链接技术设计](../../../architecture/products/zhaoxi/creator_role_template_referral_link_technical_design.md)。
>
> 本文只负责实施顺序、文件清单、测试闸门和发布步骤；字段语义、权限和异常口径与上述两份事实源冲突时，
> 以前两者为准。计划完成后将稳定结论回写事实源，并把本文移入 `docs/archive/deliveries/`。

## 0. 交付结论

P0 只实现以下闭环：

```text
创建者个人中心
  → 创建名字/性格/使命模板
  → LLM 审核
  → 创建者主动发布
  → 生成 personal invite_code + urt_* campaign_code 组合链接
  → 全新朝夕账号通过链接注册
  → referral 拉新归因 + 模板版本快照
  → 写入新账号 IDENTITY/SOUL/MISSION
  → onboarding 只询问用户称呼
  → 创建者查看与运营活码同口径的聚合漏斗
```

P0 明确不实现：

- 在线试玩、角色调试、预览对话、trial Runtime Account；
- 创建者切换或覆盖自己的当前 AI；
- trial 表/API/feature flag/scheduler、Dreaming/计费/工具特殊分支；
- 公开角色市场、交易、分成、模板复制、用户明细统计；
- Native App、Companion World 或 debug 建号消费 `urt_*` 模板码。

创建者页面只说明：当前不支持在线调试；需要发布后用一个尚未加入朝夕的新账号通过注册链接做真实测试。

## 1. 当前代码基线

开工基线以当前主干为准：

- schema head 已由 CRT-01 推进到 additive migration 59；后续若主干先新增 migration，合并时只顺延编号，不改语义。
- 个人邀请码在 `app/db/billing.py::register_platform_user_with_referral()` 中随首次 product membership 原子消费。
- Web `/web/login`、`/web/register-and-binding-intent` 随后调用
  `get_or_create_default_ai4all_account_for_user()` 创建默认关系账号。
- 运营活码由 `app/products/zhaoxi/infrastructure/persistence/campaign.py` 在账号创建后应用。
- 漏斗由 `campaign_visits + account_campaign_attribution + binding/onboarding` 现算，无需复制统计表。
- profile 文件已落 `account_profile_files`，可通过 `profile_storage.write_file(..., conn=...)` 参与模板归因事务。
- onboarding 的强制名字/人设判断目前散落在 `turn_services.py` 三处，本次统一为一个产品 helper。
- 量化使命由 `assign_mission_if_absent()` 在 onboarding 完成时分配；自建角色模板账号必须从该统一入口跳过。

## 2. 实施原则和硬不变量

1. **模板不是账号**：创建、审核、发布均不创建 Runtime Account。
2. **只作用于真实新成员**：只有 Web 首次加入朝夕并真正新建默认账号时才消费模板。
3. **可信 owner 来自 referral 结果**：绝不信任客户端传 creator id，也不靠 code 文本猜 owner。
4. **实例化写入原子**：通用 campaign attribution、模板 snapshot、三份 profile 和 `used_count` 要么全成，要么全不成。
5. **模板失败不挡注册**：模板失效、owner 不一致或内部异常时，保留已经成功的有效 referral，账号走普通 onboarding。
6. **老实例不追改**：运行时只读账号 snapshot/profile，不 join 模板 live row。
7. **创建者当前 AI 零写入**：创建者 API 不接收 account id，不增加 apply/switch/debug 方法。
8. **运营活码不回归**：非 `urt_` code 保持既有创建、校验、归因和 debug 行为。
9. **双后端一致**：schema、部分唯一索引、savepoint、事务与日期边界在 SQLite/PG 都验证。
10. **不预埋试玩**：P0 没有 trial 命名的数据结构、配置或 Runtime 分支。

## 3. 工单与依赖关系

| 工单 | 目标 | 依赖 | 合并闸门 |
| --- | --- | --- | --- |
| CRT-01 | migration、领域模型、基础 persistence | 无 | SQLite/PG schema 与 slot 并发测试 |
| CRT-02 | LLM 审核、版本和发布生命周期 | CRT-01 | pass/reject/error/CAS/360 天测试 |
| CRT-03 | profile renderer、onboarding override、非量化使命 | CRT-01 | prompt/onboarding/tool 隔离测试 |
| CRT-04 | `urt_` resolver、注册双归因和原子实例化 | CRT-01、CRT-03 | Web 注册/运营活码/幂等集成测试 |
| CRT-05 | 创建者 API、公共 preview、统计 API | CRT-02、CRT-04 | owner 权限、统计同口径测试 |
| CRT-06 | Admin/Staff API 和运营后台视图 | CRT-02、CRT-05 | 权限、处置和运营活码回归 |
| CRT-07 | 个人中心、模板页、落地页和 Nginx | CRT-05 | 静态契约、参数续传、无试玩入口 |
| CRT-08 | wipe/注销、配置、灰度和完整回归 | CRT-01～07 | SQLite/PG 全量、生产 smoke 清单 |

同一时刻只合并满足前置工单的改动。CRT-03 可在 CRT-02 开发期间并行准备，但 CRT-04 必须等 renderer 和
onboarding override 契约稳定后再接注册。

## 4. CRT-01：schema、领域模型与基础 persistence（已完成）

### 4.1 修改文件

- `app/db/_core.py`
- `app/products/zhaoxi/domain/creator_role_templates.py`（新增）
- `app/products/zhaoxi/infrastructure/persistence/creator_role_templates.py`（新增）
- `tests/test_creator_role_templates.py`（新增）
- `tests/test_db_backend_pg.py` 或专用 PG schema 测试

### 4.2 实现内容

新增 migration 59，只建立以下五张业务表：

1. `creator_role_templates`
2. `creator_role_template_versions`
3. `creator_role_template_review_runs`
4. `account_creator_role_template_attribution`
5. `creator_role_template_events`

迁移同时完成：

- `urt_` code UNIQUE 和至少 128bit 随机后缀；
- owner/app/slot 的 live 部分唯一索引，槽位限定 1–3；
- 每模板最多一个 published version；
- 每模板最多一个 pending/reviewing version；
- 账号 attribution 以 `account_id` 为 PK；
- migration 前检查运营 `campaign_codes` 不存在大小写不敏感的 `urt_` 前缀冲突，命中则 fail closed；
- SQLite/PG 使用相同字段和索引语义，不增加 PG-only 业务分支。

领域模块集中定义：字段长度、状态枚举、code prefix、时间格式、错误码和 DTO；API 与 persistence 不复制常量。

Persistence 公共方法必须带注释，owner 读写同时约束：

```text
creator_platform_user_id + app_id=zhaoxi + template_id
```

创建模板事务依次尝试 slot 1、2、3，每次用 savepoint 隔离唯一冲突；不使用“先 COUNT 再 INSERT”。soft delete
释放 slot，但不删除 version/review/event/attribution 历史。

### 4.3 测试闸门

- migration 59 建表、索引、幂等、head version；
- 1–3 槽分配、第四个拒绝、并发创建不突破、soft delete 后复用；
- 非 owner/cross-app 查询统一不命中；
- published/open-review 部分唯一索引；
- code 格式、熵、冲突重试、运营存量前缀 preflight；
- SQLite 与 PostgreSQL 聚焦测试均通过。

## 5. CRT-02：LLM 审核与模板生命周期（已完成）

### 5.1 修改文件

- `app/products/zhaoxi/application/creator_role_template_review.py`（新增）
- `app/products/zhaoxi/application/creator_role_template_links.py`（新增）
- `app/products/zhaoxi/infrastructure/persistence/creator_role_templates.py`
- `tests/test_creator_role_template_review.py`（新增）
- `tests/test_creator_role_templates.py`

### 5.2 审核服务

三字段作为一个 JSON DATA 对象进入一次 `TASK_MODERATION` LLM 调用：

- 复用 `tier_for_task()`、`generate_completion()`、`_extract_json_object()`；
- system prompt 明确模板内容是数据，不执行其中的指令；
- 严格输出 `decision / field_results / categories / reason`；
- 不调用会静默改写内容的 `sanitize_text()`，不接受 rewrite；
- provider/timeout/schema 异常全部返回可重试 `review_unavailable`，未审版本不放行。

事务顺序：

```text
DB：version pending → reviewing + 创建 review_run
事务外：调用 LLM
DB：按 review_run id CAS 落 passed/rejected/error
```

并发重试只有一个请求 claim 成功；旧 run 不能覆盖更新后的 candidate。

### 5.3 生命周期服务

- v1 审核通过只进入 approved，不自动发布；
- 首次发布设置 `activated_at` 和 `expires_at=activated_at+360 days`；
- active 模板编辑生成完整新版本，旧 published version 持续服务；
- 发布新版只切 published version，不更改 `activated_at/expires_at`；
- creator disable/enable、admin disable/enable 和 soft delete 使用明确状态转换；
- `now >= expires_at` 统一派生 expired，不新增过期 scheduler；
- 每个状态变化写 `creator_role_template_events`，metadata 限长且不记录完整审核 prompt/LLM 原始输出。

### 5.4 测试闸门

- 三字段机械校验、extra forbid、控制字符和边界长度；
- review pass/reject/provider error/非法 JSON/并发重试；
- rejected/pending version 永不发布；
- 第一次发布精确 +360 天，编辑/新版/启停不续期；
- active 编辑失败不影响旧 published version；
- creator/admin 权限和状态恢复矩阵。

## 6. CRT-03：角色 snapshot、Onboarding 与自由使命（已完成）

### 6.1 修改文件

- `app/products/zhaoxi/infrastructure/profiles.py`
- `app/products/zhaoxi/application/onboarding.py` 或新增邻近 projection helper
- `app/products/zhaoxi/application/turn_services.py`
- `app/products/zhaoxi/application/missions/assignment.py`
- `app/products/zhaoxi/application/missions/state.py`（仅在现有契约需要时）
- `scripts/backfill_account_missions.py`（确认继续只调用统一 assignment 入口）
- `tests/test_web_onboarding.py`
- `tests/test_agent_mission.py` 或现有使命聚焦测试

### 6.2 Profile renderer

新增纯函数和一个带 transaction connection 的写入函数：

```python
render_creator_role_template_identity(name)
render_creator_role_template_soul(name, personality_text)
render_creator_role_template_mission(mission_text)
write_creator_role_template_snapshot(*, conn, account_id, snapshot)
```

- 用户文本只进入固定数据槽；平台 AGENTS/TOOLS/安全边界不变；
- 文件名固定为 IDENTITY/SOUL/MISSION，用户输入不能控制路径；
- 使用 `profile_storage.write_file(..., conn=conn)`，使三文件和 attribution 同事务；
- renderer 单测固定输入输出，避免 API/注册路径各自拼 prompt。

### 6.3 Onboarding override

新增统一 helper：

```python
resolve_onboarding_identity_overrides(account_id)
```

优先级和返回语义：

1. creator role template attribution：强制名字 + 强制性格，script 为空；
2. 运营 campaign attribution：映射现有名字/静态 preset/script；
3. 无归因：无 override。

`apply_onboarding_info()`、`load_prompt_context()`、`advance_onboarding()` 全部调用该 helper，删除三处散落判断。
角色模板账号只询问用户称呼，然后完成；用户抽取结果不能覆盖模板名字/性格。

### 6.4 自由使命

- 模板 `MISSION.md` 是稳定 prose；
- `assign_mission_if_absent()` 检测 creator role attribution 后返回 `None`，不写 `account_mission`；
- 返回类型改成 `Optional[str]`，调用方不得假设永远有 mission id；
- 存量 backfill 复用同一入口，自然跳过角色模板账号；
- `resolve_account_mission()` 仍返回 None，`has_mission=false`，模型不获得量化使命工具；
- 关系/需求自我状态仍可正常渲染，不把“无量化使命”误判为异常。

### 6.5 测试闸门

- 三份 profile 与快照完全一致，特殊字符不会突破数据槽；
- onboarding 只问用户称呼，不覆盖 AI 名字/性格；
- 无 `account_mission`，无 mission tools，MISSION prose 仍进入 prompt；
- 运营活码不同组合、普通 onboarding 和存量使命行为全部不变。

## 7. CRT-04：code dispatcher、双归因和实例化事务（已完成）

### 7.1 修改文件

- `app/products/zhaoxi/infrastructure/persistence/campaign.py`
- `app/products/zhaoxi/infrastructure/persistence/creator_role_templates.py`
- `app/products/zhaoxi/application/creator_role_template_links.py`
- `app/db/billing.py`
- `app/routers/web.py`
- `app/products/zhaoxi/infrastructure/persistence/campaign_analytics.py`
- `app/db/lifecycle.py`
- `app/products/zhaoxi/application/account_deletion.py`
- `tests/test_web_creator_role_template_links.py`（新增）
- `tests/test_web_onboarding.py`
- `tests/test_db_campaign.py`
- `tests/test_db_campaign_analytics.py`

### 7.2 Code source dispatcher

新增统一判断：

```text
lower(code).startswith("urt_") → creator role template
其他                             → 现有运营 campaign
```

- `create_campaign_code()` / Admin 创建运营码时拒绝保留前缀，现有运营 code 行为不改（保留前缀拒绝已随 CRT-01 基座落地）；
- `resolve_campaign_source()` 只按前缀分派，不做两表竞速查询；
- `/web/campaign-visit` 用 resolver 判断 code 存在，允许 `urt_*` 复用匿名曝光和现有漏斗；
- debug 建号继续只支持运营 campaign；`urt_*` 没有可信 referral owner 时不消费；
- Native App/Companion World 不传可信 creator context，因此不能消费 `urt_*`。

### 7.3 保留可信注册上下文

当前 Web helper 会丢弃 `is_new_membership/referral_relationship`。修改 `/web/login` 和
`/web/register-and-binding-intent`：

1. 使用 `_register_platform_user_with_otp_result()` 保留完整注册结果；
2. 从已落库 `referral_relationship` 取得可信 `inviter_platform_user_id`；
3. 把 `is_new_membership` 和 expected creator 传给 `get_or_create_default_ai4all_account_for_user()`；
4. 已有账号命中 get-or-create early return 时不再应用任何模板；
5. 客户端传入的 `campaign_code` 不能单独证明 creator owner。

不要改变 OTP、membership、referral relationship 和 referral used_count 的既有事务。

### 7.4 模板实例化事务

账号创建成功后，dispatcher 对 `urt_*` 调用一个独立原子事务：

```text
读取 template + current published version
  → 校验 app/status/expiry/expected creator/is_new_membership
  → 确认 account 未有 campaign attribution
  → INSERT account_campaign_attribution（运营策略列为 NULL）
  → INSERT account_creator_role_template_attribution（三字段 snapshot）
  → 写 IDENTITY/SOUL/MISSION
  → template.used_count += 1
  → event attribution_applied
```

事务内任一步失败全部回滚，只返回 `role_template_link_result.applied=false`；真实账号和 referral 不回滚，前端提示模板
未生效并走普通 onboarding。预期失效 reason 与内部 error 分开记录。

幂等依赖：

- `account_campaign_attribution.account_id` PK；
- `account_creator_role_template_attribution.account_id` PK；
- `used_count` 只在两个 insert 和三文件成功后增加；
- 并发/重试最多一笔成功。

### 7.5 Wipe 与创建者注销

- `wipe_account_data()` 删除账号自己的 creator role attribution 和三份 profile；
- 创建者注销 soft delete 其全部未删除模板，立即停止新注册；
- 删除创建者不能连带删除被邀请账号 snapshot/profile；
- 删除被邀请账号不能删除模板/version/event 聚合历史。

### 7.6 测试闸门

- 有效 invite + 同 owner `urt_*`：referral 一条、两类 attribution 各一条、三文件、used_count +1；
- owner mismatch、模板过期/停用/删除：referral 保留、角色不应用、普通 onboarding；
- 无效 invite + 有效模板不允许绕过邀请策略；
- 老成员、重复登录、自邀、并发注册均不覆盖或重复计数；
- profile 中途异常整笔模板事务回滚；
- 运营 campaign、纯邀请码、无 code 注册逐项回归；
- beacon/funnel 同时接受 operator code 和 `urt_*`；
- SQLite/PG 的事务和幂等结果一致。

## 8. CRT-05：创建者 API、公共预览与统计（已完成）

### 8.1 修改文件

- `app/products/zhaoxi/api/creator_role_templates.py`（新增）
- `app/products/zhaoxi/application/creator_role_template_links.py`
- `app/products/zhaoxi/application/campaign_stats.py`（建议新增，提取日期范围 helper）
- `app/products/zhaoxi/api/admin_campaigns.py`
- `app/products/zhaoxi/manifest.py`
- `tests/test_creator_role_template_api.py`（新增或合入模板测试）
- `tests/test_web_creator_role_template_links.py`

### 8.2 创建者 API

实现技术设计 §5.1 的 list/create/detail/edit/review/publish/disable/enable/delete/stats。统一要求：

- `_require_session` + `principal.app_id=zhaoxi`；
- detail/mutation 先按 owner 查，非 owner 返回 404；
- Pydantic request `extra='forbid'`；
- feature flag 关闭时创建/修改类 API 返回稳定 capability disabled；
- 不接受 account id、creator id、原始 code、expires_at 或 used_count；
- 响应只在 effective active 时返回可复制 URL；
- 不实现 trial/debug/apply/switch endpoint。

### 8.3 Public preview

```text
GET /web/creator-role-template-links/{campaign_code}/preview?invite_code=<code>
```

- IP RPM 限流；
- 同时校验 template code、published version、状态/期限、个人邀请码和 owner 一致；
- 成功只返回名字、限长性格/使命摘要和 expires_at；
- 失败只返回稳定 reason，不泄露 creator id、手机号、used_count 或审核细节；
- preview 只改善展示，注册时必须重新校验。

### 8.4 Stats

从 `admin_campaigns.py::_resolve_stats_range` 提取共享 helper：默认最近 14 天、最大 92 天。创建者 stats 必须先按
owner 查询 template，再把服务端得到的 code 传入 `get_campaign_funnel()`；禁止接受用户任意 code 查询。

### 8.5 测试闸门

- owner/cross-app/session 权限；
- 全状态 API 和稳定错误码；
- public preview 最小字段、限流、owner mismatch/expired；
- creator/Admin 对同 code/同区间 `totals/rates/by_day` 完全一致；
- 任何 API 路由中不存在 trial/debug/apply/switch。

## 9. CRT-06：Admin/Staff 管理（已完成）

### 9.1 修改文件

- `app/products/zhaoxi/api/admin_creator_role_templates.py`（新增）
- `app/products/zhaoxi/manifest.py`
- `app/static/campaign_codes_admin.html`
- Admin API/静态页面测试

### 9.2 实现内容

- Admin/Staff 可分页、筛选和查看 template/version/review/event/聚合统计；
- disable/enable 必须写 actor、reason 和事件；
- 不允许 Admin 直接修改三字段或代用户创建；
- Reviewer P0 不增加入口；
- 现有运营活码 tab 的创建、编辑、启停和统计行为保持不变；
- 用户模板 tab 使用独立 API 和按钮，不能把用户资产提交到运营 PATCH。

### 9.3 测试闸门

- staff/admin 可读写处置，reviewer/普通用户拒绝；
- admin disabled 后创建者不能恢复；
- 审计 actor/reason/前后状态完整；
- 运营活码原有 API 和页面静态契约无回归。

## 10. CRT-07：个人中心、落地页和 Nginx（已完成）

### 10.1 修改文件

- `app/static/dashboard.html`
- `app/static/creator_role_templates.html`（新增）
- `app/static/home.html`
- `app/static/onboarding.html`
- `deploy/nginx/ai4company.top.conf`
- 静态页面与文档链接测试

### 10.2 创建者页面

- dashboard 原邀请卡片增加“创建角色模板”入口，不改变纯邀请码复制；
- 原生 HTML/CSS/JS 实现 0–3 模板列表、三字段表单、审核状态、发布/启停/删除、复制链接和统计；
- 所有用户/LLM 文本使用 `textContent`，禁止字符串拼接进 `innerHTML`；
- approved/active 状态展示“暂不支持在线调试；发布后请使用全新账号通过注册链接测试”；
- 不出现试玩、聊天、切换角色或 debug 控件。

### 10.3 落地与参数续传

- home/onboarding 从 URL 读取并保存 `invite_code + campaign_code`；
- OTP、刷新、登录、注册和扫码链路均保留两个参数；
- 调 public preview 展示角色承诺；失效时清除 template code、保留有效 invite code，并明确将走普通 onboarding；
- `/web/campaign-visit` 继续 fail-open，不让统计故障阻塞注册。

### 10.4 Nginx

增加 `/user/creator-role-templates.html` 精确白名单，header 与 dashboard location 一致。部署时更新仓库配置和真实
`/etc/nginx/conf.d/ai4company.top.conf`，执行 `nginx -t` 后 reload。

### 10.5 测试闸门

- dashboard 入口和纯邀请码功能同时存在；
- 表单、状态、无调试说明和复制 URL；
- 两个参数跨全链路不丢；
- preview 成功/失效提示；
- 静态断言无危险角色文本 innerHTML、无 trial/debug 路由；
- Nginx location 存在。

## 11. CRT-08：配置、回归与发布（开发与聚焦回归已完成；生产发布待执行）

### 11.1 配置

修改：

- `app/config.py`
- `.env.example`

只新增：

```text
CREATOR_ROLE_TEMPLATES_ENABLED=false
```

不增加 trial、Dreaming、cleanup、tool 或计费专用配置。开关关闭时停止新模板创建和新 `urt_*` 实例化，但已归因
账号继续使用自己的 snapshot/profile。

### 11.2 聚焦测试

开发阶段按工单运行，合并前至少执行：

```bash
.venv/bin/pytest \
  tests/test_creator_role_templates.py \
  tests/test_creator_role_template_review.py \
  tests/test_creator_role_template_runtime.py \
  tests/test_creator_role_template_api.py \
  tests/test_admin_creator_role_templates.py \
  tests/test_creator_role_template_static_pages.py \
  tests/test_web_creator_role_template_links.py \
  tests/test_web_onboarding.py \
  tests/test_db_campaign.py \
  tests/test_db_campaign_analytics.py -q

AI4ALL_TEST_DB=postgres .venv/bin/pytest \
  tests/test_creator_role_templates.py \
  tests/test_creator_role_template_review.py \
  tests/test_creator_role_template_runtime.py \
  tests/test_creator_role_template_api.py \
  tests/test_admin_creator_role_templates.py \
  tests/test_web_creator_role_template_links.py \
  tests/test_web_onboarding.py \
  tests/test_db_campaign.py \
  tests/test_db_campaign_analytics.py -q
```

若最终没有单独的 `test_creator_role_template_api.py`，从命令删除该文件并在交付说明中指出测试已合并到哪个文件，
不能保留不存在的命令。

### 11.3 完整回归

本需求触及 schema、注册、referral、campaign、prompt/tool、wipe 和多节点共享 PG，发布前必须执行：

```bash
.venv/bin/pytest tests/ -q
AI4ALL_TEST_DB=postgres .venv/bin/pytest tests/ -q
.venv/bin/pytest tests/test_documentation_links.py -q
```

未能运行其中任一项时，交付说明必须明确原因、替代验证和未覆盖风险。

### 11.4 当前开发验证记录（2026-08-01）

- SQLite 角色模板/campaign 聚焦集合：111 passed；
- SQLite Web onboarding：51 passed；
- SQLite 页面、Admin 视图与 Nginx 静态契约：8 passed；
- PostgreSQL 角色模板、审核、Runtime、Admin、Web 注册、onboarding 与 campaign 聚焦集合：157 passed；
- Admin/Staff 事件脱敏专项：SQLite 5 passed、PostgreSQL 5 passed；确认聚合统计可见，但
  `attribution_applied` 事件不返回被邀请账号 `account_id`；
- 多产品 referral/lifecycle 回归：9 passed，3 skipped（既有条件性跳过）；
- 文档链接：2 passed；Python `py_compile`、页面内联 JavaScript 语法检查和 `git diff --check` 通过。

完整 SQLite 套件已尝试，但当前运行环境有两项与本需求无关的既有阻断：系统 Python 链接的 SQLite 3.26
不支持 `ALTER TABLE ... DROP COLUMN`，导致 `test_account_app_id_repair_m0036` 失败；部分既有 FastAPI
`TestClient` 用例会无输出挂起（可在 `test_admin_campaigns.py::test_admin_can_create_campaign_code` 稳定复现）。
因此未把完整 SQLite/PG 套件标记为通过；生产发布前需在 CI/兼容环境补跑 §11.3，不能用上述聚焦结果替代。

## 12. 提交与合并建议

建议保持 5 个可审阅提交，不把全部功能塞入一个大提交：

1. `schema/domain`：migration 59、领域常量、persistence、双后端测试；
2. `review/lifecycle`：LLM 审核、版本、发布、启停和审计；
3. `registration/runtime`：dispatcher、可信 inviter、snapshot、onboarding、使命、wipe；
4. `api/ui/admin`：创建者/Admin API、统计、页面、preview、Nginx；
5. `config/docs/tests`：feature flag、回归补齐、运维和文档更新。

每个提交只包含对应工单，不顺手重构 campaign、referral、Runtime 或静态页面公共框架。

## 13. 生产发布顺序

### 13.1 发布前只读预检

1. 确认生产 schema version 为预期 head（当前计划为 58）；若已前进则调整 migration 编号。
2. 检查 `campaign_codes` 无大小写不敏感 `urt_` 前缀冲突。
3. 确认两个厚节点使用同一代码版本规划，只有 central 执行 DDL migration。
4. 备份中心 PostgreSQL，并确认最近恢复演练/备份状态正常。
5. feature flag 保持 false。

### 13.2 部署

1. 在 central 执行 additive migration 59，验证五张表和关键索引；
2. 两个节点部署后端，flag 继续关闭；
3. smoke：普通注册、纯邀请码、运营活码、普通 onboarding；
4. 部署静态页和 Nginx，执行 `nginx -t` 后 reload；
5. 给内部创建者灰度开启：创建→审核→发布→复制组合链接；
6. 用全新账号真实注册，核对 referral、snapshot、三文件、onboarding、无量化使命和漏斗；
7. 测试过期/owner mismatch 回退普通 onboarding；
8. 观察审核 error、归因 rollback、owner mismatch 和运营 campaign 回归指标，再扩大开放。

### 13.3 回滚

- 关闭 `CREATOR_ROLE_TEMPLATES_ENABLED`；
- 停止新创建/编辑/发布和新 `urt_*` 模板实例化；
- 不 drop migration 59、不删除模板历史、不改已有账号 snapshot/profile；
- 运营活码、个人邀请码和普通 onboarding 继续可用；
- 若静态页异常，回滚页面/Nginx 入口，不影响后端已有账号运行。

## 14. 完成定义

- PRD §15 全部验收项有自动化测试或生产 smoke 记录；
- migration 59、部分唯一索引和并发 slot 在 SQLite/PG 通过；
- LLM 不可用或内容拒绝时没有未审版本进入公开 preview/profile；
- 有效组合链接只产生一条 referral 和一份 immutable 模板 snapshot；
- owner mismatch/模板失效保留有效 referral，并回退普通 onboarding；
- 创建者当前 AI 在所有模板操作前后零写入；
- creator role 账号没有 `account_mission` 和量化使命工具，但 MISSION prose 正常进入 prompt；
- 创建者统计与 Admin 同 code/日期区间完全一致，且无用户明细泄露；
- 运营 campaign、纯邀请码、无 code、老成员、Native/Companion World 回归通过；
- 页面明确 P0 无在线调试并指引全新账号真实测试；仓库中无 trial 表/API/config/scheduler；
- SQLite/PG 完整回归通过，文档、`.env.example`、Nginx 和必要运维说明同步；
- 主要改动点和测试结果按 diff 级别记录，计划完成后归档本文。
