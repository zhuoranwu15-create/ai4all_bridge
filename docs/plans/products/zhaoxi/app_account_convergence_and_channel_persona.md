# App 接入:账号收敛(App 层身份) + 渠道化人设 — 调研与方案交接

> 状态:**方案定稿,待实施**(生产已验证 + 决策已定,见 §9)。本文自包含,可直接在线上机接续。
> 语言:中文(项目规范)。所有代码位置给到 `file:line`,均基于本次调研时的 `main`。
> 定稿更新(2026-07-16,aliyun1 生产 PG 验证):S1 审计 = 0 多账号 user ⇒ **S2 存量规范化不需要**;Q1–Q5 已决策。**最终方案见 §9**,§1–§8 为调研原文保留。

---

## 0. 一句话背景

面向 App 流量接入做梳理时,浮出一个比"文案穿帮"更底层的问题:**同一手机号在 App / 微信 / (未来)Web 之间,长期记忆与 AI 人设是否共享?** 讨论后确立了"**App(产品)层**"抽象,并选定 **A 方案(收敛:一手机号一 App 一账号)**。本文记录调研事实、三层模型、A 方案执行步骤、以及并行的"渠道化人设(穿帮修复)"方案。

---

## 1. 已验证的事实(带 file:line)

### 1.1 身份数据模型
- `platform_users`(phone UNIQUE)→ `account_owner_bindings`(`platform_user_id`↔`account_id`)→ `accounts`(`aid_` + 9 位数字)。
  - `app/db/_core.py:371` platform_users;`:464` account_owner_bindings;`:359` accounts。
  - owner_bindings 现有约束仅 `UNIQUE(platform_user_id, account_id)`(`_core.py:473`)——**只防同一账号被同一用户重复绑,不阻止一个用户拥有多个账号**。这就是多账号的口子。
- **记忆/人设/状态全部挂在 account 级**:SOUL/IDENTITY/USER/MEMORY(profile 文件,厚节点下入库 `account_profile_files` / `profile_storage`)、dreaming、relationship_state、mission、wallet、rate limit。
  - ⇒ **"共享记忆/人设" ⟺ "同一个 account_id";"独立" ⟺ "不同 account_id"**。

### 1.2 微信与 App 今天已经共享同一账号(关键结论)
- 微信绑定流(web 端扫码):`app/routers/web.py` 的 `web_login` / `web_create_binding_intent` / `web_register_and_binding_intent`:
  phone OTP → `_register_platform_user_with_otp` → **`get_or_create_default_ai4all_account_for_user`** → `create_binding_intent(channel="openclaw-weixin")` → 扫码 → completed intent。
  微信入站解析:`resolve_account_id_for_inbound_channel_identity`(`app/db/billing.py:2576`)按 completed binding intent 命中 `account_id`。
- App 流:`app/routers/app_api.py` `app_create_session`(`:172`)→ 同 phone → 同 `platform_user` → **同一个 `get_or_create_default_...`** → `get_active_bound_account_for_user_in_app(..., app_id='zhaoxi')`（取该产品唯一 active owner_binding）。
- `get_or_create_default_ai4all_account_for_user`(`billing.py:2196`):有账号返回最早那个,**仅当一个都没有时才新建**。
- ⇒ **同手机号下,App 与微信落到同一个 `account_id`,长期记忆与人设「今天就已经打通」**。这是现状,不是待建功能。

### 1.3 "一手机号最多 10 个 agent" 的真相
- 唯一出处:`app/db/billing.py:2090-2091`,`create_ai4all_account_for_user` 内的**硬编码字面量** `if existing_count >= 10: raise ValueError(...)`。全仓库无第二处、无 config、无文档。
- 正常登录链路(微信/App/web 都走 `get_or_create_default`)**永远碰不到它**(有账号即返回,不新建)。
- 能累积到多账号的入口只有两个:
  - `app/routers/web.py:948` `POST /web/agents`(`web_create_agent`,自定义 agent_name/role_prompt)。
  - `app/products/zhaoxi/api/debug.py:695` `debug_create_account`(调试)。
