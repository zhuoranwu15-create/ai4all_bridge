# Plum 总数据模型设计

更新时间：2026-08-11
状态：领域骨架评审稿；迁移 72–76 已实现内容资产、Connection、Storyline / State、系统 Work ownership、唯一重开与 Character Create 幂等基础；记忆、Turn Context Snapshot 和自动升级尚未实施

> 本文把 Plum 的 User、公开身份、创作资产、Persona、Connection、Storyline、Conversation、
> Runtime 和用户资产串成一套逻辑数据模型。产品语义以
> [账号、身份与资产关系](identity-and-assets.md) 为准；Character 字段和发布流程以
> [Work / Character 数据模型设计](character_work_data_model.md) 为准。本文负责表之间的联系、
> 状态变化、数据迁移和生产约束，不重复维护 Character 的全部创作者字段。

## 1. 目标与边界

目标是让任何私有数据都能回答四个问题：

1. 属于哪个 User；
2. 使用哪个 Persona 和 Character；
3. 属于哪段 Connection 与 Storyline；
4. 在创建、升级、重开、下架和删除时如何处理。

本文定义目标模型，不授权立即执行生产迁移。V1 不新建 `plum_users`、独立钱包、独立消息表或
Persona Version；共享账号、Membership、权益、媒体和 Runtime 基础表继续复用平台实现。

当前代码已注册迁移 72–76；它们会在新建测试库中自动验证，但截至本文更新时间，尚未由本次开发
对真实本地 Plum 数据库或生产数据库执行。迁移 73 将普通开聊切到 Connection create-or-get，并将
重开切到新 Connection + 新 Runtime；迁移 74 为存量 Connection 建立初始 Storyline / State，回填
Conversation 归属；迁移 75 将平台内置 Work 从假真人 owner 改为显式 system owner，迁移 76 增加
owner-scoped Character Create 幂等结果。新建与重开事务也会
同步创建或归档 Storyline。旧 Relationship / Binding 仅作为迁移输入保留，新业务不再双写。

## 2. 已冻结的不变量

1. `platform_users` 是 User 权利主体，`product_memberships(app_id='plum')` 是 Plum 产品资格；
2. Public Profile 是公开展示身份，不是权限主体；创作权限最终解析到 User；
3. 一个 Work 可以包含多个 Character，一个 Character 必须且只能属于一个 Work；
4. Persona 在首次建立 Connection 前可修改，首次建立 Connection 的事务将其锁定；锁定后身份内容不可修改；
5. V1 不建立 Persona Version；复制 Persona 会产生新的 `persona_id`，不继承原 Connection；
6. Connection 是 Persona 与 Character 的关系聚合根；同一组合重开也创建新 Connection；
7. 同一组 Persona + Character 同时只能存在一段 active Connection，archived 历史 Connection 可以有多段；
8. Character 发布新版本后，所有 active Connection 自动采用最新生效版本，关系和记忆保留；
9. 重开归档旧 Connection，新 Connection 不继承旧关系、记忆、剧情、会话或 Runtime Context；
10. Storyline 承载剧情作用域，但 V1 不提供“保留 Connection、只重开 Storyline”的用户动作；
11. Character 下架后停止发现、新建和所有已有 Connection 的继续互动；
12. 收藏、屏蔽、举报、购买和永久访问权益属于 User + Character，不随 Persona 或 Connection 重开；
13. Runtime Account 是内部执行容器，必须归属于 Connection，不能成为产品所有权来源。

## 3. 总体关系

```text
platform_users (User)
├── product_memberships (Plum Membership)
├── subscriptions / entitlement_wallets / entitlement_ledger
├── media_assets
├── plum_public_profiles
│   └── plum_works
│       └── plum_characters
│           ├── plum_character_versions
│           ├── plum_character_version_tags -> plum_tags
│           └── badges / stats / comments / published moments
├── plum_user_personas
│   └── plum_connections
│       ├── Character current version + adoption history
│       ├── plum_connection_relationship_state
│       ├── plum_connection_memories
│       ├── plum_connection_runtime_bindings -> accounts / runtime_ownerships
│       └── plum_storylines
│           ├── plum_storyline_state
│           ├── plum_storyline_memories
│           └── plum_conversations -> sessions / messages
└── likes / favorites / blocks / reports / Character access rights
```

