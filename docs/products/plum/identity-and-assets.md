# Plum 账号、身份与资产关系

更新时间：2026-08-11

> 归属：`product:plum`。本文冻结 Plum 账号、身份、资产及其关系的产品语义，作为注册、
> onboarding、角色互动、Context Files、记忆、创作、商业权益和数据治理的共同基础。
> 本文不是数据库或 API 设计；实现可以演进，但不得改变这里的归属和隔离语义而不更新决策。

## 1. 目标

Plum 必须同时支持一个真人拥有多个故事身份、与同一角色建立不同关系、在关系中经历多段剧情，
并清楚区分私人数据、公开内容和商业权益。基础模型需要回答：谁拥有资产、资产代表谁、在哪个
故事范围内有效，以及编辑、重开、下架和删除时会发生什么。

核心原则是：

> User 拥有账号与资产；Persona 代表用户进入故事；Persona–Character Connection 维护关系；
> Storyline 承载具体剧情；Conversation 推进 Storyline。

本期已冻结以下产品决策：

1. 一个 Work 可以包含多个 Character，但一个 Character 只能属于一个 Work；
2. Character 发布新版本后，已有 Connection 自动采用新版本，版本切换必须可追溯；
3. Persona 在首次建立 Connection 后锁定，V1 不建立 Persona Version；锁定前可修改，锁定后只能复制为新的 Persona；
4. 同一组 Persona + Character 同时只能存在一段 active Connection，但可以保留多段 archived 历史 Connection；
5. 产品只提供一种“重开”：由选定 Persona 与 Character 新建 Connection，关系、记忆、剧情和会话上下文均不继承；
6. 创作者声明分级与平台最终有效分级分开记录，访问策略独立版本化；
7. Character 被平台下架后，已有 Connection 也不能继续互动。

## 2. 核心概念

### 2.1 User 与 Plum Membership

User 是可认证的真人或合法账号主体，也是隐私请求、安全控制、付费、退款和数据删除的权利主体。
Plum Membership 表示该 User 在 Plum 中的产品资格、状态、全局偏好和合规确认。

产品文案中的“账号”默认指 User 及其 Plum Membership。底层 Agent Runtime 的 account 只是内部
执行容器，不是新的用户身份，不得暴露为用户拥有多个“账号”的产品语义。

### 2.2 Public Profile

Public Profile 是用户参与社区或创作时对外展示的身份，可以承载昵称、头像、创作者主页、公开评论
和发布内容。它不同于登录主体，也不同于故事中的 Persona。是否允许一个 User 拥有多个公开身份，
在创作者能力设计时单独决策。

### 2.3 Persona

Persona 是“用户在故事中是谁”的可复用身份卡，可以包含称呼、头像、背景、表达偏好和明确边界。
一个 User 可以拥有多个 Persona。Persona 属于 User 并默认私有；V1 不建立 Persona Version。

Persona 在首次建立 Connection 前可以修改；建立首个 Connection 的同一事务必须锁定 Persona，此后名字、
头像、背景和 Prompt 等身份内容不可修改。用户需要调整时复制为新的 Persona，再使用新 Persona 建立
Connection。停用、删除标记等生命周期字段不属于身份内容。Persona 不是实名资料，也不应默认公开；
切换 Persona 不得改变 User 的购买、安全控制或法律责任。

### 2.4 Character 与 Character Version

Character 是平台或创作者发布的角色内容资产，包括人格、世界设定、场景、开场、表达风格、内容
分级和能力声明。Character 必须具备所有者、版本、发布、审核、下架和删除语义。

已发布版本应保持可追溯。Character 发布新版本后，新旧 Connection 都自动采用最新生效版本；
Connection 必须记录当前采用的 Character Version 及版本切换历史，避免 Runtime、页面投影和审核状态
处于不同版本。Character 被平台下架时，所有 Connection 立即停止继续互动。

### 2.5 Persona–Character Connection

Persona–Character Connection 是某个 Persona 与某个 Character 建立的一段关系实例，也是叙事连续性
的聚合根。它绑定一个已锁定且不可修改的 Persona，并记录当前采用的 Character Version；Character 升级时 Connection
跟随升级，但关系状态、关系称呼、互动边界、共同里程碑和跨剧情关系记忆继续保留。

同一个 User 使用不同 Persona 与同一 Character 互动时，必须创建相互隔离的 Connection。同一个
Persona 与同一 Character 可以先后建立多段独立 Connection，用于“彻底重新认识”，但同时只能有
一段 active Connection。重开必须先让旧 Connection 退出 active，再创建新的 Connection；历史关系
继续作为独立 archived Connection 存在，不能被覆盖成同一条记录。

