# 用户自建角色模板与邀请链接技术设计

更新时间：2026-08-02

> 状态：P0 与“自定义开场白 + 公开一句话简介”增量开发实现已完成，待合并与生产灰度；不实现试玩/调试。
> 对应 PRD：
> [用户自建角色模板与邀请链接](../../../products/zhaoxi/capabilities/creator_role_template_referral_link_prd.md)。
> 关联设计：
> [运营活码](campaign_codes_technical_design.md)、
> [Campaign 漏斗](campaign_funnel_analytics_technical_design.md)、
> [使命与自我状态](agent_mission_and_orchestration_design.md)、
> [增长与拉新](../../shared/platform/entitlement_growth_design.md)。

## 0. 结论

P0 主流程使用“独立角色模板资产 + 注册时实例化 + 复用 campaign 参数与漏斗”的方案：

1. 新建 owner-aware 的角色、版本、LLM 审核、审计和账号快照表，不把用户自由文本塞进现有静态
   `soul_preset_key` / `mission_id`。
2. 不把用户角色模板行混入运营 `campaign_codes`。两类 code 使用互斥命名空间，但继续共用
   `?campaign_code=`、`campaign_visits` 和 `account_campaign_attribution`。
3. 模板 code 固定使用保留前缀 `urt_`；运营建码禁止该前缀，从规则上保证两个表不会产生同码。
4. 每个创建者通过 1–3 的 `slot_no` 和数据库部分唯一索引获得最多 3 个未删除模板的并发硬约束。
5. 名字、性格、使命、开场白在一次 LLM 调用中整体审核；不静默改写。通过后生成默认公开简介，只得到 approved version，不自动发布。
6. 创建者首次主动发布时设置 `expires_at = activated_at + 360 天`；发布新版、编辑、停用再启用不续期。
7. 注册时先完成现有 referral 事务，再校验模板 code owner。owner 一致才原子写模板实例化快照、通用
   campaign 归因、三个 profile 文件和模板实例化次数；不一致或失效时保留拉新、回退普通 onboarding。
8. 自由使命只写 `MISSION.md` prose，不写 `account_mission`，不开放量化使命工具，也不再分配默认使命。
9. 模板永远不能应用到或覆盖创建者当前 AI；创建只产生模板，真实新用户注册实例化才产生角色。
10. P0 不创建 trial Runtime Account，不增加试玩表/API、临时存储、能力门控、清理 scheduler 或隐藏调试入口。
    创建者如需验证，使用发布链接和一个尚未加入朝夕的新账号完成真实注册；该账号走全部正式链路。

## 1. 当前基线

### 1.1 已有可复用能力

| 能力 | 当前实现 | 本设计复用方式 |
| --- | --- | --- |
| 个人邀请码 | `referral_codes`、`referral_relationships`，按 `platform_user_id + app_id` 隔离 | 原样复用奖励关系；不新增奖励表 |
| 用户个人中心 | `app/static/dashboard.html`、`GET /web/me/referral-code` | 增加角色管理入口和独立页面 |
| 活码注册参数 | `home.html` / `onboarding.html` 读取 `campaign_code` 并随注册请求提交 | 参数名和前端续传链保持不变 |
| 运营活码归因 | `campaign_codes`、`account_campaign_attribution`、`apply_campaign_code_attribution` | 保留运营逻辑；新增 code source dispatcher |
| 漏斗 | `campaign_visits` + `get_campaign_funnel` | 用户角色模板仍写通用 attribution，因此统计无需复制 |
| 账号 profile | `account_profile_files`，`profile_storage.write_file(..., conn=...)` 支持事务写入和删除 | 正式归因快照与三个文件同事务写入 |
| Onboarding 强制身份 | `has_forced_ai_name` / `has_forced_soul_preset` | 统一为 identity override 投影，兼容运营活码与用户角色模板 |
| LLM 审核基础 | `TASK_MODERATION`、`generate_completion`、`_extract_json_object` | 复用 provider 和严格 JSON 解析，不复用静默改写语义 |

### 1.2 不能直接复用的部分

- `campaign_codes` 是 Admin/Staff 可编辑的运营配置，没有 creator owner、版本、3 个槽位或审核状态。
- 当前 `soul_preset_key` 只接受工程注册的静态模板，不能承载任意性格文本。
- 当前 `mission_id` 必须命中静态使命注册表，并天然带目标数、瞬间记录和进度工具；与已确认的
  “自由使命 prose、不量化”语义冲突。
- `app.platform.moderation.text_sanitizer` 会静默改写文本；本需求要求审核通过/不通过，不能在用户不知情
  时改变角色设定，因此只复用其 provider/JSON 安全模式，不直接调用 `sanitize_text()`。

### 1.3 渠道范围

P0 注册链接只接朝夕官网 Web/H5 的 `/web/login` 与 `/web/register-and-binding-intent` 默认关系账号。
Native App auth、Companion World 居民创建、世界访问邀请码不消费用户角色模板 code，
也不增加模板调试入口。已有运营活码行为不变。

### 1.4 与运营后台建活码的复用边界

现有 `campaign_codes_admin.html`、`admin_campaigns.py` 和 campaign persistence 是本能力的直接参考基线。
逐项结论如下：