- **多账号读侧从未做**:没有"列出我的 agent / 切换"端点(`list_accounts()` 是全局 admin 用)。⇒ `POST /web/agents` 是半成品遗留,不是在用的成熟功能。
- 四处"按用户取单账号"的解析器规则已统一(`ORDER BY b.created_at ASC, b.id ASC LIMIT 1`):`billing.py:340`、`1177`、`2165`;`:1967` 为反向(按账号取用户)。⇒ 收敛后"最早"即"唯一",取账号逻辑无需改。

### 1.4 穿帮(问题 A):三层"微信味" + 名字不一致
账号是 App 与微信共享的**同一个**,故 AGENTS/人设是共享的;渠道差异必须在**渲染期**处理。当前"微信"被焊死在三层:

- **L1 系统文件(全账号共享,每轮注入 Block 3)**
  - `data/system/AGENTS.md:3` "你是这个**微信账号**的个人 AI 陪伴";`:5` "像真人发**微信**";`:11` "你通过**微信插件**接入…你是用户的一个**微信好友**"。
  - `data/system/TOOLS.md:7` "只能发回当前**微信对话**";`:40` "**微信** 24 小时送达限制";`:51` "不能通过**微信之外**的渠道行动"。
- **L2 账号 IDENTITY 播种模板(渠道无关,错误地写死微信)**
  - `app/user_profiles.py:694-699` `_default_user_context_templates`:"你是用户**在微信里**的个人 AI 陪伴""以「你的**微信好友**」自称"。
  - 播种时机:首轮 `read_agent_context`(`user_profiles.py:765`)→ `ensure_agent_context_files`(`:738`)懒创建;`create_ai4all_account_for_user` **不**播种文件。
- **L3 呈现规则块(无条件注入,标题带"微信")**
  - `app/prompt_builder.py:144-150` `_OUTPUT_DIRECTIVES_FIXED` 标题"【**微信**回复呈现】""默认**微信**纯文本";`:331` `_add(...)` **无渠道判断**。
  - `PromptBuilder.assemble`(`prompt_builder.py:265`)目前**无 channel/cap 参数**。
- **L4 名字不一致**
  - App UI 打"朝夕":`app_api.py:107-108`(`_public_account` 默认名)、`:202`(welcome_message)。
  - 但 seeded IDENTITY 无名(`_NO_NAME_IDENTITY_LINE`,`user_profiles.py:57`),因 `app_create_session` 传 `display_name=None`(`app_api.py:184`)。⇒ LLM 自称"我/微信好友",不认"朝夕"。
  - `_LEGACY_DEFAULT_ASSISTANT_NAMES={"AI4ALL 助手"}`(`user_profiles.py:58`),`_clean_default_account_display_name`(`_core.py:71`)只滤 legacy ⇒ **"朝夕"作为 display_name 安全**。

### 1.5 渠道能力已有声明层(可复用)
- `app/channels.py`:`ChannelCapability` + `CHANNELS` 表。App cap(`:92`):`active_session_key="__app_active__"`、onboarding 关、`supports_proactive=False`、`tdai_enabled=False`、`default_reply_delivery="sync_http"`。
- 组装期 `cap`/`identity.channel` **可取**:`turn_service` 中 `builder.assemble` 在 `:649`(该函数 `:441` 起即有 `cap`);`read_agent_context` 在 `:544`;`ensure_agent_context_files` 在 `:921`。

---

## 2. 对齐后的三层模型

```
App / 产品 (app_id)            ← 分组单元;拥有 AGENTS.md、设计哲学、人设默认名、账号分组
  └─ Channel / 传输 (channel)  ← weixin / native / web;传输事实(24h窗)、呈现(纯文本)、短期 session 键、投递/proactive
       └─ Account              ← 记忆/人设/状态/钱包的隔离单元
```

