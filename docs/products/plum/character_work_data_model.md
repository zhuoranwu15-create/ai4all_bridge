# Plum Work / Character 数据模型设计

更新时间：2026-08-11  
状态：核心产品关系已冻结；迁移 72 已实施内容基础，迁移 76 与 Repository 已实施 Create 幂等和原子发布；正式审核/API 与 Edit 尚未实施

## 1. 目标与核心结论

Create V1 首期只允许一次创建一个角色，但数据库从第一天采用 `Work 1 ── N Character`：一个 Work
可以包含多个 Character，一个 Character 必须且只能属于一个 Work。若同一概念角色出现在不同 Work，
应创建不同的 `character_id`，不在 Work 之间复用同一 Character 资产。

本期采用以下原则：

1. `plum_works` 是作品和所有权容器；V1 不在其中提前放标题、封面、世界观或关系字段。
2. 继续扩展现有 `plum_characters`，不建立第二张角色主表。它保存当前生效的发布投影，供 Feed、聊天和 Runtime 快速读取。
3. `plum_character_versions` 保存每次审核通过的完整创作者输入快照；旧版本不可修改。
4. Create/Edit 提交先做同步机器审核。审核失败不改变正式内容；成功后在同一事务写版本、Tags 并更新当前投影。
5. `content_version` 记录完整角色内容版本；现有 `prompt_version` 只记录会影响 AI 回复的运行版本。
6. V1 不建立服务端草稿、人工审核队列、World、世界书、用户身份或多角色关系表。这些能力保留清晰扩展点，确认需求后再建模。
7. Character 发布新版本后，所有已有 Persona–Character Connection 自动采用新版本；Character 下架后已有 Connection 也不能继续互动。
8. 内容分级区分创作者声明、平台最终有效分级和版本化访问策略，旧 `content_rating` 只作为兼容投影。

## 2. 领域关系

```text
Platform User
  └── owns Plum Work
        └── contains one or more Character assets
              ├── has immutable approved Versions
              ├── has controlled Tags per Version
              └── binds independently to each consumer Runtime
```

- 一个 Work 可以拥有多个 Character 资产。
- 一个 Character 资产必须且只能属于一个 Work，数据库最终以非空 `work_id` 和外键保证该约束。
- 同一概念角色可以出现在不同 Work，但每个 Work 中是不同的 `character_id`，设定和聊天历史互不污染。
- 未来若需要跨作品复用“角色原型”，再建立独立 Character Template / Identity 资源；V1 不预建空字段。

## 3. `plum_works`

Work 是稳定的作品容器和权限边界。V1 服务层限制每个 Work 只有一个 Character，但数据库不增加唯一约束，以便未来直接支持多角色。

| 字段 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- |
| `id` | TEXT，主键 | Work 唯一 ID | `work_01JXYZ...` |
| `owner_kind` | `platform_user / system`，必填 | 区分真人创作者资产与平台内置资产 | `platform_user` |
| `owner_platform_user_id` | TEXT，真人 Work 必填、系统 Work 必须为空 | 真人资源所有者；Create/Edit/Archive 权限从这里判断 | `pusr_01J...` |
| `creator_profile_id` | TEXT，必填 | 对外展示的创作者身份；不参与权限判断 | `fprof_creator_plum` |
| `lifecycle_status` | `active / archived` | Work 自身生命周期，不等同于单个角色是否公开或被下架 | `active` |
| `created_at` | 时间，必填 | Work 创建时间 | `2026-08-11 14:00:00` |
| `updated_at` | 时间，必填 | Work 最后更新时间 | `2026-08-12 10:20:00` |

V1 明确不放入 Work 的字段：作品名称、作品封面、World、世界书、公共故事背景、故事前提、角色关系和主要角色。这些语义在多角色产品方案确定后再设计。

平台内置 Character 的 Work 使用 `owner_kind='system'`，不在 `platform_users` 中创建假真人；真人 Work
使用 `owner_kind='platform_user'`，且数据库要求非空 `owner_platform_user_id`。

## 4. `plum_characters`

### 4.1 表的定位

`plum_characters` 是“当前生效版本”的查询投影，不是创作者输入历史的唯一真相：

```text
当前公开资料
+ 当前媒体投影
+ 当前 Runtime Prompt
+ 当前生命周期与运营状态
```