| 运营活码现有做法 | 用户角色模板链接方案 | 结论 |
| --- | --- | --- |
| 同一个 `campaign_code` query 参数从落地页续传到注册 | 继续使用 `campaign_code`，与 personal `invite_code` 同链携带 | 直接复用 |
| `active/disabled + valid_from/expires_at` 决定新注册是否消费 | 继续按 active/disabled/expiry 校验；角色没有未来 `valid_from` | 复用有效窗口语义 |
| 失效 code 不阻断注册，回退默认 onboarding | 角色失效也不阻断，且必须保留有效 personal referral | 直接复用 fail-open 产品口径 |
| 注册时写 `account_campaign_attribution` 策略快照 | 同样写通用归因，再写更完整的模板版本实例化快照 | 扩展复用 |
| `used_count` 只在成功归因时增加 | 角色 `used_count` 也只随成功账号快照增加 | 直接复用 |
| `campaign_visits` + `get_campaign_funnel` | 同 code 维度复用完整漏斗和按日统计 | 直接复用 |
| Admin/Staff 创建、编辑、启停 | Admin/Staff 查看和处置用户角色模板；内容创建/编辑只属于 creator | 复用鉴权和处置范式，不复用创建权限 |
| 后台原生 HTML/JS、转义渲染、统计卡片 | 在同一活动码管理页增加“用户角色模板链接”视图并复用统计组件 | 直接复用 UI 技术栈 |
| 运营手工输入可读 code | 用户 code 必须系统随机生成，防冲突和仿冒 | 明确分开 |
| 默认有效期 3 个月，可运营修改 | 固定首次主动发布后 360 天，发布新版/编辑/启停不续期，用户不可改 | 明确分开 |
| 静态 `soul_preset_key/mission_id` + 可选 onboarding 文本 | 自由四字段、版本化、LLM 审核、非量化使命 | 明确分开 |
| 无个人 creator owner、无数量上限 | owner-scoped、最多 3 个未删除模板 | 明确分开 |

因此不复制一套新的“落地页→归因→统计”链路，也不直接把用户角色模板当成普通运营行。若把用户角色模板塞进
`campaign_codes`，现有 Admin PATCH 将能绕过 creator 版本/审核直接改内容，列表也会泄露或误操作用户资产；
同时 3 个月默认期、手工 code 和 creator 360 天规则会形成多个条件分支。独立资产表 + 统一 campaign source
resolver 能保留成熟链路，同时把权限和生命周期隔离清楚。

## 2. 总体链路

```text
创建者个人中心的模板创建主流程（创建模板本身不创建 AI account）
  → POST /web/me/creator-role-templates（名字、性格、使命、开场白）
  → DB 申请 slot 1..3 + 创建 pending version + urt_* code
  → 单次 LLM 审核四字段并生成默认公开简介
      ├─ pass：version approved；创建者可在发布前提交一次简介修改审核
      ├─ reject：version rejected；不提供有效注册链接
      └─ error：pending_review；创建者可重试
  → 创建者主动发布
      ├─ 首次发布：写 activated_at/expires_at(+360d)
      └─ 发布新版：切 published version，不续期
  → GET/list 返回注册链接：/?invite_code=<personal>&campaign_code=<urt_*>

新用户打开链接
  → 公共 preview 校验 invite_code、template code、owner、状态、有效期
  → Web 注册提交两个参数
  → register_platform_user_with_referral（现有事务）
  → create default account
  → apply registration campaign
      ├─ 普通运营 code：现有逻辑不变
      └─ urt_*：校验 referral inviter == role creator
           → 同事务写通用 campaign attribution + template version snapshot
           → 同事务写 IDENTITY.md + SOUL.md + MISSION.md + opening_line 快照 + used_count
  → onboarding 只问用户称呼，随后使用开场白，跳过 AI 名字/人设和默认 1–4 破冰菜单
  → complete 时检测 creator role template attribution，跳过默认量化使命分配
```

## 3. 数据模型（migration 59 + 60）

migration 59 建立角色模板、版本、主审核、归因和事件表；migration 60 为已有库增加开场白、默认/公开简介、
简介编辑状态、账号开场白快照和简介审核 run 表。实际 DDL
通过仓库现有 DB 适配层执行，SQLite 与 PostgreSQL 字段、索引和约束保持一致。

### 3.1 `creator_role_templates`

一行代表创建者持有的一个模板槽位和一条稳定分享 code；内容放在版本表。

| 字段 | 约束与语义 |
| --- | --- |
| `id` | PK，`crtpl_*` |
| `app_id` | 固定 `zhaoxi`，所有 owner 查询必须携带 |
| `creator_platform_user_id` | FK `platform_users.id`，模板 owner |
| `slot_no` | `1..3`；未删除模板按 owner/app/slot 唯一 |
| `campaign_code` | UNIQUE，系统生成 `urt_` + 128bit 以上随机 URL-safe token |
| `status` | `pending_review / approved / active / rejected / disabled_creator / disabled_admin / deleted` |
| `used_count` | 成功从模板实例化账号角色的累计次数，不等于拉新奖励人数 |
| `activated_at` | 第一个版本首次主动发布时间；一经设置不改 |
| `expires_at` | `activated_at + 360 天`；一经设置不因编辑/启停改变 |
| `disabled_by_admin_user_id` / `disabled_reason` | 运营处置审计；创建者不能自行解除 admin disabled |
| `status_before_admin_disable` | Admin 停用前状态；只供 Admin 恢复，不由创建者修改 |
| `deleted_at` | soft delete；非空后释放 slot、code 永久失效 |
| `created_at` / `updated_at` | 北京时间，与仓库现有表一致 |

关键索引：

```sql
CREATE UNIQUE INDEX ux_creator_role_templates_owner_slot_live
ON creator_role_templates(creator_platform_user_id, app_id, slot_no)
WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX ux_creator_role_templates_campaign_code
ON creator_role_templates(campaign_code);

CREATE INDEX ix_creator_role_templates_owner_list
ON creator_role_templates(creator_platform_user_id, app_id, deleted_at, updated_at DESC);
```

创建事务依次尝试 slot 1、2、3；每次插入放在 savepoint 中，唯一冲突则尝试下一个。三个槽都冲突时返回
`creator_role_template_limit_reached`。这比“先 COUNT 再 INSERT”更能抵抗 PG 多节点并发；SQLite 写事务同样适用。

### 3.2 `creator_role_template_versions`

创建和每次编辑都新增版本，不原地覆盖已发布内容。

| 字段 | 约束与语义 |
| --- | --- |
| `id` | PK，`crtv_*` |
| `creator_role_template_id` | FK `creator_role_templates.id` |
| `version_no` | 从 1 递增；UNIQUE `(creator_role_template_id, version_no)` |
| `ai_name` | 创建者填写的名字，1–24 字符、单行 |
| `personality_text` | 性格/底色，1–800 字符 |
| `mission_text` | 非量化使命 prose，1–500 字符 |
| `opening_line` | onboarding 收尾开场白，1–300 字符；历史版本允许 NULL |
| `generated_summary` / `public_summary` | LLM 默认简介与最终公开简介；必须包含角色名 |
| `summary_edit_status` | `unavailable / available / reviewing / accepted / rejected`；控制每版本一次修改机会 |
| `review_status` | `pending / reviewing / passed / rejected` |
| `is_published` | 0/1；每个模板最多一个当前 published version；approved candidate 为 0 |
| `review_categories_json` / `review_reason` | 结构化、限长审核结论；不保存审核 prompt |
| `reviewed_at` / `published_at` | 审核和成为 published version 的时间 |
| `created_at` / `updated_at` | 审计时间 |

