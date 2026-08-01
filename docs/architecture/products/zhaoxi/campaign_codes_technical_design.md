# 技术方案：内容创意营销活码 → Onboarding 差异化策略

更新时间：2026-07-07

对应 PRD：[`docs/products/zhaoxi/capabilities/campaign_codes_prd.md`](../../../products/zhaoxi/capabilities/campaign_codes_prd.md)

> 后续扩展：用户在个人中心创建自由角色模板，并生成“个人邀请码 + 模板活动码”组合链接的技术方案见
> [用户自建角色模板与邀请链接技术设计](creator_role_template_referral_link_technical_design.md)。该方案复用本设计的
> `campaign_code` 参数、失效回退、通用归因和漏斗，但使用独立 owner-aware 角色资产，避免改变本文
> 运营活码的 Admin 权限、手工 code、3 个月默认期和静态 preset 语义。

## 0. 范围确认

活码归因只发生在 **web 注册路径**（`create_ai4all_account_for_user`），与个人邀请码（`referral_codes`/`invite_code`）范围一致。`get_or_create_session`（纯微信消息入站、账号 upsert）不改动——它服务的是"账号已存在，确保 session 存在"，账号本身的首次创建走的是 web 注册流程。

活码与个人邀请码是两套独立业务：不共表、不触发拉新奖励逻辑，仅在"短码生成 + 校验 + 记录归因"的实现套路上可借鉴。

## 1. 数据模型（migration 13）

`app/db/_core.py` 的 `_MIGRATIONS` 当前最新是 `(12, _migration_0012_agent_mission)`。新增：

```python
_MIGRATIONS = [
    ...,
    (12, _migration_0012_agent_mission),
    (13, _migration_0013_campaign_codes),
]
```

```sql
CREATE TABLE IF NOT EXISTS campaign_codes (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    campaign_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    valid_from TEXT,
    expires_at TEXT,
    mission_id TEXT,
    onboarding_script_variant TEXT,
    soul_preset_key TEXT,
    used_count INTEGER NOT NULL DEFAULT 0,
    created_by_admin_user_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
);
CREATE INDEX IF NOT EXISTS ix_campaign_codes_status ON campaign_codes(status);

CREATE TABLE IF NOT EXISTS account_campaign_attribution (
    account_id TEXT PRIMARY KEY,
    campaign_code TEXT NOT NULL,
    mission_id TEXT,
    onboarding_script_variant TEXT,
    soul_preset_key TEXT,
    attributed_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))),
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);
CREATE INDEX IF NOT EXISTS ix_account_campaign_attribution_code ON account_campaign_attribution(campaign_code);
```

关键设计原则：`account_campaign_attribution` 存的是注册时刻解析出的**策略快照**，不是实时 join `campaign_codes`。活码后续被管理员编辑或下线，不影响已经归因 / 正在 onboarding 中账号的既定策略——只有新注册账号才会看到活码的最新配置。`campaign_codes` 本身（配置源）允许管理员编辑，"不可变"只针对归因快照这张表，这点和 `account_mission`（一经分配不可更改）不同，不要混淆两者的可变性语义。

`code`（活码字符串）由管理员在创建时**手动输入**（如 "618A"，需要拼进营销链接，要求可读好记），非系统随机生成；唯一性冲突返回 409。

`expires_at` 未显式传入时，默认取 `created_at + 3 个月`（`campaign_codes_prd.md` §4.1 的确认要求；`valid_from` 未传则视为"创建即生效"，无需额外落默认值）。

## 2. `app/products/zhaoxi/infrastructure/persistence/campaign.py`（新模块，参照 `app/products/zhaoxi/infrastructure/persistence/mission.py` 风格）