## 4. 表的权威归属

| 领域 | 权威表 | 说明 |
| --- | --- | --- |
| User 与登录 | `platform_users`、`platform_user_sessions` | 共享平台能力，Plum 不复制 User |
| Plum 资格 | `product_memberships` | 使用 `app_id='plum'` 隔离产品状态和偏好 |
| 钱包与权益 | `subscriptions`、`entitlement_wallets`、`entitlement_ledger` | User 级资产，不挂 Persona |
| 媒体 | `media_assets` | User owner、格式、审核和回收的共享权威来源 |
| 公开身份 | `plum_public_profiles` | 对外展示；不能单独用于鉴权 |
| 创作所有权 | `plum_works` | Work owner 是创作权限根 |
| Character 当前内容 | `plum_characters` | 当前生效查询投影，不是历史唯一真相 |
| Character 历史 | `plum_character_versions` | 不可变批准版本 |
| Persona | `plum_user_personas` | 锁定后不可修改的故事身份；无版本表 |
| 关系连续性 | `plum_connections` | Persona–Character 关系聚合根 |
| 剧情连续性 | `plum_storylines` | Connection 下的剧情作用域 |
| 会话入口 | `plum_conversations` | Plum 业务会话，映射共享 Runtime Session |
| 消息与 Turn | `messages`、`runtime_turn_runs` | 共享 Runtime 记录；Plum 另存 Context 版本快照 |
| Runtime 归属 | `plum_connection_runtime_bindings`、`runtime_ownerships` | Connection 到内部 Account 的映射 |

## 5. User、Membership 与 Public Profile

### 5.1 复用共享 User

所有真人拥有的 Plum 私有资产最终使用 `platform_user_id` 引用 `platform_users.id`。API 不得从 Persona、
Public Profile、Runtime Account 或客户端参数反推当前 User；必须使用已认证 Principal。平台内置内容不
伪造真人 User，必须通过资源自身的 `owner_kind='system'` 显式表达。

`product_memberships` 继续承载 Plum 状态、合规确认和全局偏好。年龄、地区、内容访问资格和安全控制
属于 User / Membership，不得放入 Persona。

### 5.2 `plum_public_profiles`

现有表继续使用。`platform_user_id` 是实际 owner，`id` 是评论、创作者页和发布内容使用的展示身份。
V1 暂时维持一个 User 最多一个 active Public Profile；未来允许多个时，Work 鉴权仍以
`owner_platform_user_id` 为准。

系统或平台官方 Profile 可以没有普通 User owner，但必须使用显式的 system ownership，不能用空 owner
绕过权限判断。

## 6. Work 与 Character

### 6.1 Work 所有权

`plum_works` 是创作所有权和生命周期根：

```text
plum_works
- id                         PK
- owner_kind                 platform_user / system
- owner_platform_user_id     nullable FK -> platform_users.id
- creator_profile_id         FK -> plum_public_profiles.id
- lifecycle_status           active / archived
- created_at / updated_at    TIMESTAMPTZ
```

一个 Character 只能通过非空 `work_id` 属于一个 Work。真人 Work 必须使用
`owner_kind='platform_user' + 非空 owner_platform_user_id`；平台内置 Work 必须使用
`owner_kind='system' + 空 owner_platform_user_id`。两种组合由数据库约束，系统主体不进入
`platform_users`，因此不会污染注册用户统计。

### 6.2 Character 当前投影与历史版本

以下表沿用专项设计：

- `plum_characters`：当前生效资料、当前 Runtime Prompt、状态和兼容投影；
- `plum_character_versions`：不可变创作者输入及审核结果；
- `plum_tags`、`plum_character_version_tags`：受控标签及版本关系。

当前 Character 必须能通过 `(id, content_version)` 解析到一个批准版本。新实现应使用稳定 Version ID，
或建立可验证的复合外键，不能只依赖应用代码约定。

创作者声明分级、平台最终有效分级、访问策略版本和审核决定必须随 Character Version 保存。
`content_rating` 只作为旧消费者的兼容投影。

## 7. `plum_user_personas`