关键索引：

```sql
CREATE UNIQUE INDEX ux_creator_role_template_versions_published
ON creator_role_template_versions(creator_role_template_id)
WHERE is_published = 1;

CREATE UNIQUE INDEX ux_creator_role_template_versions_open_review
ON creator_role_template_versions(creator_role_template_id)
WHERE review_status IN ('pending', 'reviewing');
```

编辑传部分字段时，服务端先与当前 published version 合并成完整四字段，再创建新版本并整体重审、重新生成默认简介。
审核通过只产生 approved candidate；旧 published version 继续服务。创建者主动“发布新版”时，才在同一
事务把旧版 `is_published=0`、新版 `is_published=1`。P0 不提供发布前试玩，创建者可使用发布后的链接和真实新账号
验证当前 published version。

### 3.3 `creator_role_template_review_runs`

每次首次审核或重试一行，保留调用级审计而不覆盖历史。

| 字段 | 说明 |
| --- | --- |
| `id` / `creator_role_template_version_id` / `attempt_no` | PK、版本 FK、版本内唯一尝试序号 |
| `status` | `running / passed / rejected / error` |
| `model` / `provider` / `latency_ms` | 模型与性能信息 |
| `categories_json` / `reason` | 结构化结果；不存完整模型原始输出 |
| `error_code` | provider/timeout/schema 等稳定错误码 |
| `started_at` / `finished_at` | 调用审计时间 |

### 3.4 `account_creator_role_template_attribution`

一行代表被邀请账号注册时取得的不可变模板实例化快照。

| 字段 | 约束与语义 |
| --- | --- |
| `account_id` | PK、FK `accounts.id`；一个账号最多一个 creator role template |
| `creator_role_template_id` / `creator_role_template_version_id` | 来源模板与确切 published version |
| `creator_platform_user_id` | 注册时 owner 快照，用于审计，不用于授权 |
| `campaign_code` | 当时使用的 `urt_*` code |
| `ai_name_snapshot` | 通过审核的名字 |
| `personality_snapshot` | 通过审核的性格/底色 |
| `mission_snapshot` | 通过审核的非量化使命 prose |
| `opening_line_snapshot` | 通过审核的开场白；按 `account_id` 隔离，历史归因允许 NULL |
| `attributed_at` | 注册归因时间 |

账号运行时只读取自己的 `account_id` 快照或已经写入的 profile 文件，不实时 join 创建者模板；创建者编辑、
发布新版、停用、删除或注销都不能改变已有角色实例。

### 3.5 `creator_role_template_summary_review_runs`

记录创建者对未发布版本公开简介的一次性修改审核。包含 `submitted_summary`、版本内 `attempt_no`、
`running/passed/rejected/error`、provider/model/latency、类别、原因和错误码。明确 pass/reject 消耗机会；
provider、timeout 或 schema error 恢复为 `available`，机械校验失败不创建 run。

### 3.6 `creator_role_template_events`

记录 `created / review_started / review_passed / review_rejected / review_error / version_activated /
disabled_by_creator / enabled / disabled_by_admin / deleted / attribution_applied`。字段包含 role/version、
actor type/id、限长 metadata JSON 和时间。用户操作与管理员操作共用审计格式，但 API 权限仍分开。

### 3.6 复用现有表

- `campaign_visits`：继续按 `campaign_code` 记录匿名 PV/UV，不增加用户标识。
- `account_campaign_attribution`：角色成功归因时仍写一行，`campaign_code=urt_*`，现有四个运营策略字段
  全为 NULL。它是 S1–S5 漏斗 join 锚点，不是模板内容真相源。
- `referral_relationships`：原样保存邀请关系和奖励状态；不写角色字段。
- `account_profile_files`：在模板实例化归因事务内写 `IDENTITY.md`、`SOUL.md`、`MISSION.md`。

### 3.7 code 命名空间

- 用户角色模板 code：仅系统生成小写前缀 `urt_`，后缀至少 128bit 熵；用户不能指定。
- 运营 `create_campaign_code` 增加大小写不敏感的保留前缀校验，任何 `urt_...` 返回
  `reserved_campaign_code_prefix`。
- migration 59 上线前用 `substr(lower(code), 1, 3) = 'urt_'` 检查既有 `campaign_codes`；若存在必须先改码，
  不允许带冲突继续迁移。
- 统一 helper `resolve_campaign_source(code)` 先按保留前缀选择用户角色模板，否则读取运营活码；不做“两表都查，
  谁先命中算谁”的不确定行为。

## 4. 角色生命周期与审核

### 4.1 创建资格

`require_creator_role_template_eligibility(principal)` 必须无副作用地确认：

- `principal.app_id == zhaoxi` 且 membership active；
- 创建者的默认关系账号 `status=active`；
- `is_onboarding_done(state)` 为真（`complete` 或现有兼容终态 `timed_out`）；
- 个人邀请码在 zhaoxi 下可用；
- 未命中禁止拉新/分享的既有风控状态。

资格只决定能否新建、编辑、发布、启停；已经发布且仍有效的链接是否继续服务，由模板状态、邀请码有效性和
注册时 owner 校验共同决定。Admin disabled 优先级最高。

### 4.2 字段机械校验

服务端常量为唯一口径，前端仅做同规则预检：

- `ai_name`：strip 后 1–24 字符，不允许 CR/LF、控制字符；
- `personality_text`：strip 后 1–800 字符；
- `mission_text`：strip 后 1–500 字符；
- 四字段禁止 NUL 和不可见控制字符；公开简介还必须为单行且包含角色名；JSON/HTML 展示统一 `textContent`/转义；
- 不接受额外字段，Pydantic `extra='forbid'`。