```python
create_campaign_code(*, code, campaign_key, status="active", valid_from=None, expires_at=None,
                      mission_id=None, onboarding_script_variant=None, soul_preset_key=None,
                      created_by_admin_user_id) -> dict
get_campaign_code(*, code) -> Optional[dict]
list_campaign_codes(*, status=None, limit=100) -> List[dict]
update_campaign_code(*, code, **fields) -> dict
validate_campaign_code(*, code) -> dict   # {valid, reason?, mission_id, onboarding_script_variant, soul_preset_key}
increment_campaign_code_used(*, code) -> None
write_campaign_attribution(*, account_id, campaign_code, mission_id, onboarding_script_variant, soul_preset_key) -> None
get_campaign_attribution(*, account_id) -> Optional[dict]
```

`create_campaign_code`/`update_campaign_code` 校验：
- `code`（仅创建时）必须匹配 `^[A-Za-z0-9_-]{1,64}$`——限定 URL-safe 短码字符集，否则 400。**这不只是格式洁癖**：`code` 是运营在管理 UI 上手工输入的自由文本，会被拼进活码列表页的 HTML（`data-code` 属性）；限定字符集在数据源头堵住了引号/反斜杠类字符进入前端渲染的可能，是 XSS 防护的第一道防线（第二道见 §5.2 前端渲染方式）；
- `status` 必须是 `"active"`/`"disabled"` 之一，否则 400（API 层用 pydantic `Literal` 提前挡掉，DB 层再兜底校验，供脚本等非 API 调用方复用）；
- `valid_from`/`expires_at` 若提供必须匹配 `'YYYY-MM-DD HH:MM:SS'`（与 `beijing_now_str()` 落库格式一致），且不能 `valid_from > expires_at`（`update_campaign_code` 校验时按"本次更新字段 + 现有值"合并后的有效窗口比较，而非只看本次传入的字段）；
- `mission_id` 非空时必须在 `mission_registry.MISSION_TEMPLATES` 白名单内，否则 400；
- `soul_preset_key` 非空时必须在 `user_profiles._SOUL_TEMPLATES` 白名单内，否则 400；
- `onboarding_script_variant` **不做白名单校验**——它是运营自由填写的文本内容（见第 4 节说明），不是预注册的枚举 key。

`create_campaign_code` 唯一性冲突（`code` 重复）返回 409。

`code` 重复上述两项校验（`mission_id`/`soul_preset_key`白名单）在创建/编辑时就拦截，不留到 onboarding 运行时才 fallback——这是吸取 `_resolve_persona_preset` 现有实现"未知值悄悄 fallback 成 blank"这个坑的教训。

## 3. Web 注册入口改造

> **实现阶段修正**：`create_ai4all_account_for_user` 实际的两个"新用户注册"调用点是
> `get_or_create_default_ai4all_account_for_user`（内部在账号不存在时才调用
> `create_ai4all_account_for_user`），真正被 `/web/register-and-binding-intent`、`/web/login`
> 两个 handler 直接调用。`/web/register` 本身**不创建账号**（只注册 `platform_user`），
> `/web/agents`（`WebCreateAgentRequest`）是登录后"新建一个分身"的接口，语义上不是
> "新用户注册"，故不接入活码。以下按实际调用链描述，取代原稿基于错误行号的假设。

- `WebRegisterAndBindingIntentRequest`、`WebLoginRequest`（`app/routers/web.py`）新增 `campaign_code: Optional[str] = None`；`WebRegisterRequest` 不加（该接口不创建账号，加了也无处消费）。
- `/web/config` 的 `registration` 块新增 `"campaign_code_param": "campaign_code"`，与已有 `invite_code_param` 并列。
- `get_or_create_default_ai4all_account_for_user` 新增 `campaign_code` 形参，透传给 `create_ai4all_account_for_user`。
- `create_ai4all_account_for_user`（`app/db/billing.py`）新增 `campaign_code: Optional[str] = None` 形参：在账号创建事务提交、`grant_new_user_shells`/`retry_qualified_referral_rewards_for_user` 等既有的提交后最佳努力步骤之后（与它们同一模式，非强一致事务），追加：
  1. `validate_campaign_code(code=campaign_code)`；
  2. **校验失败（不存在 / 已过期 / 已 disabled）时静默失败**——记一条 warning/error 日志，注册流程正常继续，只是不写归因（不像 `invite_code` 那样返回 400 硬拒绝）。活码没有金钱奖励含义，不应因为一个过期的营销码挡住真实注册；
  3. 校验通过则写 `account_campaign_attribution` 快照 + `increment_campaign_code_used`；
  4. 若快照里 `soul_preset_key` 非空，**立即**调用 `apply_soul_preset(account_id, soul_preset_key)`（见第 4 节）。