现有 Feed 和 Chat 查询继续读取本表。Create/Edit 的原始内容与历史版本从 `plum_character_versions` 读取。

### 4.2 标识与归属

| 字段 | 状态 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- | --- |
| `id` | 现有 | TEXT，主键 | 角色资产 ID；聊天、收藏、评论等关系继续引用它 | `char_01JABC...` |
| `work_id` | 新增 | TEXT，最终必填，引用 `plum_works.id` | 角色所属作品；所有权从 Work 获取 | `work_01JXYZ...` |
| `creator_profile_id` | 现有兼容 | TEXT | 消费者接口当前读取的创作者身份投影，不得用于鉴权 | `fprof_creator_plum` |

迁移现有内置角色时，应先创建系统 Work 并回填 `work_id`，再收紧非空约束；不要通过角色名称猜测归属。

`plum_works.creator_profile_id` 是作品创作者身份的权威来源；发布时同步到现有 `plum_characters.creator_profile_id`，避免当前 Feed/Chat 查询立即跨表改造。两处不允许由不同接口分别修改。

### 4.3 公开角色信息

| 字段 | 状态 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- | --- |
| `display_name` | 现有 | TEXT，必填 | Create 页的 Name；也是 Runtime 身份名称 | `Luna` |
| `gender` | 新增 | `male / female / non_binary`；历史数据可暂时为空 | 用于筛选，也作为基础身份传给模型 | `female` |
| `intro` | 现有 | TEXT，必填 | Create 页的 Character Intro；公开展示 | `A guarded stargazer hiding an impossible secret.` |
| `tagline` | 现有兼容 | TEXT，必填 | 现有卡片和聊天接口使用；新版本发布时写入与 `intro` 相同的内容，由各页面自行截断 | 与 `intro` 相同 |
| `greeting` | 现有兼容 | TEXT，必填 | Create 页的 Opening Scene；仅用于初始化新聊天的第一条角色内容 | `*Luna closes the door.* “You came.”` |
| `tags_json` | 现有兼容 | JSON 文本数组 | 当前 Feed/Chat 的标签展示投影；由当前版本 Tags 生成，不是长期唯一数据源 | `["Romance","Fantasy"]` |
| `creator_declared_rating` | 新增 | 平台分级代码，必填 | 创作者对当前版本内容等级的声明，不直接决定访问权限 | `mature` |
| `platform_effective_rating` | 新增 | 平台分级代码，必填 | 审核后生效的最终内容等级，是 Feed 和访问判断的权威来源 | `mature` |
| `access_policy_version` | 新增 | TEXT，必填 | 计算年龄、地区和内容访问资格时使用的策略版本 | `plum-rating-v1` |
| `moderation_decision_id` | 新增 | TEXT；新发布必填，迁移历史可空 | 关联使当前版本生效的真实审核决定；历史内容不伪造审核记录 | `moddec_01J...` |
| `content_rating` | 现有兼容 | TEXT | `platform_effective_rating` 的兼容投影；不得再作为独立可写权威字段 | `mature` |
| `visibility` | 新增 | `public / private`，默认 `public` | Private 不进入公共 Feed、搜索或推荐，但所有者仍可编辑和聊天 | `public` |

Gender 的 V1 默认模型身份如下；不依赖用户在 Character Settings 中重复填写：

| `gender` | 默认英文身份 / 代词 |
| --- | --- |
| `male` | Male；he / him |
| `female` | Female；she / her |
| `non_binary` | Non-binary；they / them |

V1 不提供 Pronouns 输入框。未来如支持自定义代词，应增加独立结构化字段，而不是要求模型从 Settings 猜测。

现有 `content_rating` 迁移时先同时回填声明分级和平台有效分级，并记录专用迁移审核决定。以下仅是
历史代码到首版策略的初始映射，不得把 `limited / limitless` 继续作为永久分级体系：

```text
general -> policy-defined general tier
mature  -> policy-defined mature tier
```

最终分级代码、年龄阈值和地区可用性由独立内容访问策略冻结；无论分级为何，都不能绕过平台安全规则。

### 4.4 图片与取景