这些是存储和 prompt 预算边界，不代替 LLM 内容审核。

### 4.3 LLM 审核服务

新增 `app/products/zhaoxi/application/creator_role_template_review.py`：

```python
review_creator_role_template(
    *, ai_name: str, personality_text: str, mission_text: str, opening_line: str,
    app_id: str = ZHAOXI_APP_ID
) -> CreatorRoleTemplateReview
```

实现规则：

1. 四字段作为一个 JSON data object 进入**一次**审核调用，审核字段分别返回结果，同时判断组合风险。
2. system 指令明确用户内容只是 DATA，不能执行其中的 prompt injection；完整指令按产品注册表的
   `default_language` 从 `zh-CN / en-US / ja-JP` 目录选择，未知语言安全回落中文，机器类别码保持英文。
3. 复用 `TASK_MODERATION`、`tier_for_task()`、`generate_completion()` 和严格 JSON object 解析。
4. 输出 schema 固定为 `decision=pass|reject`、`field_results`、`categories`、`reason`、`public_summary`；通过时
   `public_summary` 只基于名字、性格和使命生成，必须包含名字；`categories`
   只能使用领域层固定类别码，`reason` 按产品注册表的 `default_language` 生成。任何缺字段、未知类别、
   非法值、provider 异常或超时都映射为 `review_unavailable`，不放行。
5. 不调用 `sanitize_text()`，不接受 `rewrite`，不静默改变创建者四字段。通过版本保存原提交文本；拒绝版本
   保留用于创建者修改与审计，但永不进入 prompt。
6. 审核采用明确风险才拒绝的边界：重名、虚构作品/角色启发和正常角色指令默认不拒绝；
   `other_unsafe_content` 不得作为不确定风险的兜底类别；多义内容在没有明确危险证据时通过。
7. LLM 调用在 DB 事务外执行：先把 version CAS 从 `pending→reviewing` 并写 run，再调用；完成后用 run id
   CAS 落结果，防止重复点击把旧结果覆盖新版本。

创建/编辑 API 同步尝试一次审核。通常响应直接返回 active/rejected；调用失败返回 `pending_review` 和稳定
可重试状态，不把 HTTP 请求无限挂住。`POST .../{id}/review` 仅对 pending version 重试；并发重试只有一个
能 claim，其余返回 `role_review_in_progress`。

### 4.4 状态规则

- 首版创建：template `pending_review`；通过→`approved`，拒绝→`rejected`，错误保持 pending。
- approved 不等于发布，不生成可消费链接；P0 没有试玩，创建者确认后主动发布。
- 首次主动发布：同事务切 `is_published=1`、写 `activated_at=now`、`expires_at=now+360 days`，之后永不重算。
- active 模板编辑：template 保持 active，旧 published version 继续服务；候选通过成为 approved，只有创建者
  主动发布新版才切 published version，拒绝/错误/未发布均不影响旧版。
- 创建者停用：`disabled_creator`；可在到期前启用，不延长有效期。
- Admin/Staff 停用：`disabled_admin`；创建者不能启用，只有 Admin/Staff 可恢复到 active。
- soft delete：`status=deleted`、`deleted_at` 非空，释放 slot；不可恢复，code 永久失效。
- 过期不需要 scheduler 改行；`now >= expires_at` 即派生 `effective_status=expired`。过期角色仍占槽，创建者
  soft delete 后才能释放。

## 5. API 设计

新增产品模块 `app/products/zhaoxi/api/creator_role_templates.py`，由 manifest 挂载，不把产品业务继续堆进共享
`app/routers/web.py`。所有 `/web/me/*` API 使用 `_require_session` 并二次校验 `app_id=zhaoxi`。

### 5.1 创建者 API

| Method | Path | 说明 |
| --- | --- | --- |
| `GET` | `/web/me/creator-role-templates` | 列出本人 0–3 个未删除模板、published/approved/pending 版本摘要 |
| `POST` | `/web/me/creator-role-templates` | 申请空闲 slot、创建 v1、同步尝试 LLM 审核 |
| `GET` | `/web/me/creator-role-templates/{template_id}` | owner-scoped 详情；非 owner 统一 404 |
| `PATCH` | `/web/me/creator-role-templates/{template_id}` | 合并字段后创建新版本并重审，不原地覆盖 published version |
| `POST` | `/web/me/creator-role-templates/{template_id}/review` | 重试 pending review |
| `POST` | `/web/me/creator-role-templates/{template_id}/summary-edit` | 对未发布版本提交唯一一次简介修改审核；明确拒绝也关闭机会，服务异常不消耗 |
| `POST` | `/web/me/creator-role-templates/{template_id}/publish` | 发布 approved version；首次发布开始 360 天，发布新版不续期 |
| `POST` | `/web/me/creator-role-templates/{template_id}/disable` | 创建者停用 |
| `POST` | `/web/me/creator-role-templates/{template_id}/enable` | 仅到期前、非 admin disabled 时启用 |
| `DELETE` | `/web/me/creator-role-templates/{template_id}` | soft delete 并释放 slot |
| `GET` | `/web/me/creator-role-templates/{template_id}/stats` | 与运营一致的 owner-scoped 漏斗，`from/to` 规则相同 |

模板响应只在 `effective_status=active` 且存在 published version 时返回 `registration_url`。URL 由服务端返回 code 与 personal invite
code，前端使用 `location.origin + '/?invite_code=...&campaign_code=...'` 组装，避免再新增公网 origin 配置；
生产仍得到 `https://ai4company.top/`。

模板与版本响应保留 `status`、`effective_status`、`review_status` 等稳定机器码，并额外返回
`*_display` 展示字段。用户 API 不返回原始 `review_reason`，只返回按产品默认语言确定性生成的
`review_reason_display`；LLM 原始理由仅保留在数据库和管理审核接口中供审计。历史未知类别统一回落到
对应语言的通用拒绝提示。

稳定错误码：

- `creator_role_template_not_eligible`
- `creator_role_template_limit_reached`
- `creator_role_template_not_found`
- `creator_role_template_review_in_progress`
- `creator_role_template_review_unavailable`
- `creator_role_template_content_rejected`
- `creator_role_template_expired`
- `creator_role_template_admin_disabled`
- `creator_role_template_no_published_version`