- `web_register_and_binding_intent` / `web_login` 两个 handler 分别把 `payload.campaign_code` 透传进 `get_or_create_default_ai4all_account_for_user`。

## 4. Onboarding 差异化落地

### 4.1 Mission 分配

`mission_assignment.py::assign_mission_if_absent` 内部新增前置判断：先 `get_campaign_attribution(account_id)`，若存在且 `mission_id` 非空，直接用；否则走现有 `_pick_mission_id` 哈希逻辑。调用方 `turn_service.py:1204` 不需要改（保持模块既有约束"两处都只应调用 `assign_mission_if_absent`"）。

### 4.2 Onboarding 话术——自由文本 override

`onboarding_script_variant` 是运营在 UI 上**自由填写的引导文本**，不是工程师预注册的枚举变体——因为营销创意数量可能"很多个"，做成枚举每次都要开发加代码部署，不现实。

`build_onboarding_prompt_context`（`onboarding.py:96-187`）新增 `onboarding_script_override: Optional[str] = None` 形参：在对应 `state` 分支的既有文案之后追加这段文本（不替换既有规则文案，只叠加营销场景专属的引导语）。`turn_service.py` 调用处新增一次 `get_campaign_attribution` 查询并透传 `onboarding_script_variant` 字段值。

### 4.3 SOUL 人设——强制覆盖

若账号归因快照里 `soul_preset_key` 非空：
1. 在 `create_ai4all_account_for_user` 写入归因快照后**立即**调用 `apply_soul_preset(account_id, soul_preset_key)`——账号从第一条消息起就是目标人设，不必等到 onboarding STEP2 对话轮次；
2. `build_onboarding_prompt_context` 在 `ONBOARDING_STEP2_SENT` 分支下新增判断：若账号有强制人设（新增 `has_forced_soul_preset: bool` 形参），只问 AI 名字，跳过"选人设"这部分话术；
3. `apply_extracted_onboarding_info`（`onboarding.py:297-380`）里 `persona` 应用分支对这类账号短路——即使 LLM 从用户回复里抽出了 persona，也不再次调用 `apply_soul_preset` 覆盖强制值。

`turn_service.py` 需要在调用 `build_onboarding_prompt_context`/`apply_extracted_onboarding_info` 前各查一次归因，透传 `has_forced_soul_preset`（可与 4.2 的归因查询合并为一次调用，结果复用）。

### 4.4 AI 名字——强制指定（migration 19）

第四个策略旋钮 `ai_name_preset`（活码 + 归因快照各加一列，见 migration 19；14–18 为空号/废弃，不再回填）。它是运营在 UI 上**自由填写的短字符串**（AI 的名字，如"小满"），不是白名单枚举；校验只做 strip + 单行 + 长度上限 24（`campaign.py::_normalize_ai_name_preset`），防止多行文本注入 IDENTITY.md/SOUL.md。

若账号归因快照里 `ai_name_preset` 非空：

1. `apply_campaign_code_attribution` 写归因快照后**先** `write_ai_name_to_identity(account_id, ai_name_preset)`、**再** `apply_soul_preset`——顺序不能反：`render_soul_preset` 会从 IDENTITY.md 读 AI 名字拼进人设自称，反了则强制人设首轮自称仍是"我"；
2. `build_onboarding_prompt_context`/`next_onboarding_state`/`apply_extracted_onboarding_info` 新增 `has_forced_ai_name: bool` 形参，onboarding 跳过"问 AI 名字"这一步，且用户回复里抽出的 ai_name / 预设默认名回填都对这类账号短路，不覆盖已定死的名字。

**四种组合的 onboarding 行为**（user_name 始终问，活码只覆盖 AI 侧身份）：