### 2.6 Storyline 与 Conversation

Storyline 是一段 Connection 中的具体剧情线或篇章，承载场景、剧情变量、当前进度和只在该剧情
成立的记忆。Conversation 是推进 Storyline 的会话与消息容器；一条 Storyline 可以跨多次会话继续。

Storyline 可以用于系统内部的篇章组织，但产品不提供“保留原 Connection、只重开 Storyline”的
重开方式。用户执行重开时，由选定 Persona 与 Character 创建全新 Connection，并从空白关系、记忆、
剧情和会话上下文开始。

## 3. 关系模型

```text
User
├── Plum Membership：产品资格、合规确认、全局偏好
├── Public Profile：公开社区与创作身份
├── User 级资产：收藏、屏蔽、举报、钱包、购买与角色访问权益
└── Persona（可多个，首次建立 Connection 后锁定）
    └── Persona–Character Connection（可多个独立关系）
        ├── Locked Persona
        ├── Current Character Version + Upgrade History
        ├── Relationship State
        ├── Shared Relationship Memory
        └── Storyline（可多个剧情篇章）
            ├── Plot State
            ├── Plot Memory
            └── Conversation / Messages
```

Character 由平台或创作者拥有；User 购买的是访问权或使用权益，不因此取得 Character 的内容所有权。
User 拥有自己产生的私有 Persona、Connection、Storyline、会话和记忆数据，并受平台条款、安全规则
和适用法律约束。

## 4. 资产归属与作用域

| 资产 | 权利或内容主体 | 直接作用域 | 关键规则 |
| --- | --- | --- | --- |
| 登录凭证、合规确认、全局偏好 | User | User / Membership | 不随 Persona 切换 |
| 钱包、订阅、退款与永久权益 | User | User / Membership | Persona 删除不得影响账务记录 |
| 收藏、关注、屏蔽、举报 | User | User + Character | 可以在选择 Persona 前产生 |
| 角色访问或购买权益 | User | User + Character | 默认跨 Persona 复用；剧情消耗品另行定义 |
| Persona | User | Persona | 默认私有；首次建立 Connection 后锁定，可复制和停用 |
| Character 及发布版本 | 平台或创作者 | Work / Character | 一个 Character 只属于一个 Work；可审核、版本化、下架和申诉 |
| 关系、称呼、边界与共同里程碑 | User，通过 Persona 参与 | Connection | 不得跨 Persona 或独立 Connection 串联 |
| 跨剧情关系记忆 | User，通过 Persona 参与 | Connection | 只在同一关系连续性内使用 |
| 剧情状态与局部剧情记忆 | User，通过 Persona 参与 | Storyline | 不得自动进入其他 Storyline |
| 会话和消息 | User，通过 Persona 参与 | Storyline / Conversation | 必须可追溯到 User、Connection 和 Storyline |
| 公开评论、分享或创作内容 | 对外身份及其 User | Public Profile / Published Asset | 公开副本与私有上下文分离并经过治理 |

这里不建立一个笼统的 “User–Character State” 或 “Persona–Character State”。User 与 Character 之间
存在收藏、安全控制、权益等离散资产；Persona 与 Character 之间通过 Connection 建立叙事关系。
总互动次数、最近聊天时间、Connection 数量等可以聚合或缓存，但不是关系事实的权威来源。

### 4.1 内容分级与访问资格

Character Version 分别记录创作者声明分级和平台审核后的最终有效分级。发现、开聊和每次继续互动
都依据平台有效分级及对应的访问策略版本，结合 User 的年龄、地区和合规状态重新判断资格；Persona
不能改变或绕过该资格。Character 升级后如果最新版本不再对当前 User 可用，该 Connection 必须暂停，
不得回退到旧 Character Version 继续互动。

## 5. 版本跟随与重开

### 5.1 版本规则

- 新建 Connection 时引用已锁定的 Persona，并记录 Character Version；
- 首次建立 Connection 的事务负责锁定 Persona；锁定后的身份内容不可更新，复制会生成新的 `persona_id`；
- Character 发布新版本后，已有 Connection 自动采用最新生效 Character Version；
- Character 升级不重建 Connection，也不清除既有关系、记忆或剧情；
- 版本切换必须在生成下一条回复前完成，Runtime 刷新失败时不得形成新旧版本混用；
- Character Version、采用时间和升级结果必须可审计，每个生成 Turn 能追溯实际使用的版本；
- 最新版本不满足当前 User 的内容访问资格时暂停互动，不允许继续使用旧版本；
- 安全或法律下架优先于版本跟随并立即阻断互动。