### 5.2 公共预览

```text
GET /web/creator-role-template-links/{campaign_code}/preview?invite_code=<code>
```

按 IP RPM 限流，依次验证：保留前缀、模板存在、published version、未删除/停用/过期、个人邀请码有效、
邀请码 owner 与模板 creator 一致。成功只返回：

```json
{
  "valid": true,
  "role": {
    "name": "...",
    "summary": "...",
    "expires_at": "..."
  }
}
```

公开简介来自审核通过版本的 `public_summary`，历史版本缺失时只回退为角色名；接口不返回性格、使命或开场白。
前端只用 `textContent`。失败返回稳定 reason，不泄露 creator ID、手机号、使用次数或
审核细节。落地页失败时清除待提交的 template code、保留有效 invite code，并显示“角色模板已不可用，将按普通
流程创建朝夕伙伴”。注册提交时仍重新校验，不能信任 preview。

### 5.3 Admin/Staff API

新增 `app/products/zhaoxi/api/admin_creator_role_templates.py`：

- `GET /admin/creator-role-templates`：按 creator、status、review status、日期分页；
- `GET /admin/creator-role-templates/{id}`：模板、版本、审核 runs、聚合统计和事件；
- `POST /admin/creator-role-templates/{id}/disable`：必填 reason，写 `disabled_admin`；
- `POST /admin/creator-role-templates/{id}/enable`：仅未过期；按停用前状态恢复，未发布模板可恢复到原审核/approved 状态；
- 不提供管理员直接改写四字段或公开简介的 API，避免审计不清；运营只能查看审计并处置状态。

鉴权使用 `require_admin_or_staff_user`。Reviewer P0 不新增入口；若后续增加人工复核，另行设计 reviewer
最小权限和队列，不让 Reviewer 获得用户/增长管理权限。

Admin UI 不另起孤立工具页：扩展现有 `app/static/campaign_codes_admin.html`，增加两个清楚分离的视图：

- “运营活码”：现有创建、编辑、启停和统计功能逐字保留；
- “用户角色模板链接”：列表/筛选 creator、模板四字段、默认/公开简介、版本/LLM 审核/发布摘要、状态、360 天有效期、used_count、
  同口径统计以及 Admin/Staff 停用/恢复；不提供直接编辑用户内容或代用户创建入口。

两个视图共用日期范围、漏斗渲染、HTML 转义和 API error 展示 helper，但使用不同 API 和操作按钮，避免
运营误把用户角色模板按普通营销活码编辑。

### 5.4 统计 API

把 `admin_campaigns.py::_resolve_stats_range` 提取到产品 application helper，Admin 与创建者 API 共用：

- 默认最近 14 个北京自然日；
- 单次区间最大 92 天；
- 返回现有 `totals / rates / by_day` 全字段；
- 创建者 endpoint 必须先用 `(template_id, creator_platform_user_id, app_id)` 完成 owner 查询，再把 code 传给
  `get_campaign_funnel`，禁止直接按用户传入 code 查统计。

## 6. 注册双归因

### 6.1 取得可信 inviter

当前 Web wrapper 丢弃了 `register_platform_user_with_referral()` 返回的 `is_new_membership` 和
`referral_relationship`。两条 P0 注册 handler 改用 `_register_platform_user_with_otp_result()`：

1. 现有事务消费 OTP、创建 membership、校验 personal code、创建 referral relationship；
2. 从返回的 relationship 取得可信 `inviter_platform_user_id`；客户端不能直接提交 creator id；
3. 只有 `is_new_membership=true` 且确实创建了个人邀请关系时，才把 expected creator 传给模板实例化归因；
4. 已有朝夕成员没有新 relationship，模板 code 不消费、不覆盖账号。

### 6.2 campaign dispatcher

`apply_campaign_code_attribution()` 增加可选的
`expected_creator_platform_user_id` 和 `is_new_membership`：

- 非 `urt_` code：逐字保持现有运营活码校验、快照和 fail-open 行为；
- `urt_` code：调用 `apply_creator_role_template_attribution()`；缺 expected creator、owner 不一致或非新 membership
  均返回未应用原因，不写任何角色/campaign 数据；
- debug 建号没有真实 referral owner，默认不能消费用户角色模板 code，避免污染用户拉新和角色统计。

`get_or_create_default_ai4all_account_for_user()` 只在真正创建新账号时调用 dispatcher，现有账号直接返回，
因此重复登录不会二次应用。

### 6.3 模板实例化归因原子事务

`apply_creator_role_template_attribution(account_id, code, expected_creator)` 在一个 DB 事务中：

1. 按 code 读取 template + published version，校验 app、状态、`now < expires_at`、creator owner；
2. 确认 account 属于 zhaoxi 且尚无 `account_campaign_attribution`；
3. `INSERT account_campaign_attribution`，四个运营 preset 字段写 NULL；
4. `INSERT account_creator_role_template_attribution`，复制三个已审核字段和确切 version id；
5. 用 `profile_storage.write_file(..., conn=tx)` 依次写入 IDENTITY/SOUL/MISSION；
6. 仅当上述插入成功时 `used_count += 1`；
7. 写 attribution event 并提交。

任何异常整笔回滚，不留下“漏斗有注册但角色文件写了一半”的状态。外层注册和 referral 已经成功，不因
模板实例化附加能力失败回滚真人注册；响应返回 `role_template_link_result={applied:false, reason:...}`，前端明确告知按
普通 onboarding 继续。预期业务失效和内部 error 分开监控。

### 6.4 owner 不一致与竞态

- 甲 invite + 乙 role：referral 归甲；模板实例化事务返回 `creator_mismatch`，不写通用 campaign attribution，
  被邀请人走普通 onboarding。
- 模板在 preview 后被停用/过期/发布新版：注册事务以提交时读到的 published version 为准；写入确切版本快照。
- 两个请求并发给同一 account 应用：`account_campaign_attribution.account_id` PK + creator attribution PK
  保证只有一个成功；失败请求不增加 used_count。