- **归属规则**:`一个手机号 × 一个 App = 一个 account`;该 App 下所有 channel 共享它。跨 App 完全隔离(账号、AGENTS、哲学另起一套)。
- 现状映射:今天全部资产 = **一个 App「朝夕相伴」**(app_id 拟为 `zhaoxi`);微信 + 朝夕 native +(未来)朝夕 web 都是它的 channel。
- 各层内容归属(问题 A 的正解):

| 内容 | 归层 | 处理 |
|---|---|---|
| "你是用户的个人 AI 陪伴""设计哲学""语气" | **App**(app_id) | `AGENTS.<app_id>.md`,**传输中性**(去"微信");未来 app 各一套 |
| "通过微信联系/24h 送达窗/只能发回当前对话" | **Channel**(weixin) | 组装期按 channel 注入的传输事实块;native/web 各有自己的 |
| "默认纯文本/不用 Markdown" | **Channel** | `cap.reply_presentation`,按 channel |
| AI 名字 | **App 默认 + 账号可覆盖** | zhaoxi 默认"朝夕";用户在微信 onboarding 起过名则全渠道显示那个名 |

---

## 3. 决策:选 A(收敛)

> **A = 收敛为"一 App 一账号"**:每个 `platform_user` 最多一个 active 账号(单 App 期);未来多 App 泛化为"每 `(platform_user, app_id)` 一个"。
> 依据:与产品语义最贴合、模型最干净;且多账号本是半成品遗留(§1.3),收敛代价小。

---

## 4. A 收敛 — 执行步骤(有序,存在数据依赖)

**S1. 数据审计(需在线上 PG 跑,本地查不到)**
```sql
SELECT platform_user_id, COUNT(*) AS n
FROM account_owner_bindings
WHERE status='active'
GROUP BY platform_user_id HAVING COUNT(*) > 1;
```
- 空 → 直接 S3 加索引。
- 非空 → 先做 S2。

**S2. 存量规范化(仅当 S1 有多账号;受控脚本,不进自动 `_MIGRATIONS`)**
- 规范账号选择规则:**有 active 微信 `channel_binding` 的那个;否则最早(created_at ASC)**。
- 其余 owner_binding `status` 置 `archived`。**不删账号、不删记忆**(账号隔离 + 用户数据不变量)。
- 脚本放 `scripts/`,人工审阅后手动执行,不静默自动化(数据决策)。

**S3. DB 层唯一保证(进 `_MIGRATIONS` 新增迁移函数,勿用启动补丁)**
```sql
CREATE UNIQUE INDEX ux_owner_binding_active_user
ON account_owner_bindings(platform_user_id) WHERE status='active';
```
- 已有同款部分唯一索引范式:`ux_messages_account_message`(`_core.py`)。SQLite/PG 均支持 `WHERE`。
- 存量有重复会建索引失败 ⇒ 必须先 S2。
- Phase 2 落 `app_id` 时改为 `(platform_user_id, app_id) WHERE status='active'`。

**S4. 关闭多账号入口 `web_create_agent`(`web.py:948`)**
- 待决策 Q1:直接删(端点 + `WebCreateAgentRequest`),或先 `410 Gone` + 命中日志灰度观察再删。
- 保留 `create_ai4all_account_for_user` 本体(get_or_create / debug 仍用)。

**S5. 收敛 `>=10` 上限(`billing.py:2090`)**
- 语义(≤10)与 A(≤1)冲突,且由 S3 索引接管。
- 改为:create 前若该 user 已有 active 账号则拒绝直接建号(get_or_create 无害,只在无账号时进 create);或移除软检查,交唯一索引兜底并把 `IntegrityError` 翻译成清晰报错。

**S6. 解析器显式化（现为 `get_active_bound_account_for_user_in_app`）**
- 逻辑不变(earliest);返回前 `COUNT`,`>1` 打 `warning` 日志,探测未收敛用户,避免"取最早"静默掩盖脏数据。

**冲突/不一致对照表**