### 7.1 目标字段

| 字段 | 约束 | 含义 |
| --- | --- | --- |
| `id` | PK | Persona ID |
| `platform_user_id` | FK，必填 | Persona owner，也是账号隔离锚 |
| `display_name` | 必填 | 故事中的名称 |
| `avatar_media_id` | FK，可空 | Persona 头像媒体来源 |
| `avatar_ref` | 兼容，可空 | 现有消费者展示引用 |
| `description` | 必填，默认空 | 用户可见的身份说明 |
| `prompt_text` | 必填，默认空 | 注入模型的故事身份内容 |
| `status` | `active / inactive / deleted` | 生命周期；不表示是否锁定 |
| `is_default` | Boolean | User 默认 Persona |
| `locked_at` | 可空 | 非空表示身份内容永久锁定 |
| `derived_from_persona_id` | 自引用，可空 | 由哪个 Persona 复制而来；不表示继承关系 |
| `created_at / updated_at` | TIMESTAMPTZ | 创建和最后生命周期更新时间 |

### 7.2 锁定规则

- `locked_at IS NULL` 且尚未被 Connection 引用时，允许更新身份内容；
- 建立首个 Connection 的同一事务先锁定 Persona，再创建 Connection；
- `locked_at IS NOT NULL` 后，`display_name`、头像、`description` 和 `prompt_text` 均不可更新；
- 锁定后只允许修改 `status` 等生命周期字段；
- “复制并编辑”创建全新 Persona，`derived_from_persona_id` 仅用于产品追踪；
- 已有关联的 Persona 删除默认先停用；硬删除必须与其 Connection 一起进入 User 数据删除流程。

现有 `version` 列迁移期固定为 `1`，不再递增；旧读写清理完成后再单独评估删除，不在首个迁移中
破坏现有结构。

## 8. `plum_connections`

### 8.1 表定位

Connection 是一段独立关系的权威根，不再使用 `platform_user_id + character_id` 表示唯一关系。

| 字段 | 约束 | 含义 |
| --- | --- | --- |
| `id` | PK | Connection ID |
| `platform_user_id` | FK，必填 | owner 和账号隔离锚 |
| `persona_id` | FK，必填 | 已锁定 Persona |
| `character_id` | FK，必填 | Character 资产 |
| `current_character_version` | 正整数，必填 | 当前采用的 Character Content Version |
| `status` | `active / archived / blocked` | Connection 生命周期 |
| `blocked_reason` | 可空 | `character_taken_down / access_denied / policy` 等 |
| `creation_reason` | `initial / restart / backfill` | Connection 的创建原因 |
| `creation_idempotency_key` | User 内唯一 | 防止创建或重开请求重复生成 Connection |
| `replaces_connection_id` | 自引用，可空 | 重开时被替换的旧 Connection；仅用于审计，不允许读取旧 Context |
| `migration_source` | 可空 | 存量迁移来源；新业务写入为空 |
| `created_at / updated_at` | TIMESTAMPTZ | 生命周期时间 |
| `archived_at` | 可空 | 重开或删除时退出 active 的时间 |

数据库或服务层必须验证 `persona_id` 的 owner 等于 `platform_user_id`。推荐增加 Persona 的
`UNIQUE(id, platform_user_id)`，再由 Connection 使用复合外键，避免仅靠调用方自律。

同一 Persona + Character 同时只能有一段 active Connection，数据库必须使用 partial unique index 防止
并发请求突破产品规则：

```sql
CREATE UNIQUE INDEX ux_plum_connections_active_persona_character
ON plum_connections(persona_id, character_id)
WHERE status = 'active';

CREATE UNIQUE INDEX ux_plum_connections_user_idempotency
ON plum_connections(platform_user_id, creation_idempotency_key)
WHERE creation_idempotency_key IS NOT NULL;
```

普通“开始聊天”是 create-or-get：已有 active Connection 时恢复它。只有显式重开可以在同一事务中
归档旧 Connection 并创建新 ID。`creation_idempotency_key` 防止用户双击或客户端重试连续重开两次；
同一 key 必须返回同一条新 Connection。

### 8.2 `plum_connection_character_adoptions`