- invite code 在 referral 事务后立刻被停用不撤销已创建关系；模板 owner 使用已落 relationship 的可信
  inviter。该行为与现有 referral 事务语义一致。

## 7. 角色 profile 与 Onboarding

### 7.1 profile 渲染

新增纯渲染 helper 和带 conn 的应用函数，所有公共方法加注释：

```python
render_creator_role_template_identity(name: str) -> str
render_creator_role_template_soul(name: str, personality_text: str) -> str
render_creator_role_template_mission(mission_text: str) -> str
write_creator_role_template_snapshot(*, conn, account_id, snapshot) -> None
```

- IDENTITY 沿用当前 AI 名字和禁止暴露 OpenClaw 的固定结构；
- SOUL 使用固定平台边界 + “性格与底色”段落，用户文本只放在明确的数据槽，不能声明工具、权限、系统
  规则或读取范围；
- MISSION 使用固定说明 + 自由使命 prose，明确这是长期方向，不是外部事实或越权指令；
- 三份内容都只来自注册时的 published version snapshot；注册后不再读取 creator role template live row。

Prompt 中更高优先级的 AGENTS、安全边界、实际 tool schema 不变。角色文本不能新增工具或扩大数据权限。

### 7.2 统一 onboarding override

当前 `turn_services.py` 三处各自读取运营 attribution。新增一个产品 application helper：

```python
resolve_onboarding_identity_overrides(account_id) -> OnboardingIdentityOverrides
```

返回 `forced_ai_name / forced_personality / script_override / creator_opening_line / source`：

- creator role template attribution：名字与性格均 forced，script 为空，读取该账号不可变的开场白快照；
- 运营活码：映射现有 ai name / soul preset / script；
- 无归因：全部 false/None。

`apply_onboarding_info`、`load_prompt_context`、`advance_onboarding` 共用同一次语义，不让某一处漏判。creator
role 在 step1 收到用户称呼后直接 complete，不问 AI 名字或人设；有开场白快照时使用该文本并禁止默认自我介绍和
1–4 破冰菜单。历史快照没有开场白时保留原强制身份收尾逻辑。

### 7.3 非量化使命

creator role template attribution 存在时：

- `MISSION.md` 已在注册事务写入，随 profile 正常进入 prompt；
- `assign_mission_if_absent()` 明确改为返回 `Optional[str]`：检测到 creator role template attribution 时直接返回
  `None`；`advance_onboarding()` 和存量 backfill 继续只调用这一统一入口，不在调用方复制判断；
- `account_mission` 不写行，`resolve_account_mission()` 返回 None；
- `has_mission=False`，不提供 `mission_status` / `record_mission_moment`；
- `build_agent_self_state_block()` 仍可渲染关系/需求状态，但不渲染量化使命进度；
- 存量使命 backfill 也必须跳过有 creator role template attribution 的账号，避免日后补上第二个使命。

这不是“没有使命”：使命 prose 已在上下文中；只是没有现有结构化量化使命能力。

### 7.4 reset、wipe 与注销

- onboarding debug reset 继续只用于现有内部账号；P0 不增加 creator role template debug 建号或重放入口。
- `wipe_account_data` 删除 `account_creator_role_template_attribution` 和三个 profile 文件；通用
  `account_campaign_attribution` 按现有统计保留策略处理。
- 创建者注销：在 `account_deletion.py` 中把其全部未删除模板 soft delete，立即停止新注册；已有被邀请人
  profile 和快照不变。
- 删除模板只释放 slot，不物理删除被引用版本、review/event 审计或账号快照。

### 7.5 创建者当前 AI 不可切换

这是跨实现层的永久不变量，不是 feature flag：

- 创建、编辑、审核、发布、停用、删除模板都不能写创建者默认 account 的
  `account_profile_files / account_mission / sessions / memories / account_user_meta`；
- 注册 dispatcher 只有 `is_new_membership=true` 且可信 referral inviter 与模板 owner 一致时才实例化；
  创建者已有 zhaoxi membership，自点链接也不能应用模板；
- 创建者 API 不接受 `account_id`，服务端不能提供 `apply_to_my_account`、`switch` 或等价内部方法；
- 将来即使支持“从模板新建另一个独立 AI”，也必须是新的产品能力，不能覆盖或复用当前 AI 的关系状态。

### 7.6 P0 无试玩/调试实现

- 不新增 trial/preview conversation 数据表，不在 `accounts` 增加临时账号类型。
- 不新增试玩 turn API、WebSocket/SSE 会话、tool capability 分支、计费分支或专用日志。
- 不修改 Dreaming、session lifecycle、账号 wipe、Companion World auxiliary account 或 owner resolver。
- 不增加 `apply_to_my_account`、`switch`、debug bootstrap 或仅管理员可见的隐藏调试方法。
- 创建者页面只展示说明：发布后使用全新朝夕账号打开注册链接完成真实测试。该账号从现有注册入口进入，完全复用
  §6 的双归因和 §7.1–§7.3 的正式实例化/onboarding，不需要任何测试专用 Runtime 代码。
- V2 若重新立项，另写独立 ADR/技术设计；当前 migration、API 和代码不为未知方案预埋抽象。

## 8. 前端与 Nginx

### 8.1 页面

- `app/static/dashboard.html`：在现有邀请卡片增加“创建角色模板”入口，不改纯邀请码功能。
- 新增 `app/static/creator_role_templates.html`：原生 HTML/CSS/JS，模板列表、四字段表单、默认/公开简介、一次性简介修改、审核/发布状态、
  发布、复制链接、启停/删除和统计；不引入框架或依赖。
- approved/active 状态附近展示固定说明：“当前暂不支持在线试玩。若需验证角色实际效果，请发布并复制注册链接，
  使用一个尚未加入朝夕的新账号完成注册测试。”不展示试玩、调试、切换或临时账号按钮。
- `app/static/home.html` / `onboarding.html`：读取两个参数后调用公共 preview；展示角色摘要或失效回退提示；
  OTP/登录/扫码期间继续保留两个参数。
- 所有用户/LLM 文本用 `textContent`，不得通过字符串拼 `innerHTML`；复制动作只复制服务端确认 active 的 URL。

### 8.2 Nginx