| # | 冲突点 | 位置 | 修改 |
|---|---|---|---|
| C1 | 多账号创建口 | `web.py:948 POST /web/agents` | 410→删(或直删) |
| C2 | ≤10 上限语义 | `billing.py:2090` | 改 ≤1 不变量 / 交索引兜底 |
| C3 | 存量多账号 | 数据 | 审计→规范化脚本(archive 非规范,不删记忆) |
| C4 | 无 DB 唯一保证 | `_core.py` owner_bindings | 加部分唯一索引(迁移) |
| C5 | "取最早"静默 | `billing.py:2158` | >1 告警日志 |
| C6 | debug 建号可绕过 | `debug.py:695` | 校验:仅对新 user 建;命中索引给清晰错(见 Q3) |

---

## 5. 渠道化人设(问题 A 穿帮修复)— 与收敛正交,可并行

**Phase 1(当下,单 App,零账号语义变更):只做 Channel 渲染期差异化 + 名字兜底**
- L3:`ChannelCapability` 加 `reply_presentation`(weixin/app/web);`prompt_builder` 把 `_OUTPUT_DIRECTIVES_FIXED` 改为按 key 取的字典,`assemble(reply_presentation="weixin")` 默认选微信原文(**默认字节级不变,满足原则一**);`turn_service:649` 传 `cap.reply_presentation`。
- L1:把 `AGENTS.md`/`TOOLS.md` 里的**传输三行**(微信好友/24h/只能发回微信)从共享人设中剥离,改为**按 channel 注入的传输事实块**;weixin 传输块把这几行"还"回去,使**微信面渲染结果与今天等价**(以快照测为验收线;跨层重组后严格逐字节需仔细摆位)。
- L2:`read_agent_context`/`ensure_agent_context_files`/`_default_user_context_templates` 加 channel 参数;IDENTITY 播种改中性文案(去"在微信里/微信好友"),微信默认保持现文。
- L4:`app_create_session` 名字兜底 = 账号真实名;"朝夕"降为 zhaoxi 默认兜底(**仅新建且无名时**写入,不无条件覆盖用户已起的名)。

**Phase 2(第二款独立 App 出现时才做):App 分组落库**
- `accounts.app_id`(迁移,backfill `zhaoxi`)+ App 化解析器 `get_or_create_account_for_user_in_app(platform_user_id, app_id)` + `AGENTS.<app_id>.md` 变体 + `app/apps.py` 注册表(`app_id → {default_ai_name, agents_variant, ...}`)。
- 即 **App 分组是"预留设计、按需启用"**,不提前背迁移成本。

---

## 6. 待决策(阻塞实施)

- **Q1** `web_create_agent`:直接删 还是 先 410+日志灰度?
- **Q2** 本次是否**顺带加 `accounts.app_id` 列**为多 App 打底,还是纯做 A 最小集?
- **Q3** debug 建号是否需保留"给已有 user 建第二账号"能力(影响唯一索引是否留 bypass)?
- **Q4** 短期会话语义确认:App 与微信是"两个独立实时会话(`__app_active__` vs `__account_active__`)+ 共享长期记忆",认可?还是要连实时上下文也串(则 session 隔离键需合并,改动更大)。
- **Q5** channel 命名:现 `CHANNEL_APP="app"` 指 native 传输,会与"App 层"撞名。是否把该 channel 改名 `native`(数据量小,迁移便宜)以彻底区分"App=产品 / channel=传输"?

---

## 7. TODO(按建议顺序;`[ ]` 待办)

> 决策已定 + 生产已验证 + **代码已实现并通过测试**(2026-07-16,分支 `feat/app-account-convergence-and-channel-persona`);下表标记进度。**待生产部署**(迁移 m0022–m0024 于发布时由 `init_db` 应用)。