### 5.2 唯一重开方式

- 用户选择 Persona 与 Character 后创建新的 Connection；即使组合与旧 Connection 相同，也不得复用旧 Connection；
- 创建普通聊天时如果该 Persona + Character 已有 active Connection，应恢复现有 Connection，不能并行新建；
- 新 Connection 不继承旧关系状态、关系记忆、剧情状态、剧情记忆、会话或 Runtime Context；
- 旧 Connection 必须退出 active 状态且不能再向新 Connection 提供 Context。这里的“清零”指不继承，
  旧数据是归档还是物理删除仍由数据生命周期和用户删除规则决定。

收藏、屏蔽、举报、购买和永久访问权益属于 User，不因新建或删除 Connection 改变。

## 6. 可见性、分享与删除

- Persona、私有关系记忆、剧情记忆和会话默认仅当前 User 可见；
- Character、公开主页和已发布内容按发布状态与审核结果可见；
- 将私有会话或记忆分享为公开内容时，应创建受审核的发布副本或快照，不把私有 Context 直接公开；
- 停用 Persona 后不得用于新 Connection，已有 Connection 继续引用原锁定 Persona，直至用户删除相关数据；
- 删除 Storyline 只影响该剧情线；删除 Connection 同时处理其 Storyline、关系状态和关系记忆；
- 删除 User 触发所有私有资产、公开身份、授权和法定留存数据的完整账号生命周期流程；
- Character 下架后停止发现、新建 Connection 和所有已有 Connection 的继续互动；历史数据仅按隐私、
  申诉和法定留存规则保留，不得再次进入生成 Context。

具体软删除、恢复期、法定留存、导出格式和创作者内容下架策略在隐私与账号生命周期专题中冻结。

## 7. Context 与隔离不变量

LLM 上下文应按以下语义逐层组装：

```text
产品和安全规则
→ Locked Persona
→ 当前 Connection 已采用的 Character Version
→ 当前 Connection 的关系状态与共享关系记忆
→ 当前 Storyline 的剧情状态与剧情记忆
→ 当前 Conversation
```

以下是不允许被实现细节改变的产品不变量：

- 所有私有资产最终都能解析到唯一 User，任何查询和写入必须保持账号隔离；
- Connection 之间不得自动共享关系、称呼、共同经历或长期记忆；
- Storyline 之间不得自动共享局部剧情事实；
- Persona 切换必须选择或创建对应 Connection，不得静默改变现有 Connection 的身份；
- 已被 Connection 使用的 Persona 不得修改身份内容；复制 Persona 不得继承原 Connection 的关系和记忆；
- Character 新版本可以更新 Connection 使用的角色定义，但不得跨 Connection 合并或转移关系与记忆；
- 重开后旧 Connection、Storyline、Conversation 和 Runtime Context 不得进入新 Connection；
- Character 下架后任何已有 Connection 都不得继续生成角色回复；
- Runtime account、文件目录或模型 session 必须映射到明确 User 和叙事作用域，不得成为新的所有权来源；
- 推荐与统计可以聚合，但不得反向污染权威关系状态和记忆；
- 支付、安全、隐私和法律责任始终以 User 为主体，不能转移给 Persona。

## 8. 后续专题必须引用的边界

- 注册与产品 onboarding 创建或确认 User、Membership 和合规状态，不等于建立角色关系；
- 角色故事入口选择 Persona，并创建或恢复 Persona–Character Connection；
- Context Files 和记忆方案必须分别说明 User、Persona、Connection、Storyline 和 Conversation 的来源；
- 钱包、订阅和永久角色权益默认属于 User，Connection 或 Storyline 级商品必须明确告知重开后的处理；
- 推荐事件至少保留 `user_id` 与 `character_id`，可以按场景附带 Persona、Connection 和 Storyline；
- 创作者系统必须区分 User、Public Profile、Character 所有权和最终发布版本。

## 9. 待后续冻结

- Public Profile 是否允许一个 User 拥有多个，以及 Persona 是否可以主动公开；
- 哪些记忆允许从 Storyline 提升为 Connection 级共享记忆，以及由谁确认；
- 创作者注销和 Character 所有权转移时的资产处理；
- Connection / Storyline 级付费内容、消耗品和退款规则；
- 数据导出、恢复期、硬删除时限和法定留存要求。