生产 Nginx 是显式白名单，需在 `deploy/nginx/ai4company.top.conf` 增加：

```nginx
location = /user/creator-role-templates.html {
    proxy_pass http://127.0.0.1:8180/ui/creator_role_templates.html;
    # headers 与 dashboard location 一致
}
```

现有 `/api/web/` 反代已覆盖新增 `/web/*` API，无需新增 API location。部署时同步更新真实
`/etc/nginx/conf.d/ai4company.top.conf`，执行 `nginx -t` 后 reload。

## 9. 模块与文件改动

| 文件 | 主要改动 |
| --- | --- |
| `app/db/_core.py` | migration 59/60、表/增量列/索引、跨后端 checkpoint |
| `app/products/zhaoxi/domain/creator_role_templates.py` | 状态、字段上限、code prefix、DTO/domain 校验 |
| `app/products/zhaoxi/infrastructure/persistence/creator_role_templates.py` | owner-scoped CRUD、slot、版本、review run、快照、事件 |
| `app/products/zhaoxi/application/creator_role_template_review.py` | LLM 审核与严格 schema |
| `app/products/zhaoxi/application/creator_role_template_links.py` | 生命周期、资格、URL/preview、注册归因编排 |
| `app/products/zhaoxi/application/onboarding.py` 或邻近 helper | 统一 identity overrides |
| `app/products/zhaoxi/application/missions/assignment.py` | creator role template 账号跳过量化使命 |
| `app/products/zhaoxi/infrastructure/profiles.py` | 角色 profile 纯渲染与事务写入 helper |
| `app/products/zhaoxi/infrastructure/persistence/campaign.py` | code source dispatcher；运营 code 行为不变 |
| `app/products/zhaoxi/infrastructure/persistence/campaign_analytics.py` | 接受 `urt_*` 通用归因；统计计算本身不改 |
| `app/routers/web.py` | 注册 handler 保留完整 referral result；beacon 使用统一 code source |
| `app/db/billing.py` | 新建正式账号时透传可信 expected creator；现有账号不应用 |
| `app/products/zhaoxi/api/creator_role_templates.py` | 创建者 API + public preview |
| `app/products/zhaoxi/api/admin_creator_role_templates.py` | Admin/Staff 查看和处置 |
| `app/products/zhaoxi/manifest.py` | 挂载两个新 router |
| `app/static/dashboard.html` | 入口 |
| `app/static/creator_role_templates.html` | 用户角色模板管理、无试玩说明和真实新账号测试指引 |
| `app/static/campaign_codes_admin.html` | 增加“用户角色模板链接”视图，复用现有漏斗 UI，只允许查看/处置 |
| `app/static/home.html` / `onboarding.html` | preview、失效提示、参数续传回归 |
| `deploy/nginx/ai4company.top.conf` | 新用户页面白名单 |
| `app/config.py` / `.env.example` | `CREATOR_ROLE_TEMPLATES_ENABLED` 开关及注释 |
| `app/db/lifecycle.py` / `account_deletion.py` | invitee wipe 与 creator 注销规则 |

不要在根兼容模块新增业务实现；新能力保持在 `app/products/zhaoxi/`。

## 10. 配置、可观测与隐私

### 10.1 配置

新增 `CREATOR_ROLE_TEMPLATES_ENABLED=false`，上线时先迁移和部署后端，再灰度开启。开关关闭时：

- 不展示创建入口，创建/编辑/发布 API 返回 capability disabled；
- 新 `urt_*` 注册不实例化模板，保留有效 referral 并回退普通 onboarding；
- 已归因账号继续读自己的 profile，不受开关影响。

审核使用现有 moderation task tier/provider，不新增密钥，不硬编码模型。

### 10.2 结构化日志与指标

至少记录：

- `creator_role_template.created/review_passed/review_rejected/review_error/activated/disabled/deleted`
- `creator_role_template.preview_invalid`（reason，不记录 invite code 全文）
- `creator_role_template.attribution_applied/attribution_skipped/attribution_error`
- LLM review latency、error rate、pass/reject rate
- owner mismatch、expired、slot limit、并发冲突次数
- profile transaction rollback 次数

日志不写四字段、公开简介全文、手机号、session token、完整邀请码或完整活动码；可写 role/version id 和 code 的短 hash。

### 10.3 隐私与授权

- 创建者统计只有聚合值；不得返回 invitee 列表或账号 ID。
- owner API 一律先按 principal owner 查询，非 owner 404，避免枚举。
- public preview 只返回 published version 的限长公开摘要。
- rejected/pending 内容只对 owner 和 Admin/Staff 可见，不进入 prompt 或公开 preview。
- account 快照按 `account_id` 读取；任何不带 account_id 的运行时 profile 查询视为 bug。

## 11. 兼容性与风险控制

| 风险 | 控制 |
| --- | --- |
| 用户 code 与运营 code 冲突 | `urt_` 保留前缀 + migration preflight + Admin 创建校验 |
| 两个 code 被手工错拼 | referral 返回可信 inviter；模板实例化事务强制 owner 一致 |
| 并发突破 3 个模板 | slot 1..3 部分唯一索引 + savepoint 重试 |
| LLM 不可用时未审先发 | pending/reviewing CAS，任何异常 fail-closed |
| 编辑污染线上角色 | immutable version；审核通过才原子切 active |
| 编辑延长 360 天 | activated/expires 只在 NULL 时首次写，后续不可更新 |
| 模板实例化归因只写一半 | attribution、三个 profile 文件、used_count 同一 DB 事务 |
| 使命重复 | creator attribution 抑制 `account_mission` 默认分配和 backfill |
| 老用户或创建者当前 AI 被覆盖 | 仅 `is_new_membership` + 真正新建默认账号时实例化；无 self-apply API |
| 为试玩预埋代码反而增加复杂度 | P0 不建 trial 表/API/flag/scheduler，不修改 Runtime/Dreaming/计费；V2 重新立项 |
| 创建者误以为可在线调试 | 页面明确无试玩，并说明须用全新账号通过发布链接做真实测试 |
| 创建者看到用户隐私 | 只复用聚合 funnel；owner-scoped stats；无明细 endpoint |
| 运营活码回归 | 独立表和保留前缀；非 `urt_` 走现有函数与测试 |
| 回滚破坏已有角色 | feature flag 只关新创建/新归因；已写 profile 快照继续运行 |