记录 Connection 自动跟随 Character 升级的过程：

| 字段 | 含义 |
| --- | --- |
| `id` | Adoption 事件 ID |
| `connection_id` | 所属 Connection |
| `from_content_version / to_content_version` | 升级前后内容版本 |
| `from_prompt_version / to_prompt_version` | 升级前后 Runtime Prompt 版本 |
| `trigger` | `created / character_published / backfill` |
| `status` | `pending / applied / failed` |
| `failure_code` | 刷新失败原因，可空 |
| `created_at / applied_at` | 审计时间 |

需要刷新 Prompt 时，Adoption、Runtime Binding 和 Connection 当前版本必须在一次受控切换中保持一致。
失败时 Connection 继续指向旧版本且不得生成混合版本回复；访问资格失效或 Character 下架则直接阻断，
不能回退旧版本继续。

### 8.3 `plum_connection_relationship_state`

一条 Connection 一行，承接现有关系字段：

```text
connection_id          PK / FK
state                  connected / paused / ended
relationship_level     >= 0
relationship_xp        >= 0
completed_turn_count   >= 0
first_connected_at
last_interacted_at
updated_at
```

未来新增称呼、边界或里程碑时，应定义明确结构或独立表，不把多种未知语义提前塞进无约束 JSON。

### 8.4 `plum_connection_memories`

保存跨 Storyline 仍成立的共享关系记忆：

```text
id                     PK
connection_id          FK
kind                   relationship / boundary / milestone
summary                私有记忆内容
source_turn_run_id     可空，来源 Turn
status                 active / superseded / deleted
created_at / updated_at
```

任何查询必须经 Connection 校验 `platform_user_id`；不得只凭 Memory ID 读取。剧情局部事实不能直接写入
本表，提升为 Connection Memory 的产品机制后续单独冻结。

## 9. Storyline、Conversation 与消息

### 9.1 `plum_storylines`

| 字段 | 约束 | 含义 |
| --- | --- | --- |
| `id` | PK | Storyline ID |
| `platform_user_id` | 复合 FK，必填 | owner 隔离投影，必须与 Connection 一致 |
| `connection_id` | FK，必填 | 所属 Connection |
| `character_id` | 复合 FK，必填 | Character 隔离投影，必须与 Connection 一致 |
| `ordinal` | 正整数 | Connection 内顺序 |
| `opening_character_version` | 正整数 | 创建该 Storyline 时采用的 Character Version |
| `status` | `active / archived` | Storyline 生命周期 |
| `created_at / updated_at / archived_at` | TIMESTAMPTZ | 生命周期时间 |

每个新 Connection 创建一条初始 Storyline。V1 没有“只重开 Storyline”的用户入口；表仍独立存在，
用于隔离剧情状态、未来篇章演进和一条 Storyline 跨多次 Conversation 的能力。

### 9.2 `plum_storyline_state`

一条 Storyline 一行：

```text
storyline_id            PK / FK
platform_user_id        owner 隔离投影，与 Storyline 一致
connection_id           Connection 隔离投影，与 Storyline 一致
current_chapter_no      >= 1
state_schema_version    正整数
plot_state_json         JSONB，必须符合对应 schema version
updated_at
```

`plot_state_json` 只保存明确属于 Storyline 的剧情变量，不保存 User、Persona、Character Prompt、关系状态
或跨剧情记忆。

### 9.3 `plum_storyline_memories`

结构与 Connection Memory 类似，但 owner 是 `storyline_id`，只能进入当前 Storyline 的 Context。重开新
Connection 时不得复制或读取旧 Storyline Memory。

### 9.4 `plum_conversations`

现有表继续作为 Plum 会话投影，并新增：

```text
connection_id           FK -> plum_connections.id
storyline_id            FK -> plum_storylines.id
```

迁移 73 已增加、回填并收紧必填 `connection_id`；迁移 74 已增加、回填并收紧必填 `storyline_id`，并以
`storyline_id + platform_user_id + connection_id + character_id` 复合外键防止跨 User、Connection 或
Character 串线。active Conversation 唯一约束现以 `connection_id` 为键，不再以 User + Character 为键，
因此同一 User 使用不同 Persona 时可以与同一 Character 分别保持 active Connection。

