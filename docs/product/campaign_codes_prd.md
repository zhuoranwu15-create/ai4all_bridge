# 产品专题 PRD：内容创意营销活码 → Onboarding 差异化策略

更新时间：2026-07-07

## 0. 一句话定位

支持运营为每个"内容创意营销"投放渠道创建一个专属活码，新用户经活码注册后，onboarding 阶段按该活码预设的策略（使命分配 / onboarding 话术 / SOUL 人设）走差异化流程，而不是所有新用户走同一套默认流程。

## 1. 背景与定位

- 现有个人分享邀请码（`referral_codes`，见 [`entitlement_growth_prd.md`](./entitlement_growth_prd.md) 拉新奖励章节）解决的是"老用户拉新 + 奖励"问题：一人一码、邀请关系、给邀请人发贝壳奖励。
- 本 PRD 的"活码"特指**营销活动码**，是完全独立的业务实体：一个活码对应一个营销投放渠道（如某条短视频、某篇内容），可被大量陌生新用户使用，**不产生邀请关系，不触发拉新奖励**。
- 两者仅在"生成短码 + 落地页 query 参数校验 + 记录归因"这类实现套路上可以互相借鉴，业务语义、数据表、奖励逻辑完全不复用。
- 现状盘点（本 PRD 提出前）：
  - `referral_codes.code_type` 字段预留了扩展位（当前全仓库只用到 `'personal'` 一个值），`created_by_admin_user_id` 字段存在但从未有写入路径——说明"渠道活码"曾被设想但从未实现。
  - `accounts` 表没有任何来源渠道/campaign 字段。
  - Mission 分配（`app/mission_assignment.py::_pick_mission_id`）目前是 `sha256(account_id) % 模板数` 的纯哈希随机，无业务条件分支。
  - Onboarding 话术（`app/onboarding.py::build_onboarding_prompt_context`）只按 onboarding 状态机的 `state` 分支，无渠道维度。
  - SOUL 人设（`app/user_profiles.py`）已有预设模板机制（`_SOUL_TEMPLATES`：blank/xiaotaiyang/xiaoyueya/ju，另有 chaochao/xixi 两个未启用的模板文件），当前由用户在 onboarding 对话中"自己选"，LLM 抽取回答后 `apply_soul_preset` 写入。

## 2. 目标

- 支持运营在后台（前端 UI）创建/编辑/查看多个营销活码，各自独立配置：指定使命（mission）、指定 onboarding 话术变体、指定 SOUL 人设预设，三者互相独立可选。
- 新用户通过活码链接注册时，记录来源活码归因。
- Onboarding 阶段读取该归因，触发对应的差异化策略。
- 基础转化可观测：每个活码的使用次数计数。

## 3. 非目标（本期不做）

- 活码不触发拉新奖励（贝壳等），与个人邀请码体系完全解耦，不改动 `entitlement_growth_prd.md` 描述的拉新奖励逻辑。
- 不做批量生成活码的 API，运营在 UI 上手工逐个创建。
- 不做 AB 流量配比 / 权重分配 / 复杂数据看板，仅保留 `used_count` 基础计数。
- 不在本 PRD 内确定具体新增的 SOUL 预设模板内容——运营后续会重新设计，本 PRD 只要求模板注册机制支持"新增变体"这件事本身成本足够低（已确认现状改动量小，不涉及状态机改造）。

## 4. 数据模型

### 4.1 新表 `campaign_codes`

| 字段 | 说明 |
| --- | --- |
| `code` | 唯一活码字符串，随链接携带 |
| `campaign_key` / `name` | 后台辨识用的活动名称（如"618种草视频A"） |
| `status` | `active` / `disabled`，管理员可手动提前下线 |
| `valid_from` / `expires_at` | 默认 `created_at` + 3 个月，创建时可覆盖 |
| `mission_id` | 可空。为空则新用户回退现有哈希随机分配，不影响老逻辑 |
| `onboarding_script_variant` | 可空，指定 onboarding 话术变体 key |
| `soul_preset_key` | 可空，指定 SOUL 人设预设 key |
| `ai_name_preset` | 可空，强制 AI 的名字（注册/onboarding 时写入 `IDENTITY.md`）；见技术设计 §4.4（migration 19） |
| `used_count` | 累计使用次数 |
| `created_by_admin_user_id` / `created_at` / `updated_at` | 审计字段 |

四个策略字段互相独立、各自可选（管理员可以只配 mission，不配 soul，onboarding 话术走默认，AI 名字走默认）。

### 4.2 新表 `account_campaign_attribution`

`account_id` 唯一，注册时一次性写入：
- `campaign_code`
- 当时解析出的**策略快照**：`mission_id` / `onboarding_script_variant` / `soul_preset_key` / `ai_name_preset`

**关键设计原则：快照而非实时关联查询。** 活码后续被管理员编辑或下线，不应该影响已经归因 / 正在 onboarding 中用户的既定策略——只有新注册用户才会看到活码的最新配置。这与 mission 系统已有的"账号只存 mission_id，一经分配不可更改"原则一致（见 [`agent_self_prd.md`](./agent_self_prd.md)）。

## 5. 入口链路

Web 端新增独立 query 参数 `?campaign_code=xxx`，与现有 `?invite_code=xxx`（个人邀请码）机制并存、互不冲突、各自独立归因——一个用户理论上可以同时携带朋友的邀请码和某条广告的活码进来，两套归因分别记录。

## 6. Onboarding 差异化落地

- **Mission 分配**：`mission_assignment.py::_pick_mission_id` 增加前置判断——账号归因快照里有 `mission_id` 就直接用，否则回退现有哈希随机逻辑。
- **Onboarding 话术**：`onboarding.py::build_onboarding_prompt_context` 在现有按 `state` 分支的基础上，叠加按 `onboarding_script_variant` 的分支，取不同文案模板。
- **SOUL 人设**：**强制覆盖**——若归因快照里存在 `soul_preset_key`，onboarding 对话流程**跳过"让用户自己选人设"这一问**，程序直接调用 `apply_soul_preset` 写入，用户对这个选择过程无感知。若归因快照为空，走现有"用户在对话中自主选择"的流程不变。

## 7. 有效期与失效处理

默认有效期 3 个月，创建时可修改。活码到期或被设为 `disabled` 后：新的点击 / 注册按"无归因"处理，走系统默认策略，不阻断注册流程；已经归因的老用户不受影响（策略已快照落地，与活码当前状态无关）。

## 8. 后台管理

需要新增一个前端管理界面（UI），供运营创建 / 编辑 / 查看活码列表、配置三项策略、启停活码、查看 `used_count`。

> **修正（技术方案 + 实现阶段确认）**：`app/static/moderation_admin.html` + 共享 `admin.js` 已是本仓库既有的原生 JS 管理界面先例，无需新增前端构建工具链；本 PRD 撰写时误判"无前端管理界面先例"。另外，`app/static/home.html`（登录/落地页）与 `app/static/onboarding.html`（手机号注册 + 微信绑定页）就是本仓库内实现的、面向 C 端用户的真实注册前端——不在生产 nginx 单独部署的另一个仓库里，§5 的 `?campaign_code=xxx` 采集逻辑因此也在本仓库范围内实现，见技术方案 §3。

## 9. 开放事项（跟踪，非阻塞）

- 新增 SOUL 预设模板的具体内容 / 数量，运营后续单独设计确认。
- 管理后台 UI 的技术栈选型与挂载方式，进入技术方案阶段时单独对齐。