| 字段 | 状态 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- | --- |
| `portrait_media_id` | 新增 | `media_assets.id`；历史 Fixture 可暂时为空 | 用户上传并标准化后的原图，是媒体真实来源 | `mda_01JIMAGE...` |
| `portrait_position_x` | 新增 | `0..100`，默认 `50` | 长方形立绘/封面的水平焦点 | `46` |
| `portrait_position_y` | 新增 | `0..100`，默认 `50` | 长方形立绘/封面的垂直焦点 | `31` |
| `avatar_position_x` | 新增 | `0..100`，默认 `50` | 圆形头像的水平焦点 | `52` |
| `avatar_position_y` | 新增 | `0..100`，默认 `50` | 圆形头像的垂直焦点 | `24` |
| `cover_ref` | 现有兼容 | TEXT，可空 | 供现有首页卡片读取的稳定封面展示引用 | `/media/characters/.../cover` |
| `avatar_ref` | 现有兼容 | TEXT，可空 | 供现有聊天和历史记录读取的稳定头像展示引用 | `/media/characters/.../avatar` |
| `accent_color` | 现有系统字段 | CSS 颜色值 | 缺省视觉强调色；Create V1 不允许用户填写 | `#7c3aed` |

- V1 只上传一张 Portrait，Avatar 不单独上传。
- Portrait 与 Avatar 分别保存焦点，因为长方形构图和圆形取景通常不同。
- 不破坏性修改原图，只保存位置参数。
- `portrait_media_id` 是来源；`cover_ref / avatar_ref` 是兼容现有消费者接口的派生投影。
- 文件真实格式、尺寸、所有者、存储路径和媒体审核状态继续由共享 `media_assets` 管理，不新建 Plum 图片表。

### 4.5 私有 Runtime 信息

以下字段会影响模型回复，必须从消费者 DTO、公开日志和分析事件中排除。

| 字段 | 状态 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- | --- |
| `persona_prompt` | 现有 | TEXT，必填 | Character Settings 的当前 Runtime 投影；包含身份、性格、目标与行为原则 | `You are Luna, a guarded astronomy presenter...` |
| `scenario_prompt` | 现有 | TEXT，默认空 | Settings 中可选的长期关系/故事前提；Create V1 不提供独立输入框 | `You and the user work at a remote observatory.` |
| `speaking_style` | 现有 | TEXT，默认空 | Response Rules 的当前 Runtime 投影；名称保留以兼容现有代码 | `Never decide the user's actions.` |
| `example_dialogues` | 新增 | TEXT，默认空 | 范例对话原文；只作为模型示例，不能写成真实聊天历史 | `{{user}}: ...\n{{char}}: ...` |
| `prompt_version` | 现有 | 正整数，默认 `1` | 只在影响模型行为的内容改变时递增，用于识别过期的用户角色 Runtime | `3` |

Create V1 不依赖 AI 自动拆分 Character Settings：

- 完整 Settings 可以直接作为 `persona_prompt`。
- `scenario_prompt` 可以为空；只有导入数据、内置内容或未来明确的结构化来源提供了长期故事前提时才单独写入。
- Opening Scene 是一次性开场，写入 `greeting`，不能重复注入长期 Scenario。
- `response_rules` 投影到现有 `speaking_style`。

### 4.6 版本、生命周期与运营

| 字段 | 状态 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- | --- |
| `content_version` | 新增 | 正整数 | 当前完整内容版本，对应 `plum_character_versions.version_number` | `4` |
| `status` | 现有，明确枚举 | `draft / active / taken_down / archived` | 角色生命周期；同步机器审核不增加 `pending_review` | `active` |
| `published_at` | 新增 | 时间，可空 | 首次正式发布时间；可用于 Latest 排序 | `2026-08-11 15:30:00` |
| `heat_count` | 现有系统字段 | 非负整数 | 热度/互动聚合值，创作者不能修改 | `48200` |
| `sort_order` | 现有系统字段 | 整数 | 当前运营排序兼容字段 | `20` |
| `capabilities_json` | 现有系统字段 | JSON 文本 | 当前角色能力；Create V1 固定文本聊天，不提供 Voice | `{"text":true,"voice":false}` |
| `fixture_version` | 现有系统字段 | TEXT，可空 | 内置样例数据版本；用户创建角色为空 | `plum-reference-v2` |