迁移期保留 `platform_user_id` 和 `character_id` 作为隔离及查询投影，但权威关系从 Connection 解析。
`runtime_session_id` 继续映射共享 `sessions`，消息继续保存在共享 `messages`，不新建 Plum 消息副本。

一条 Storyline 可以有多次已归档 Conversation，但 V1 同时只允许一条 active Conversation。

## 10. Runtime 与 Turn 审计

### 10.1 `plum_connection_runtime_bindings`

替换现有以 User + Character 为主键的 `plum_character_bindings`：

| 字段 | 约束 | 含义 |
| --- | --- | --- |
| `connection_id` | PK / FK | 一条 Connection 一个 active Binding |
| `platform_user_id` | 隔离投影 / 复合 FK | 同时校验 Connection owner 与 Runtime owner |
| `runtime_account_id` | UNIQUE / FK | 内部 Runtime Account |
| `applied_content_version` | 正整数 | Runtime 已采用的 Character Content Version |
| `applied_prompt_version` | 正整数 | Runtime 已编译的 Prompt Version |
| `status` | `active / inactive` | 重开后旧 Binding 失效 |
| `created_at / updated_at` | TIMESTAMPTZ | 生命周期时间 |

`runtime_ownerships` 使用：

```text
app_id      = plum
source_type = plum_connection
source_id   = connection_id
```

重开必须创建新 Connection 和新 Runtime Binding；不得让新 Connection 复用旧 Runtime Account、Context
Files、Session 或长期记忆。

重开后的旧 Runtime Account、Ownership、Context Files、Session 和 Message 暂时保留用于历史审计；重开
事务同步把旧 Account 置为 `disabled`、Ownership 与 Connection Runtime Binding 置为 `inactive`，确保旧
Runtime 不再可执行。正式上线前仍须冻结用户可见历史、保留期限和延迟物理清理策略，不在重开事务中直接
删除历史数据。

### 10.2 `plum_turn_context_snapshots`

每次实际生成保存一条轻量版本快照：

```text
runtime_turn_run_id             PK / FK -> runtime_turn_runs.id
platform_user_id
connection_id
storyline_id
conversation_id
persona_id
character_id
character_content_version
character_prompt_version
access_policy_version
created_at
```

Persona 锁定后不需要 Persona Version；`persona_id` 可以唯一定位生成时的身份内容。该表用于问题追踪、
升级审计和安全复核，不复制完整 Prompt、私有记忆或消息正文。

## 11. User–Character 资产与公开内容

以下资产继续属于 User + Character，不迁入 Connection：

- `plum_character_likes`；
- `plum_character_favorites`；
- 后续 Character block、report 和 access grant；
- 钱包、购买和永久访问权益的共享平台记录。

`plum_character_stats`、热度和计数是可重建投影，不是关系事实。`plum_character_comments` 使用 Public
Profile 展示作者，但删除、封禁和权限必须仍能解析到 User。

现有 `plum_character_memories` 表达可发布的互动内容或回忆卡，不得作为 LLM 私有记忆来源。后续建议
改名为 `plum_published_moments`；在重命名完成前，代码和文档必须明确区分它与
`plum_connection_memories`、`plum_storyline_memories`。

## 12. 核心事务机制

### 12.1 首次建立 Connection

```text
校验 User / Membership / Character / 分级访问资格
→ 校验 Persona owner 且 status=active
→ 若 Persona 未锁定，在同一事务写 locked_at
→ 创建 Connection 和初始 Character Adoption
→ 创建 Relationship State
→ 创建初始 Storyline 和 Storyline State
→ 创建 Runtime Account、Ownership 和 Connection Binding
→ 创建 active Conversation / Runtime Session
```

任何一步失败都不允许留下已锁定但无 Connection 的半完成状态。

创建前先查询该 Persona + Character 的 active Connection；已存在时直接恢复。并发首次创建以 partial
unique index 为最终防线，冲突方重新读取并返回胜出的 active Connection，不能把唯一冲突暴露成随机
500 错误。

### 12.2 每次生成 Turn