| soul 强制 | ai_name 强制 | `step1_sent` 之后问什么 | 状态推进 |
|:---:|:---:|---|---|
| ✗ | ✗ | AI 名字 + 人设菜单（默认） | → step2 |
| ✓ | ✗ | 只问 AI 名字（§4.3） | → step2 |
| ✗ | ✓ | 只给人设菜单，不问 AI 名字（名字-only 简化处理） | → step2 |
| ✓ | ✓ | AI 身份全定：确认用户称呼 + 以强制身份自我介绍 + 破冰 | **step1_sent → complete**（少问一轮） |

两者都强制时 `next_onboarding_state` 让 `step1_sent` 直接跳 `complete`——收到用户称呼后已无 AI 相关问题可问，本轮即收尾并触发使命分配。

调试面板 reset（`debug.py::debug_reset_onboarding`）重建默认文件后，按归因快照**同序重放** `ai_name_preset` + `soul_preset_key`（先 IDENTITY 名字再 SOUL），返回 `restored_ai_name`/`restored_soul_preset`，避免重跑模拟漂移到空白模板。

## 5. Admin API + 管理 UI

### 5.1 API

新 router `app/products/zhaoxi/api/admin_campaigns.py`：

- `POST /admin/campaign-codes`（创建）
- `GET /admin/campaign-codes`（列表，支持 `status` 过滤）
- `PATCH /admin/campaign-codes/{code}`（编辑字段 / 启停）

参照 `admin_security.py` 的 Pydantic 请求模型 + handler 范式（`PlaintextGrantRequest` / `admin_create_plaintext_grant` / `admin_list_plaintext_grants`）。

鉴权：`Depends(require_admin_or_staff_user)`（admin + staff 均可读写，创建时记录 `created_by_admin_user_id`）——运营 staff 应该能自主建活码，不必每次找 admin。

> **实现阶段修正**：本模块不直接 `from app.config import settings`（认证/DB 依赖都在 `app.routers.deps`/`app.db` 里已绑定），故 `tests/conftest.py` 无需追加 per-module patch——与 `admin_accounts.py`/`admin_security.py` 同理，两者也未被 patch。

### 5.2 管理 UI

复用仓库已有先例：`app/static/moderation_admin.html` + 共享 `admin.js`（`apiFetch`/`getToken` 封装 Bearer token 鉴权，`localStorage` 存 token），通过 `StaticFiles` 直接挂载在 `/ui`、`/ops` 前缀下，无构建工具链。

新增 `app/static/campaign_codes_admin.html`，复用 `admin.js`，原生 JS + `esc()` 拼 HTML 渲染列表/表单——不引入 React/Vue/webpack（引入前端构建链是本仓库从未做过的事，成本显著更高，不在本期范围）。

页面含「新建」「编辑」两个表单卡片 + 列表：编辑覆盖 `campaign_key`/`status`/`mission_id`/`soul_preset_key`/`valid_from`/`expires_at`/`onboarding_script_variant` 全部可编辑字段（PRD §8 要求的"创建 / 编辑 / 查看 / 配置三项策略 / 启停"）。

> **实现阶段修正（codex review 发现）**：列表行的操作按钮最初把 `esc(c.code)` 拼进单引号包裹的内联 `onclick` 属性——但 `admin.js` 的共享 `esc()` 只转义 `& < > "`，不转义单引号/反斜杠，而 `code` 是运营手工输入的自由文本，理论上可构造出打破属性边界的值来对其他登录 admin/staff 执行 JS（同站权限提升）。修复采用双重防线：① `code` 建时限定 `^[A-Za-z0-9_-]{1,64}$`（见 §2），从数据源头排除引号/反斜杠；② 前端改为 `data-code` 属性 + `tbody` 上的单个委托事件监听器（`e.target.closest('button[data-code]')`），不再把任何字段值拼进内联 JS 字符串——即使①的字符集校验将来被绕过，②也独立生效。`admin.js` 的共享 `esc()` 本身未改动（其余页面对 id 的用法均是系统生成的安全格式，改动它超出本次修复的最小范围）。