内置 Character seed 只负责首次创建，不会原地覆盖或重新激活已有 Character。修改
`REFERENCE_CHARACTERS` 必须同步提升 `FIXTURE_VERSION`，并先通过正式发布流程创建新的不可变
Character Version；该发布流程尚未实现时，seed 会对版本漂移显式失败，禁止静默刷新当前投影。
| `created_at` | 现有 | 时间 | 角色记录创建时间 | `2026-08-11 14:00:00` |
| `updated_at` | 现有 | 时间 | 当前投影最后更新时间 | `2026-08-12 10:20:00` |

状态语义：

| 状态 | 含义 |
| --- | --- |
| `draft` | 保留给尚未发布的服务端角色；V1 浏览器本地草稿不写入正式角色表 |
| `active` | 已发布且正常可用 |
| `taken_down` | 被平台下架；消费者端展示 Character unavailable |
| `archived` | 创作者主动归档，不再公开提供 |

`taken_down` 优先于 `visibility`：角色被下架后，即使 `visibility=public` 也不得公开访问、创建或继续消费者聊天；所有者编辑和运营复核入口另行按权限开放。

## 5. `plum_character_versions`

本表保存每次审核通过的完整创作者输入快照。行写入后不可原地修改；回滚也应创建一个内容相同的新版本，而不是把历史行改回当前。

| 字段 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- |
| `character_id` | TEXT，引用 `plum_characters.id` | 角色 ID；与 `version_number` 组成主键 | `char_01JABC...` |
| `version_number` | 正整数 | 完整内容版本号 | `4` |
| `prompt_version` | 正整数 | 该快照对应的 Runtime Prompt 版本；纯展示修改时可与上一内容版本相同 | `2` |
| `display_name` | TEXT，必填 | 该版本角色名称 | `Luna` |
| `gender` | `male / female / non_binary` | 该版本结构化性别 | `female` |
| `portrait_media_id` | `media_assets.id` | 该版本使用的原图 | `mda_01JIMAGE...` |
| `portrait_position_x/y` | 各 `0..100` | 立绘/封面焦点 | `46 / 31` |
| `avatar_position_x/y` | 各 `0..100` | 头像焦点 | `52 / 24` |
| `intro` | TEXT，必填 | Character Intro 的唯一创作者输入；发布时同时投影到 `intro` 和 `tagline` | `A guarded stargazer...` |
| `opening_scene` | TEXT，必填 | Opening Scene 原文；发布时投影到 `greeting` | `*Luna looks up.* “You're early.”` |
| `character_settings` | TEXT，必填 | 私有长期角色设定原文 | `# Roles and Goals...` |
| `scenario_prompt` | TEXT，可空 | 兼容已有长期故事前提；Create V1 默认空 | `You and the user work at a remote observatory.` |
| `example_dialogues` | TEXT，可空 | 私有范例对话原文 | `{{user}}: ...` |
| `response_rules` | TEXT，可空 | 私有回复规则原文 | `Never decide for the user.` |
| `creator_declared_rating` | 平台分级代码 | 创作者提交该版本时声明的内容等级 | `mature` |
| `platform_effective_rating` | 平台分级代码 | 审核后确定的最终有效等级 | `mature` |
| `access_policy_version` | TEXT | 该版本发布时使用的内容访问策略版本 | `plum-rating-v1` |
| `moderation_decision_id` | TEXT；新发布必填，迁移历史可空 | 使该版本生效的真实审核决定 | `moddec_01J...` |
| `visibility` | `public / private` | 该版本公开范围 | `public` |
| `created_at` | 时间 | 该版本审核通过并生效的时间 | `2026-08-12 10:20:00` |

主键：

```text
PRIMARY KEY (character_id, version_number)
```

版本表保存创作者原始语义；当前 `plum_characters` 保存准确的 Runtime 投影。V1 不额外设计 Prompt 编译产物历史表。未来若需要逐字复现历史 system prompt，应单独增加编译器版本与产物审计设计，不向本表塞不明确的 JSON 占位字段。

## 6. Tags 表

### 6.1 `plum_tags`

Tags 必须是平台受控词表，Create/Edit 只提交稳定 `tag_id`，不接受自由输入。