```text
按已认证 User 读取 active Conversation
→ 解析 Storyline / Connection / Persona / Character
→ 检查 Character 状态、平台有效分级和访问策略
→ 检查 Character 最新版本
→ 必要时原子完成 Adoption 与 Runtime 刷新
→ 组装 Persona → Character → Connection → Storyline → Conversation Context
→ 创建 runtime_turn_run 与 Plum Context Snapshot
→ 生成、落消息、更新关系和记忆
```

### 12.3 唯一重开

```text
按 creation_idempotency_key 查找已完成结果，存在则直接返回
→ 锁定当前 active Connection
→ 归档旧 Conversation / Storyline / Connection
→ 关闭旧 Runtime Session 和 Binding
→ 使用选定 Persona 与 Character 创建全新 Connection
→ 写 replaces_connection_id 和相同 idempotency key
→ 新建 Storyline、Runtime Account、Session 和 Conversation
→ 不复制旧关系、记忆、剧情或 Context Files
```

归档旧 Connection 和插入新 Connection 必须在同一数据库事务中完成，并由 partial unique index 防止
出现两个 active Connection。重开不改变 User 级收藏、屏蔽、举报、购买和永久访问权益。

### 12.4 Character 升级

发布新 Character Version 后，不批量同步重建所有 Runtime。Connection 在下一次生成前检查并采用新版本；
需要改变 Prompt 时先刷新该 Connection 的 Runtime，成功后再切换当前版本。这样避免发布事务扫描所有
消费者，同时保证用户下一次回复使用最新版本。

### 12.5 Character 下架

下架事务更新 Character 审核状态；所有发现、详情、开聊和 Turn 入口实时检查该状态。已有 Connection
转为 `blocked` 可以由同步写入或幂等后台任务完成，但生成入口不能依赖后台任务完成后才阻断。

## 13. 删除与保留

| 动作 | 直接影响 | 不应影响 |
| --- | --- | --- |
| 停用 Persona | 不再用于新 Connection | 已有历史和 User 级资产 |
| 删除 Persona | 按确认范围删除或归档关联 Connection | 钱包、收藏、购买 |
| 重开 Connection | 旧 Connection 归档，新 Connection 空白开始 | User 级资产 |
| 删除 Storyline | 当前剧情状态、剧情记忆和会话 | Connection 之外的资产 |
| Character 下架 | 发现、新建和已有 Connection 生成 | 用户依法可保留的历史副本 |
| 删除 User | 所有 Plum 私有数据、公开身份和授权进入完整生命周期流程 | 法律要求保留的最小财务审计数据 |

User 删除必须以 `platform_user_id` 编排，不能只调用某个 Runtime `account_id` 的清理函数。创作者注销、
Work 所有权转移、Character 历史版本保留和消费者历史处理需要单独生命周期方案。

`plum_character_versions` 当前由数据库触发器禁止 UPDATE 和 DELETE。User 删除专项必须提供受控的物理删除
通道及完整依赖删除顺序；不得为了方便普通业务删除而全局放开 Version DELETE。

## 14. 现有表迁移映射

| 现有结构 | 目标结构 | 迁移原则 |
| --- | --- | --- |
| `plum_user_personas.version` | 不再表示版本 | 固定为 1，增加 `locked_at` 和复制来源 |
| `plum_user_character_relationships` | Connection + Relationship State | 每组现有 User + Character 建立一条迁移 Connection；迁移后停止新业务写入 |
| `plum_character_bindings` | Connection Runtime Binding | Runtime owner source 改为 Connection；迁移后停止新业务写入 |
| `plum_conversations` | 增加 Connection / Storyline FK | 保留现有 ID 和 Runtime Session 映射 |
| `current_chapter_no` | Storyline State | 回填到初始 Storyline |
| `plum_character_memories` | Published Moment 语义 | 不迁入私有 Runtime Memory |
| `plum_characters` | 当前投影 | 建立 Work 和 Character Version 1 后继续使用 |

历史数据无法知道用户当时使用哪个 Persona，因为旧关系表只有 User + Character。迁移时使用该 User 的
当前默认 Persona；没有默认 Persona 时创建迁移专用 Persona，并在建立 Connection 时锁定。迁移结果必须
记录 `migration_source`，不能根据聊天文案猜测 Persona。