## 12. 测试方案

### 12.1 数据与领域测试

新增建议：`tests/test_creator_role_templates.py`、`tests/test_creator_role_template_review.py`。

- migration 59/60 SQLite/PG 建表、增列、索引、幂等与 reserved prefix preflight；
- slot 1–3 分配、第四个拒绝、并发创建不突破、soft delete 释放；
- 非 owner CRUD 404、跨 app 拒绝；
- 字段空值/长度/换行/额外字段；
- v1 pass/reject/error，重试 claim 幂等；
- active 模板编辑期间旧 published version 可用；审核通过只生成 approved candidate，主动发布才切版；
- 首次主动发布精确 +360 天，发布新版/编辑/启停不续期，边界 `now == expires_at` 失效；
- creator/admin disable 权限和恢复规则；
- code 生成熵、前缀、冲突重试，Admin 禁止保留前缀。

### 12.2 注册与 Onboarding 集成

扩展 `tests/test_web_onboarding.py`，新增 `tests/test_web_creator_role_template_links.py`：

- 有效 invite + 同 owner role：只建一条 referral，通用/角色 attribution 各一行，used_count +1；
- IDENTITY/SOUL/MISSION 与 version snapshot 一致，三个文件和 attribution 原子；
- onboarding 只问用户称呼，然后 complete；名字/性格不被 extraction 覆盖；
- complete 后无 `account_mission`，mission tools 不出现，MISSION prose 仍在 prompt；
- 创建者创建/编辑/发布模板前后，其当前 AI 的 profile、mission、session、memory、关系状态逐项不变；
- 创建者自点模板邀请链接不实例化、不覆盖当前 AI；
- 过期/停用/删除 role：referral 保留、无角色 attribution、普通 onboarding；
- owner mismatch：referral 归邀请码 owner、角色不应用、统计不计 role 注册；
- 已有朝夕成员登录不消费、不覆盖、不增 used_count；
- 同 account 并发/重试只归因一次；
- profile 事务中间故障整笔回滚；
- 运营 campaign code 全部既有行为不变；Native/Companion World 不消费 `urt_*`。

### 12.3 API、前端与统计

- 创建者 list/detail/stats 全部 owner-scoped；
- 现有运营活码后台创建/编辑/启停无回归；用户角色模板视图不出现内容编辑或代创建按钮；
- preview 限流、字段最小化、失效和 mismatch reason；
- 创建者统计与 Admin 对同 code、同日期区间返回完全相同的 `totals/rates/by_day`；
- 默认 14 天、最大 92 天；
- dashboard 入口、模板页四字段、默认/公开简介、一次性简介编辑、审核/发布状态、复制链接；
- 页面没有试玩/调试/切换入口，并展示“发布后使用全新账号通过链接测试”的说明；静态/API 路由断言不存在
  `creator-role-template-trials` 或等价隐藏入口；
- home/onboarding 两参数跨刷新/OTP/登录/扫码不丢；失效提示并清除 template code；
- 所有角色文本通过 `textContent`，静态断言不出现危险 innerHTML 拼接；
- Nginx 配置存在 `/user/creator-role-templates.html` location。

### 12.4 建议执行命令

```bash
.venv/bin/pytest \
  tests/test_creator_role_templates.py \
  tests/test_creator_role_template_review.py \
  tests/test_web_creator_role_template_links.py \
  tests/test_web_onboarding.py \
  tests/test_db_campaign.py \
  tests/test_db_campaign_analytics.py -q

```

改动触及 schema、注册、referral、prompt/tool 和账号 wipe，合并前还必须执行完整 PG 回归。

## 13. 实施切分与发布

### Phase A：数据、审核与领域基座（已完成）

- migration 59/60、code namespace、slot/版本/review/summary review/attribution/event persistence；
- LLM reviewer 和纯 profile renderer；
- 全部领域测试；feature flag 保持关闭。

### Phase B：注册与 Runtime（已完成）

- referral result 透传、campaign dispatcher、角色原子快照；
- onboarding override、非量化使命抑制、wipe/注销；
- 注册、prompt、tool、并发集成测试。

### Phase C：模板 API 与前端

- 创建者/Admin API、审核后主动发布、preview、统计 owner guard；
- dashboard/角色页/落地页和 Nginx；
- API/静态页面/文档测试。

### Phase D：灰度

1. 备份 PG，执行 migration 59 preflight，并依次完成 migration 59/60；
2. 部署后端，开关关闭，跑运营活码/邀请码/普通 onboarding smoke；
3. 部署静态页和 Nginx，`nginx -t`；
4. 给内部账号开启模板主流程，真实创建→发布→复制链接→全新账号注册→onboarding；
5. 核对 referral 奖励链、角色漏斗、LLM review、角色效果和账号隔离；
6. 扩大开放范围。

回滚时关闭 `CREATOR_ROLE_TEMPLATES_ENABLED`，停止新模板创建和新模板实例化归因；不回滚 migration、不删除表、
不改已有正式账号 profile。运营活码、个人邀请码和普通 onboarding 应保持可用。

## 14. 完成定义

- PRD §15 所有验收点有自动化测试或明确的生产 smoke 记录；
- SQLite/PG 迁移、并发 slot、注册双归因和 snapshot 原子性通过；
- LLM 审核不可用时没有未审内容进入 preview/prompt；
- creator role template 账号不出现量化使命表行或使命工具；
- 创建者当前 AI 不存在任何模板 apply/switch 路径，隔离回归通过；
- P0 没有试玩/调试/trial account 的表、API、配置、scheduler、Runtime 分支或隐藏页面入口；
- 页面明确提示发布后需使用全新账号通过注册链接真实测试；内部 smoke 已验证该账号走正式注册与 onboarding；
- 创建者与 Admin 同口径统计一致，且没有用户明细泄露；
- 运营 campaign、纯邀请码、普通 onboarding、Native/Companion World 回归通过；
- `.env.example`、Nginx、部署步骤和必要运维说明同步更新。