| 字段 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- |
| `id` | TEXT，主键 | 稳定 Tag ID | `tag_romance` |
| `code` | TEXT，唯一 | 不随展示文案变化的稳定业务代码 | `romance` |
| `display_name` | TEXT，必填 | V1 英文展示名；最终 Tag 选项确定后再评审多语言结构 | `Romance` |
| `status` | `active / disabled` | Disabled 不再允许新选择，但历史版本仍可解释 | `active` |
| `sort_order` | 整数 | Create 选择器中的运营排序 | `20` |
| `created_at` | 时间 | 创建时间 | `2026-08-11 14:00:00` |
| `updated_at` | 时间 | 最后更新时间 | `2026-08-12 10:20:00` |

Tag 分类、同义词、互斥规则和多语言翻译表等到产品给出正式 Tag 集合后再设计。本期不把竞品截图里的词直接当生产数据。

V1 运营词表以 `data/plum/tags.json` 作为版本化离线输入源，使用下面的命令校验并幂等同步到
`plum_tags`：

```bash
.venv/bin/python -m scripts.sync_plum_tags
# 或：make sync-plum-tags
```

同步允许修改展示名、启用状态和排序，但不允许改变既有 `id / code` 身份。配置文件中遗漏的
数据库 Tag 不会被自动禁用；需要下线时必须显式写入 `status=disabled`，以免一次不完整上传造成
批量误下线。运行时 API 和创建校验都读取数据库，不在请求过程中直接读取配置文件。未来运营
后台接管后继续维护同一张表，客户端合同和历史版本关系无需改变。

### 6.2 `plum_character_version_tags`

| 字段 | 取值 / 约束 | 解释 | 示例 |
| --- | --- | --- | --- |
| `character_id` | TEXT | 角色 ID | `char_01JABC...` |
| `version_number` | 正整数 | 对应的角色内容版本 | `4` |
| `tag_id` | TEXT，引用 `plum_tags.id` | 受控 Tag ID | `tag_romance` |

主键：

```text
PRIMARY KEY (character_id, version_number, tag_id)
```

- 每个版本至少 1 个、最多 5 个 Tag，由服务层校验。
- 当前有效 Tags 通过 `plum_characters.content_version` 关联本表获得。
- 发布事务同时把当前 Tag 展示名同步到 `plum_characters.tags_json`，兼容现有 Feed/Chat。
- 删除或禁用 Tag 不得破坏历史版本关系。

## 7. Create / Edit / Runtime 数据流

### 7.1 首次创建

```text
浏览器本地草稿
  -> 提交完整 Create 请求
  -> 校验媒体 owner、字段、Tags 和并发条件
  -> 同步机器审核
  -> 审核失败：不写正式内容
  -> 审核通过：一个事务内
       1. 创建 Work
       2. 创建 Character 当前投影
       3. 写 Character Version 1
       4. 写 Version Tags
       5. 标记 Portrait media 为 referenced
```

迁移 76 与 `publish_created_character()` 已实现 owner-scoped 幂等记录及上述单事务写入。该函数只接受
服务端已验证的审核决定，不直接挂载 HTTP；正式 Create API 必须先接入海外审核 adapter，不能把审核字段
暴露给客户端。Creator Public Profile 在事务内按 User get-or-create，权限仍只以 Work owner 判断。

### 7.2 编辑

编辑请求携带 `base_version`，防止两个浏览器标签页静默覆盖：

```text
base_version != plum_characters.content_version
  -> 返回版本冲突，不覆盖任何内容

审核失败
  -> 保持当前发布内容和所有历史 Runtime 不变

审核通过
  -> 写入 version_number = current + 1
  -> 原子更新 plum_characters 当前投影与 Tags
```

V1 不建立“待审核版本指针”。未来若审核改为异步人审，再单独设计 `character_edit_submissions`，不要提前往 `plum_characters` 增加 `pending_revision_id`。

### 7.3 `content_version` 与 `prompt_version`

```text
content_version = 整个角色资料版本
prompt_version  = 影响 AI 回复的运行版本
```

| 编辑内容 | `content_version` | `prompt_version` | 解释 |
| --- | --- | --- | --- |
| Portrait、Avatar crop、Intro、Tags、分级、Visibility | +1 | 不变 | 不改变 AI 行为，但 Connection 仍记录已采用的新内容版本 |
| Opening Scene | +1 | 不变 | 只影响以后新建聊天的开场，不改写历史会话 |
| Name、Gender、Character Settings、Example Dialogues、Response Rules | +1 | +1 | 改变 Runtime 身份或回复行为 |