迁移 73 首次新建 `plum_connections` 空表，因此建立 active 唯一索引时不存在目标表存量重复问题。
若未来从外部导入 Connection，或恢复一个已提前创建的目标表，则必须先扫描
`(persona_id, character_id)` 重复 active 数据：保留 `updated_at` 最新的一条为 active，其余记录归档并
标记 `migration_duplicate`；不得静默删除关系、会话或记忆。验证重复数归零后再创建索引。

## 15. 实施顺序

1. 已实现：新增 Work、Character Version、分级和审核字段，回填现有 Character；
2. 已实现：改造 Persona 字段，增加永久锁定保护并停止递增旧 `version`；
3. 已实现：新增 Connection、Adoption、Relationship State 和 Runtime Binding；
4. 已部分实现：Storyline、State 与 Conversation FK 已创建并回填；Storyline Memory 尚未实施；
5. 待实施：Turn Context Snapshot / Storyline Context 组装、Character 自动升级与访问资格矩阵；
6. 已部分实现：唯一重开及 Character 实时下架阻断；User 级删除编排尚未实施；
7. 已切换：新路径只写 Connection 权威模型；旧 Relationship / Binding 仅作为迁移输入保留，清理延后。

重开 API 要求客户端提供 8–128 字符的 `idempotency_key`。相同 User、旧 Connection 与 key 的重试
返回同一条新 Conversation；同一个 key 用于另一重开操作返回冲突。

每个实施 PR 都必须包含 Migration、回填验证、Repository/Service 变更、聚焦 PostgreSQL 测试和回滚方案。
迁移采用“加表/加列 → 回填 → 双写 → 校验 → 切读 → 收紧约束 → 延后清理”，禁止一次性破坏旧表。

对任何真实数据库执行迁移 72–76 前，必须先为带存量回填的迁移 72–75 运行只读预检：

```bash
.venv/bin/python scripts/precheck_plum_data_model.py --pg "$DATABASE_URL"
```

预检只输出表级盘点和异常聚合计数，不输出用户或 Runtime 明细；`result=BLOCK` 时不得进入迁移窗口。

### 15.1 生产迁移与回退

迁移 72–76 与新 Repository 必须作为同一发布单元：先停止旧 Plum 写流量并确认没有旧 worker，再备份
数据库、执行迁移和回填校验，最后部署新代码及要求 `idempotency_key` 的客户端。禁止让迁移前后的 Plum
写实例长期混跑；旧实例不知道 `connection_id` / `storyline_id`，也不能表达多 Persona Connection。

迁移在单一 PostgreSQL 事务内执行，回填、owner 校验或约束收紧失败时整体回滚。迁移提交但尚未恢复流量时
若应用验收失败，优先修正并前向发布；必须回退 schema 时只能使用已演练的数据库快照/反向迁移，并同时
回退客户端契约。不得临时删除 Connection 表或把新 Connection 的 Runtime 重新指回旧 Binding。

### 15.2 Connection 聚焦验收

- 并发为同一 Persona + Character 开聊，最终只产生一段 active Connection；
- 重复普通开聊返回已有 active Connection，不创建新记录；
- 同一 Persona 与不同 Character 可以分别 active；
- 不同 Persona 与同一 Character 可以分别 active；
- 显式重开后旧 Connection 为 archived，新 Connection 为唯一 active，且没有旧 Context；
- 相同 `creation_idempotency_key` 重试重开返回同一条新 Connection；
- archived 历史 Connection 数量不受 active 唯一索引限制；
- Character 下架或访问资格不足时不能通过新建 Connection 绕过 blocked 状态。

## 16. 尚待冻结

- Public Profile 是否允许一个 User 拥有多个；
- Character 从 public 改为 private 或创作者主动 archived 后，已有 Connection 是否继续；
- 创作者注销、Work 转移和 Character 所有权继承；
- 分级代码、年龄阈值、地区矩阵和访问策略版本格式；
- 哪些 Storyline Memory 可以提升为 Connection Memory，以及确认机制；
- 普通重开后旧 Connection 在用户界面中可见、可恢复还是仅保留到删除期限；
- Connection / Storyline 级付费内容和消耗品在重开后的处理。
