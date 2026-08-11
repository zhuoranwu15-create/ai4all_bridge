# Plum 注册登录与用户创建角色开发准备

更新时间：2026-08-11
状态：P0 准备已完成；用于 2026-08-12 两项需求开工，P1 等待产品决策与正式 adapter

## 1. 目标与边界

本计划只为以下两项需求消除产品和工程歧义：

1. Plum 用户注册、登录、恢复会话和退出；
2. 已登录用户上传 Portrait，并创建首个可版本化 Character。

本期不继续扩展 Connection、Storyline、记忆或剧情编排；也不提前实现角色编辑、服务端草稿、人工审核队列、支付和社交能力。

## 2. 已冻结的共同不变量

- `platform_users` 是真人权利主体，`product_memberships(app_id='plum')` 是 Plum 产品资格；Runtime account 不是登录账号。
- 新用户口径以“首次创建 Plum Membership”为准，不以平台 User 是否首次出现为准。
- 所有用户私有查询和写入都必须携带 `platform_user_id`；跨 owner 的资源统一按不存在处理。
- Plum Web 会话继续使用 `plum` audience 的 HttpOnly cookie；所有 mutation 继续校验 double-submit CSRF。
- 注册登录不创建 Connection；创建 Character 也不创建 Persona、Connection、Storyline 或聊天会话。
- 钱包、新用户赠送和产品入口 Runtime 必须幂等，失败时不能签发一个无法完成 `/auth/me` 或 `/bootstrap` 的会话。
- 角色发布只允许服务端把“已经过可信审核的决定”交给发布事务，客户端不能提交或伪造 `moderation_decision_id`、平台最终分级和审核结果。

## 3. 注册登录契约

### 3.1 两层边界

```text
海外身份 provider / adapter
  -> 验证邮箱、手机号或 OAuth 凭证
  -> 原子解析或创建 Platform User
  -> 返回已验证 platform_user_id
  -> Plum 身份编排
       1. 创建或确认 active Plum Membership
       2. 幂等准备 Plum 入口 Runtime、钱包和首期产品资产
       3. 签发 audience=plum 的 cookie session
       4. 返回 is_new_membership、User、Session、Wallet
```

Plum 身份编排只接受服务端内部的“已验证 User”，不直接理解 Google、Apple、邮箱或短信 token。具体 provider 的验证与账号合并规则属于共享身份层，不能塞进 `app/products/plum/`。

### 3.2 稳定 HTTP 契约

以下接口可立即作为客户端稳定依赖：

| 接口 | 语义 |
| --- | --- |
| `GET /auth/me` | 恢复当前 `plum` 会话并返回 User、Session 到期时间和 Wallet |
| `DELETE /auth/session/current` | 撤销当前 session 并清理 session / CSRF cookie |
| `POST /auth/access-code` | 仅保留公测和本地测试入口；正式 provider 就绪后由配置关闭，不作为海外正式注册模型 |

正式登录发起/回调接口在 provider 拍板后补齐；无论采用 OAuth redirect 还是 verification token exchange，成功出口都必须调用同一个 Plum 身份编排，不能分别实现 Membership、钱包和 session。

### 3.3 错误语义

| HTTP | `detail` | 含义 |
| --- | --- | --- |
| 400 | `identity_verification_invalid` | provider 凭证无效、过期或已消费 |
| 401 | `authentication_required` / `session_expired` | 无有效 Plum 会话 |
| 403 | `csrf_validation_failed` | mutation 缺少匹配 CSRF |
| 403 | `product_membership_disabled` | Membership 被显式停用，不得在登录时静默恢复 |
| 409 | `identity_link_conflict` | provider 身份与既有 User 合并存在冲突 |
| 503 | `identity_provider_unavailable` | provider 暂时不可用，fail closed |
| 503 | `plum_user_provisioning_failed` | Membership 后的产品资产准备未完成，不签发 session |

### 3.4 注册登录验收矩阵

- 首次加入 Plum：创建 Membership 和产品资产，只赠送一次，返回 `is_new_user=true`。
- 平台已有 User 首次加入 Plum：同样返回 `is_new_user=true`，但不得创建第二个 Platform User。
- Plum 回流登录：复用 User、Membership、入口 Runtime 和 Wallet，返回 `is_new_user=false`。
- disabled User 或 Membership：拒绝登录且不得产生新资产或 session。
- 同一 provider 回调并发重放：只得到一个 User/Membership，凭证消费和账号解析必须原子。
- 朝夕/鸣蝉 session 或 Membership 不能访问 Plum；Plum session 也不能访问其他产品 audience。
- mutation 缺失/错误 CSRF 均为 403；GET 恢复会话不要求 CSRF。

## 4. 用户创建 Character 契约

### 4.1 V1 请求

`POST /creator/characters` 使用完整提交，不建立服务端草稿。请求包含：

- `idempotency_key`：8–128 字符，同一 User 内唯一；相同 key + 相同内容返回原结果，不同内容返回 409；
- `display_name`、`gender`、`intro`、`opening_scene`、`character_settings`；
- 可选 `example_dialogues`、`response_rules`；`scenario_prompt` V1 固定为空；
- `portrait_media_id` 及 Portrait / Avatar 四个 `0..100` 焦点坐标；
- 1–5 个稳定 `tag_id`；
- `creator_declared_rating` 与 `visibility=private|public`。