收敛(A):
- [x] **S1** 线上 PG 审计 = **0 多账号 user**(2026-07-16,§9.1)
- [x] **S2** 免做(S1 为空,无存量清理)
- [x] **A1/A2**(原 S3)`_MIGRATIONS` m0022 `accounts.app_id`+owner_bindings 冗余列(Q2)→ m0023 部分唯一索引 `(platform_user_id, app_id)`
- [x] **A3** 建账号/绑定写 `app_id='zhaoxi'`(`billing.DEFAULT_APP_ID`)
- [x] **A6**(原 S4)**直接删** `web_create_agent` + `WebCreateAgentRequest`(Q1)
- [x] **A4**(原 S5)收敛 `billing.py` ≤10 → ≤1,IntegrityError 跨后端翻译兜底
- [x] **A5**(原 S6) 产品级入口账号解析器加 >1 告警
- [x] **A7** debug 建号:`debug_create_account` 不建 owner_binding,天然不碰唯一索引,无需改(Q3)
- [x] **B**(Q5)channel `app`→`native`:m0024 单行 `UPDATE` + `channels.py` 常量值(变量名保留)
- [x] 测试:同 `(user,app)` 二次建号被拒;索引 DB 层兜底 + archived 不占名额;解析器告警;m0022–m0024 SQLite 幂等
- [ ] **部署**:发布分支 → `init_db` 在生产 PG 应用 m0022–m0024(PG 端 DDL 于部署时最终验证)

渠道化人设(问题 A,已随本次并行实现):
- [x] L3 `reply_presentation`(channels + prompt_builder + turn_service),默认 weixin 档字节不变
- [x] L1 传输措辞剥离为 channel 变体文件(`AGENTS.native.md`/`TOOLS.native.md`);weixin 读原文件字节不变,native 读变体、缺失回落原文件
- [x] L2 IDENTITY 播种加 channel 参数(`read_agent_context`/`ensure_agent_context_files`/`_default_user_context_templates`),微信默认不变
- [x] L4 名字兜底(`app_create_session` 传 zhaoxi 默认名「朝夕」,仅 get_or_create 新建无名时写入)
- [x] 测试:App(native)组装 prompt 的 L1/L2/L3 不含"微信"、播种「朝夕」;weixin 默认档逐字节不变(单测覆盖)

> L1 实现说明:调研设 §5 设想"传输块按 channel 注入 + weixin 逐字节还原",但 AGENTS.md 的"微信"字样是句中词而非独立行,strip+append 无法字节还原。为守住原则一,改为**渠道变体文件**:weixin 恒读原文件(字节级不变、非"重组还原"),native 读去微信味的 `AGENTS.native.md`/`TOOLS.native.md`。此机制正是 Phase 2 `AGENTS.<app_id>.md` 变体的前身,方向一致。变体文件为新拟文案,建议运营/产品复核。

Phase 2(按需,Q2 已决定本次提前落 app_id 列):
- [x] `accounts.app_id` 列 + backfill zhaoxi(m0022;owner_bindings 亦加冗余列)
- [ ] App 化解析器 `get_or_create_account_for_user_in_app` + `app/apps.py` 注册表(仍 Phase 2)
- [ ] `AGENTS.<app_id>.md` 变体机制(本次先落 channel 变体 `AGENTS.native.md`,app_id 变体待第二款 App)

---

## 8. 关键不变量与红线(实施时守住)
- **按 `account_id` 隔离是核心不变量**:任何未按 account 约束的 DB 查询/文件写入都是 bug。
- **收敛不删记忆**:规范化只 archive 非规范 owner_binding,绝不删账号/记忆。
- **原则一(微信零影响)**:所有渠道差异化默认值回落微信原文;微信主链路渲染结果字节级不变(以快照测把关)。
- schema 改动走 `_core.py` 的 `_MIGRATIONS`,不用启动期 `_ensure_column`。

---

## 9. 最终方案(生产验证后定稿,2026-07-16)

### 9.1 生产验证结果(aliyun1 本地 PG,只读审计)

