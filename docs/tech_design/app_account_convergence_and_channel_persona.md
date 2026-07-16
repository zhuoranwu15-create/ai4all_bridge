# App 接入:账号收敛(App 层身份) + 渠道化人设 — 调研与方案交接

> 状态:**设计/调研完成,待决策后实施**。本文自包含,可直接在线上机接续。
> 语言:中文(项目规范)。所有代码位置给到 `file:line`,均基于本次调研时的 `main`。

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
- App 流:`app/routers/app_api.py` `app_create_session`(`:172`)→ 同 phone → 同 `platform_user` → **同一个 `get_or_create_default_...`** → `get_first_active_account_for_user`(取最早 active owner_binding)。
- `get_or_create_default_ai4all_account_for_user`(`billing.py:2196`):有账号返回最早那个,**仅当一个都没有时才新建**。
- ⇒ **同手机号下,App 与微信落到同一个 `account_id`,长期记忆与人设「今天就已经打通」**。这是现状,不是待建功能。

### 1.3 "一手机号最多 10 个 agent" 的真相
- 唯一出处:`app/db/billing.py:2090-2091`,`create_ai4all_account_for_user` 内的**硬编码字面量** `if existing_count >= 10: raise ValueError(...)`。全仓库无第二处、无 config、无文档。
- 正常登录链路(微信/App/web 都走 `get_or_create_default`)**永远碰不到它**(有账号即返回,不新建)。
- 能累积到多账号的入口只有两个:
  - `app/routers/web.py:948` `POST /web/agents`(`web_create_agent`,自定义 agent_name/role_prompt)。
  - `app/routers/debug.py:695` `debug_create_account`(调试)。
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

**S6. 解析器显式化(`get_first_active_account_for_user`,`billing.py:2158`)**
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

收敛(A):
- [ ] **S1** 线上 PG 跑审计 SQL(§4),记录是否存在多账号 user
- [ ] **S2** 如有:写 `scripts/` 规范化脚本(规则见 §4),人工审阅后执行
- [ ] **S3** `_MIGRATIONS` 加迁移:owner_bindings 部分唯一索引
- [ ] **S4** 处理 `web_create_agent`(依 Q1)
- [ ] **S5** 收敛 `billing.py:2090` 的 ≤10 → ≤1
- [ ] **S6** `get_first_active_account_for_user` 加 >1 告警
- [ ] 测试:同 user 二次建号被拒;审计脚本干跑;解析器告警

渠道化人设(问题 A,可并行):
- [ ] L3 `reply_presentation`(channels + prompt_builder + turn_service),微信默认快照不变
- [ ] L1 传输三行剥离为 channel 传输块,微信渲染快照等价
- [ ] L2 IDENTITY 播种加 channel 参数,微信默认不变
- [ ] L4 名字兜底(app_create_session 传账号真实名,"朝夕"仅新建无名时)
- [ ] 测试:App 组装 prompt 的 L1/L2/L3 不含"微信"、自称"朝夕";微信 prompt 与改前逐字节一致快照

Phase 2(按需,Q2 决定是否提前):
- [ ] `accounts.app_id` 列 + backfill zhaoxi
- [ ] App 化解析器 + `app/apps.py` 注册表
- [ ] `AGENTS.<app_id>.md` 变体机制

---

## 8. 关键不变量与红线(实施时守住)
- **按 `account_id` 隔离是核心不变量**:任何未按 account 约束的 DB 查询/文件写入都是 bug。
- **收敛不删记忆**:规范化只 archive 非规范 owner_binding,绝不删账号/记忆。
- **原则一(微信零影响)**:所有渠道差异化默认值回落微信原文;微信主链路渲染结果字节级不变(以快照测把关)。
- schema 改动走 `_core.py` 的 `_MIGRATIONS`,不用启动期 `_ensure_column`。