客户端不得提交 Work owner、Creator Profile owner、`platform_effective_rating`、`access_policy_version`、审核决定、版本号、热度、能力或 Runtime Prompt 投影版本。

建议 V1 长度上限：名称 40、Intro 500、Opening Scene 2,000、Character Settings 12,000、Example Dialogues 6,000、Response Rules 3,000 字符。API contract 与审核 adapter 必须使用同一份规范化内容，避免“审核文本”和“实际发布文本”不同。

### 4.2 服务端流程与事务

```text
认证 + CSRF
  -> 按 owner 校验 pending Portrait、字段和 active Tags
  -> 校验创作合规确认与发布资格
  -> 对规范化完整内容执行同步审核
  -> 审核拒绝：不写 Work / Character / Version，不认领 media
  -> 审核通过：单事务
       1. 锁定同 User + idempotency_key
       2. get-or-create 该 User 的 V1 Creator Public Profile
       3. 创建一个 Work（V1 服务层仅含一个 Character）
       4. 创建 Character 当前投影和 Character Version 1
       5. 写入 Version Tags 和 Character Stats
       6. pending Portrait -> referenced
       7. 记录创建幂等结果
```

任何一步失败都必须整体回滚。Work 权限只看 `owner_platform_user_id`，`creator_profile_id` 只用于展示。新建 Character 的审核决定必须非空且来自服务端已验证结果；当前海外审核 provider 未落地时，不开放生产创建接口。

### 4.3 读取和可见性

| 接口 | 规则 |
| --- | --- |
| `GET /creator/tags` | 返回 active 受控 Tag，供创建页选择 |
| `GET /creator/characters` | 只返回当前 User 拥有的 Character，可包含 private / taken_down / archived |
| `GET /creator/characters/{id}` | 必须经 Work owner 校验；跨 owner 返回 404 |
| `GET /feed` | 只返回 `status=active AND visibility=public` 且满足访问策略的 Character |

Prompt、Settings、Examples 和审核详情不进入公共 DTO。Portrait 发布后需要独立的受权读取地址；不能把 owner-only `/creator/media/{id}` 当作公共图片 URL。

### 4.4 错误语义

| HTTP | `detail` | 含义 |
| --- | --- | --- |
| 404 | `creator_media_not_found` / `character_not_found` | 不存在或不属于当前 User |
| 409 | `character_create_idempotency_conflict` | 同 key 被用于不同创建内容 |
| 409 | `creator_media_not_claimable` | media 已被其他正式内容引用或并发消费 |
| 422 | `character_payload_invalid` | 字段、焦点、Tag 数量或枚举不合法 |
| 422 | `character_tag_invalid` | Tag 不存在或已 disabled |
| 422 | `character_moderation_rejected` | 同步审核拒绝，不产生正式记录 |
| 503 | `character_moderation_unavailable` | 审核不可用，fail closed |

### 4.5 创建验收矩阵

- 正常创建后 Work、当前投影、Version 1、Tags、Stats、media 状态同时可见。
- 任一 SQL 或 media 认领失败时，上述记录全部不存在，media 仍为 pending。
- 同 owner 同 key 同内容重放返回同一个 Character；同 key 改内容返回 409。
- 两个 User 使用相同 key 互不影响；User B 不能使用、查看或认领 User A 的 media / Work / Character。
- disabled Tag、超过 5 个 Tag、重复 Tag、非图片 media、过期或 referenced media 均拒绝。
- private Character 不进入 Feed；public Character 只暴露消费者 DTO。
- 客户端提交审核字段或未知字段时直接 422。

## 5. 明天开工前必须拍板

以下四项不能由工程默认值替代：

1. **海外登录 provider**：V1 是邮箱验证码、Google、Apple，还是组合；同时确定账号合并、删除和 provider 数据区。
2. **首版 Tag 词表**：稳定 `tag_id/code/display_name`、排序和是否存在互斥；未给词表前不能把竞品文案写入生产 seed。
3. **角色审核 adapter**：正式文本/图片 provider、超时和降级规则、decision 的审计落点；不可用时默认 fail closed。
4. **发布图片读取**：签名 URL、受权媒体 endpoint 或 CDN 派生物，以及 private/public/taken_down 时的授权失效规则。

内容分级 V1 暂按现有 `general / mature` 代码和 `plum-rating-v1` 实施；在正式开放 public Character 前仍需补齐年龄、地区和合规资格规则。

## 6. 实施批次

- **P0（已完成）**：provider-neutral Plum 登录编排；角色创建契约；创建幂等记录与原子发布 repository；聚焦测试。
- **P1（明日）**：接入已选身份 provider；实现正式登录 HTTP；接入审核 adapter；挂载 Character Create / owner-only read API。
- **P2（生产启用前）**：Tag 生产 seed；公开图片授权；内容访问策略；账号删除/导出；限流、可观测性和生产 smoke test。

完成标准：P0/P1 的稳定结论回写 Plum 产品文档，计划完成后移入 `docs/archive/`。