### 5.3 C 端注册页 `?campaign_code=xxx` 采集

> **修正**：面向 C 端用户的真正注册/登录页面**就在本仓库**——`app/static/home.html`（登录/落地页，调用 `/web/login`）与 `app/static/onboarding.html`（手机号注册 + 微信绑定页，调用 `/web/register-and-binding-intent`）。生产 nginx 只是把这两个静态页面部署在 `/`、`/user/*` 路径，不是另一套前端代码。原稿"不在本仓库、需要另一个前端仓库配合"的判断有误，已在实现阶段修正并落地：

- `home.html`：`S.campaignCode` 状态字段，`initCampaignCode()` 在 `window load` 时从 `?campaign_code=` 读取（与既有 `initInviteCode()` 并列），`doLogin()` 组装 `payload.campaign_code`。与 `invite_code` 不同，**不展示可见 UI 提示**（营销活码是链接静默携带的归因参数，不是用户手填的邀请码，无需可编辑/可见）。
- `onboarding.html`：`state.campaignCode`，`initCampaignCodeFromUrl()` 同样在 `window load` 时读取，`registerAndCreateBinding()` 组装 `payload.campaign_code` 传给 `/web/register-and-binding-intent`。
- 均不做大小写归一化（`campaign_codes.code` 存储时不转大写，与 `invite_code` 的 `_normalize_referral_code` 大小写不敏感行为不同，见 §2 `create_campaign_code` 校验规则）。

## 6. 测试计划

- `app/products/zhaoxi/infrastructure/persistence/campaign.py` 单测：CRUD、`validate_campaign_code` 各分支（不存在 / 过期 / disabled / 正常）、归因写入唯一性、`mission_id`/`soul_preset_key` 白名单校验、`code` 字符集校验、`status` 枚举校验、`valid_from`/`expires_at` 格式与顺序校验、默认 3 个月有效期计算。
- `admin_campaigns.py`：非法 `status` 走 pydantic `Literal` 返回 422；非法 `code` 字符集走 DB 层校验返回 400；PATCH 编辑多字段（非仅启停）。
- 静态页面回归：`campaign_codes_admin.html` 不应再出现内联 `onclick="toggleStatus("` 拼接模式，应使用 `data-code` + 事件委托。
- `mission_assignment.py`：归因指定 mission 优先命中 vs 无归因回退哈希。
- `onboarding.py`：`build_onboarding_prompt_context` 追加 override 文本 + 强制人设账号跳过选人设问句；`apply_extracted_onboarding_info` 在强制人设账号下不应用 persona。
- `admin_campaigns.py` 路由测试仿照现有 admin 路由测试风格（含 staff token 可写、无 token / 错误 token 403）。
- Web 注册集成测试：四种 `campaign_code` 场景（有效 / 过期 / disabled / 不存在），确认无效场景下注册仍然成功、只是不写归因；`/web/register-and-binding-intent` 与 `/web/login` 两个真实调用点各覆盖一次。
- 静态页面文本断言：`home.html`/`onboarding.html` 均需断言已包含 `campaign_code` 采集与透传的关键代码片段（沿用既有 `test_home_carries_invite_code_to_login` 的断言写法）。
- 触及共享基础设施 + schema + onboarding/mission 跨模块契约，改完后按 CLAUDE.md 约定跑全量 `pytest`。

## 7. 上线注意

不需要存量账号回填脚本——活码只影响新注册账号。Migration 13 沿用现有双后端（SQLite/PG）方言翻译层写法（参照 migration 11/12），无需额外兼容处理。

## 8. 非目标（继承 PRD）

- 活码不触发拉新奖励（贝壳等），与 `entitlement_growth_prd.md` 描述的拉新奖励逻辑完全解耦。
- 不做批量生成活码的 API。
- 不做 AB 流量配比 / 复杂数据看板，仅 `used_count` 基础计数。
- 不引入前端构建工具链。
- 不在本方案内确定具体新增的 SOUL 预设模板内容——运营后续单独设计确认，本方案只保证 `_SOUL_TEMPLATES` 白名单校验机制就位。