Character 新版本审核通过并生效后，所有已有 Connection 自动采用该版本：

- Connection 保存当前采用的 `character_id + content_version`，并保留版本切换审计；
- 纯展示内容变化只更新 Connection 的已采用内容版本，不重建 Runtime；
- `prompt_version` 变化时，在生成下一条回复前原子刷新或重建该 Connection 的 Runtime；刷新失败时不得
  更新已采用版本，也不得在同一轮生成中混用新旧 Prompt；
- 每个生成 Turn 记录实际采用的 Character `content_version`、`prompt_version` 和访问策略版本，保证
  升级前后的历史回复可以审计；
- Character 升级保留 Connection 的关系、记忆和剧情；Opening Scene 只用于重开后创建的新 Connection，
  不插入已有 Conversation；
- 最新平台有效分级或访问策略使当前 User 不再具备资格时暂停 Connection，不得回退旧版本绕过访问控制；
- `taken_down` 优先于升级流程，下架后新旧 Connection 都不得继续生成角色回复。

现有 `plum_character_bindings` 以 `platform_user_id + character_id` 唯一，不能表达同一 Persona 与 Character
的多个独立 Connection。它只能作为迁移期兼容结构；生产实现应改为 Connection 级 Runtime Binding，
`character_prompt_version` 只用于缓存失效，不能代替 Character Version。

## 8. 不放入上述表的内容

| 数据 | 处理方式 | 原因 |
| --- | --- | --- |
| 成人与版权确认 | 后续合规审计记录 | 需要记录用户、规则版本和确认时间，不能只是 Character 布尔值 |
| 审核策略、命中项与人审操作 | 后续审核提交/记录表 | 本文只定义审核通过后的正式内容和 `taken_down` 结果 |
| World、世界书 | 未来 `World / World Version / Work-World Binding` | World 是可跨 Work 复用并独立版本化的创作资源 |
| 作品故事前提与初始局面 | 多角色作品设计时再定 | 它回答“这次故事为什么发生”，不同于 World 的运行规则和 Opening Scene 的第一幕 |
| 角色关系 | 多角色 Runtime 与消费交互确定后再定 | 不能先用无语义 JSON 占位 |
| 用户身份 / Persona | 单独的用户拥有与隐私模型 | 不能作为 Character 普通字段 |
| 服务端草稿同步 | 确认跨设备草稿需求后单独设计 | V1 原型使用浏览器本地自动保存，不能让草稿覆盖当前发布投影 |
| 角色审核后的编辑队列 | 未来 `character_edit_submissions` | V1 采用同步机器审核，拒绝时不产生正式版本 |

未来 World 的边界已经确定，但本期不建表：

```text
World：这个世界如何运转？
故事前提与初始局面：这次故事为什么发生、从什么局面开始？
Opening Scene：用户进入后第一幕看到了什么？
```

World 应独立版本化并由 Work 固定绑定某一版本；Character 不能直接读取另一 Work 的数据。

## 9. 实施兼容要求

1. 采用加表、加列和回填迁移，不替换现有 `plum_characters`，也不改变已有 `character_id`。
2. 旧 `tagline / greeting / tags_json / avatar_ref / cover_ref / persona_prompt / scenario_prompt / speaking_style` 均保留，继续满足现有 Feed、聊天和 Fixture。
3. 现有内置角色建立初始 Work 和 Version 1；不能根据文案自动推断 Gender，允许在数据补齐前为空。
4. 普通消费者 DTO 明确列字段，绝不能使用 `SELECT *` 后删除少数字段的方式暴露私有 Settings、Examples 或 Prompt。
5. 当前投影、版本行、Tags 和媒体引用必须在同一数据库事务生效；任何一步失败都不得出现“页面是新版本、Runtime 仍是旧版本”的半完成状态。
6. 迁移 72 与 76、Create 原子发布 Repository 已在代码中实现并带 PostgreSQL 聚焦测试；正式环境仍须通过受控迁移闸执行，部署前核对回填计数和备份，不由普通应用启动隐式跨越。