| 审计项 | SQL/口径 | 结果 | 结论 |
|---|---|---|---|
| **S1 多 active 账号 user** | `owner_bindings status='active' GROUP BY user HAVING COUNT>1` | **0 行** | **S2 存量规范化脚本不需要** |
| 规模 | platform_users / accounts / owner_bindings | 72 / 85 / 72 | active 集合 **72↔72 一对一** |
| 每 user active 账号数 | 分布 | 全部 = 1 | "最早=唯一"已然成立 |
| 历史任意 status 多账号 user | `GROUP BY user HAVING COUNT(DISTINCT account)>1` | 0 | 无历史多账号残留 |
| 13 个无 owner_binding 孤儿账号 | 明细 | 1 本地 mock + 11 已 `deactivated` 真实号 + 1 `smoke`(is_debug=1) | 均非"活跃用户第二账号",不威胁不变量 |
| S3 索引可建性 | — | 无重复冲突 | 可直接建,**无需先跑 S2** |
| channel `'app'` 存量 | 5 张含 channel 列的表 | **仅 `channel_bindings` 1 行 `'app'`**;`accounts.channel` 全 `openclaw-weixin` | 改名迁移只需单行 `UPDATE` |
| 迁移基线 | `schema_migrations` | 线上已应用至 **v21**(1–13,19–21) | 新迁移从 **v22** 起 |

⇒ **A 收敛在生产是"零存量清理"场景**,风险远低于原调研预估。

### 9.2 决策定稿(§6 Q1–Q5)

| Q | 决策 | 说明 |
|---|---|---|
| **Q1** web_create_agent | **直接删** | 无读侧半成品死代码,线上无活跃多账号,无灰度必要(符合门控偏好:死代码直接清) |
| **Q2** app_id 列 | **本次顺带加** | 加 `accounts.app_id`(+ owner_bindings 冗余列)backfill `zhaoxi`,唯一索引直接落 `(platform_user_id, app_id)` 终态,多背一次迁移换未来省事 |
| **Q3** debug 建号 | **保留 admin 建号能力,不留唯一索引 bypass** | 对已有 active `(user, app)` 再建 → 命中唯一索引 → 翻译成清晰 409/报错 |
| **Q4** 短期会话 | **确认现状**:App 与微信各自独立实时会话(`__app_active__` vs 账号 active)+ 共享长期记忆 | 本次不合并 session 隔离键 |
| **Q5** channel 命名 | **改名 `native`** | 彻底区分 "App=产品层 / channel=传输层";数据仅 1 行,迁移便宜 |

> `app_id` 说明:`app_id` 是账号不可变属性(账号一旦属于某 App 永不改),故冗余到 `owner_bindings` 安全(创建时写入、永不同步漂移),使"每 `(user, app)` 一个 active 账号"能用单条部分唯一索引表达。当前单 App 下 `app_id` 恒为 `'zhaoxi'`,`(user, app_id)` 索引行为等价于 `(user)`,但形态已是终态、Phase 2 无需重建索引。**解析器 App 化(`get_or_create_account_for_user_in_app`)与 `app/apps.py` 注册表仍属 Phase 2,本次不做**;本次仅保证新账号/新绑定写入 `app_id='zhaoxi'`。

### 9.3 最终执行步骤(依赖有序)

**Group A — 收敛 + app_id 打底(DB 先行)**

- **A1 迁移 m0022 `_migration_0022_account_app_id`**:`accounts` 与 `account_owner_bindings` 各 `ADD COLUMN app_id TEXT NOT NULL DEFAULT 'zhaoxi'`(自动 backfill 存量为 `zhaoxi`);owner_bindings 的 `app_id` 亦可从 `accounts` JOIN 回填校验。跨后端 DDL 参照 m0004/m0021 范式。
- **A2 迁移 m0023 `_migration_0023_owner_binding_active_unique`**:`CREATE UNIQUE INDEX ux_owner_binding_active_user_app ON account_owner_bindings(platform_user_id, app_id) WHERE status='active';`(SQLite/PG 均支持部分索引;S1 已验证无冲突)。
- **A3 写入 app_id**:`create_ai4all_account_for_user`(`billing.py`)建账号/建 owner_binding 时写 `app_id='zhaoxi'`。
- **A4 收敛 ≤10(S5,`billing.py:2090`)**:移除 `if existing_count >= 10` 软检查,create 前若该 `(user, 'zhaoxi')` 已有 active 账号则直接拒绝;唯一索引兜底,`IntegrityError` 翻译成清晰报错。`get_or_create_default` 无害(仅无账号时进 create)。
- **A5 解析器告警(S6,`get_active_bound_account_for_user_in_app`)**:返回前按 `(user, app)` `COUNT`,`>1` 打 `warning`(收敛后恒为 1,用于探测异常)。
- **A6 删 web_create_agent(S4,`web.py:949`)**:删 handler + `WebCreateAgentRequest` 模型 + 路由注册;保留 `create_ai4all_account_for_user` 本体。
- **A7 debug 建号(Q3,`debug.py:695`)**:保留能力;命中唯一索引时返回清晰 409(而非 500)。

**Group B — channel 改名 native(正交,可与 A 并行)**

- **B1 迁移 m0024 `_migration_0024_rename_channel_app_to_native`**:`UPDATE channel_bindings SET channel='native' WHERE channel='app';`(生产 1 行)。审查其余 4 表确认无 `'app'`(已验证)。
- **B2 代码**:`channels.py:23` `CHANNEL_APP` 常量值 `"app"` → `"native"`(变量名可保留 `CHANNEL_APP`,零波及引用点:`turn_service.py:18/907`、`app_api.py:28/187/245/318/326` 均引用常量而非字面量)。`CHANNELS` 表 key 同步。

**Group C — 渠道化人设 Phase1(正交;原则一:微信字节不变)**

- **C1 L3 reply_presentation**:`ChannelCapability` 加 `reply_presentation`(weixin/native/web);`prompt_builder._OUTPUT_DIRECTIVES_FIXED` 改为按 key 取的字典;`assemble(reply_presentation=...)` 默认 `weixin`(默认字节不变);`turn_service:649` 传 `cap.reply_presentation`。
- **C2 L1 传输三行剥离**:`data/system/AGENTS.md`/`TOOLS.md` 的传输三行(微信好友/24h/只能发回微信)从共享人设移出 → 组装期按 channel 注入的"传输事实块";weixin 块还原原文,**微信渲染快照等价**(快照测验收)。
- **C3 L2 IDENTITY 播种中性化**:`user_profiles._default_user_context_templates` 去"在微信里/微信好友";`read_agent_context`/`ensure_agent_context_files` 加 channel 参数;微信默认保持现文。
- **C4 L4 名字兜底**:`app_create_session`(`app_api.py:184`)传账号真实名;`zhaoxi` 默认名 `"朝夕"` **仅新建且无名时**写入,不覆盖用户已起的名。全 App 一致显示同名。

### 9.4 测试方案(交付前)

- **收敛**:同 `(user, 'zhaoxi')` 二次建号被唯一索引拒 + 清晰错;`get_first_active` `>1` 告警路径;m0022/m0023 幂等(SQLite + PG 双跑);`web_create_agent` 已删(404)。
- **channel 改名**:m0024 后全表无 `'app'` 残留;`app_api` 端到端一轮(建 session→发消息→回包)。
- **人设**:App 组装 prompt 的 L1/L2/L3 **不含"微信"**、自称 **"朝夕"**;**微信 prompt 逐字节快照与改前一致**(原则一硬验收线)。
- **回归**:`make test-fast` 全量 + 聚焦 `test_turn_service` / `prompt_builder`;触及 schema/billing 故建议提交前 `make test` + `make test-pg`。

### 9.5 上线顺序与回滚

- 顺序:先 DB 迁移(m0022→m0023→m0024,`init_db` 顺序应用)→ 部署代码。迁移与代码同一次发布内完成;唯一索引在 A1 之后、A4 代码之前生效不影响正常登录链路(`get_or_create` 不新建)。
- 回滚:代码回滚即可(唯一索引/新列对旧代码兼容——旧代码不读 `app_id`、不受 `native` 影响,因线上 `'app'` 仅 1 行已迁走)。迁移不做 down(项目惯例:仅前向)。
- 双机：aliyun1 + aliyun2；aliyun2 当前为厚 node，直连中心 PG 并本地执行 turn，但 migration
  仍只由 central 执行一次。
