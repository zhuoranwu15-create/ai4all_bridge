# 后台管理说明

当前已有一个轻量 Web 后台，也保留 Admin API。Admin 最高权限使用 `ADMIN_TOKEN`，普通后台用户使用 `ADMIN_STAFF_TOKEN`。

Web 后台入口：

```text
http://127.0.0.1:8180/ui/
```

Web onboarding 入口：

```text
http://127.0.0.1:8180/ui/onboarding.html
```

## Admin 鉴权

所有 `/admin/*` 和 `/debug/*` 接口都需要 Admin 或 Staff Token：

```http
Authorization: Bearer <ADMIN_TOKEN>
# 或
Authorization: Bearer <ADMIN_STAFF_TOKEN>
```

本地 `.env` 中配置：

```bash
ADMIN_TOKEN=replace-with-a-strong-token
ADMIN_STAFF_TOKEN=replace-with-a-staff-token
```

默认开发值见仓库根目录 `.env.example`：

```bash
ADMIN_TOKEN=dev-admin-token
ADMIN_STAFF_TOKEN=
```

`ADMIN_TOKEN` 映射为 `role=admin`，可审批临时明文授权并直接调用明文接口；`ADMIN_STAFF_TOKEN` 映射为 `role=staff`，默认只能看脱敏视图，需要申请并获批临时明文授权后才可查看指定账号、指定资源的明文。

## 当前管理模型

新的目标模型以 AI4ALL 业务账号为核心：

```text
AI4ALL Account = 业务隔离账号，未绑定 legacy 入站可由 OpenClaw session_key fallback，绑定后使用预创建 aid_...（当前生成规则为 aid_ + 9 位数字）
Channel Account = OpenClaw / 微信通道侧账号或机器人账号
Channel Binding = AI4ALL Account 与 Channel Account / session_key 的绑定
session = AI4ALL Account 下的会话
profile = 该账号或会话对应的 Soul / 风格 / Prompt 配置
message = 收发消息记录
```

现有 DB/API 里仍有 `account_id` 旧命名。当前语义上应理解为 AI4ALL Account ID，不等同于 OpenClaw payload 原生 `account_id`。

## 注册与验证码配置

Web onboarding 的注册链路现在要求：

```text
阿里云图形验证码 -> 短信 OTP -> 一次性 otp_token -> /web/register-and-binding-intent -> 二维码
```

本地开发时，`APP_ENV=local` 且 Aliyun SMS/Captcha 配置为空会进入 mock 模式，OTP 会输出到 backend 日志。非 local/test 环境缺少 Aliyun 凭据会直接失败，不会静默跳过验证码或短信发送。

需要在 `.env` 中配置：

```bash
ALIYUN_ACCESS_KEY_ID=
ALIYUN_ACCESS_KEY_SECRET=
ALIYUN_SMS_SIGN_NAME=
ALIYUN_SMS_TEMPLATE_CODE=
ALIYUN_SMS_MAX_PER_PHONE_PER_HOUR=5
ALIYUN_CAPTCHA_SCENE_ID=
ALIYUN_CAPTCHA_PREFIX=
OTP_EXPIRES_MINUTES=10
OTP_TOKEN_EXPIRES_MINUTES=10
```

当前静态 onboarding 页面通过 `GET /web/config` 读取 Aliyun Captcha 的公开 `scene_id` / `prefix`。上线或切换环境时需要让 `.env` 中的 `ALIYUN_CAPTCHA_SCENE_ID`、`ALIYUN_CAPTCHA_PREFIX` 与控制台配置保持一致；服务端 AccessKey 和短信模板凭据不得暴露给前端。

## 查看账号

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/accounts
```

这里返回的是 AI4ALL Account。账号详情接口会包含 `channel_bindings`，用于查看对应的 Channel Account ID、session key、sender/chat 等通道身份。

## 查看账号详情

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/accounts/aid_123456789
```

详情接口返回账号基础信息、channel bindings、owner bindings、binding intents、wallet、proactive state 等排障信息。当前代码不再提供旧 `/admin/contacts/*` 路由；账号级管理统一走 `/admin/accounts/*`。

## 修改备注、显示名或限流

```bash
curl -X PATCH http://127.0.0.1:8180/admin/accounts/aid_123456789 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"测试账号","notes":"内部测试账号","daily_limit":100,"rpm_limit":10}'
```

当前支持字段：

- `display_name`
- `notes`
- `daily_limit`
- `rpm_limit`

自 m0031 起，`daily_limit/rpm_limit` 对有归属真人的账号是 **platform_user 级** override：从任一 resident/account 修改都会同步该真人全部 runtime account；传 `null` 表示恢复全局默认。无归属真人的孤儿账号仍保留 per-account 兼容。

当前支持的状态：

- `active`
- `disabled`

## 禁用或启用

禁用：

```bash
curl -X POST http://127.0.0.1:8180/admin/accounts/aid_123456789/disable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

启用：

```bash
curl -X POST http://127.0.0.1:8180/admin/accounts/aid_123456789/enable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

被禁用的记录再次发消息时，Backend 会返回 `status=disabled` 和 `no_reply=true`，不会调用 LLM。

## 修改 Profile

```bash
curl -X PATCH http://127.0.0.1:8180/admin/accounts/aid_123456789/profile \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'
```

当前支持字段：

- `display_name`
- `style`
- `system_prompt`
- `preferences`

Profile 当前会影响 LLM Prompt。后续会调整为更明确的账号级 Soul / Prompt 配置。

## 查看会话

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/sessions
```

## 查看会话详情

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/sessions/1
```

返回内容包括：

- 会话信息。
- Profile。
- 最近消息。

## 重置会话

```bash
curl -X POST http://127.0.0.1:8180/admin/sessions/1/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

这会删除该 session 下已保存的消息。

## 主动提醒 Scheduler

开发期手动测试优先使用 Proactive Debug 后台：

```text
http://127.0.0.1:8180/ui/proactive_debug.html
```

该页面可以选择账号、开启 proactive state、模拟入站、让 pending reminder 到期、运行 scheduler、生成/提升/清理 account check draft，并在单账号 `Run Proactive Check` 后展示内容邀请是否生成及原因。

查看或更新某个账号的 proactive state：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/proactive-state

curl -X PATCH \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/proactive-state \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "next_scan_at": "2000-01-01 00:00:00",
    "metadata": {
      "source": "manual-test",
      "account_check_candidate": {
        "id": "manual-1",
        "text": "记得关注一下事情 B。",
        "source": "manual-test"
      }
    }
  }'
```

`next_scan_at` 和 `cooldown_until` 支持 ISO datetime 或 `YYYY-MM-DD HH:MM:SS`，会规范化为 `YYYY-MM-DD HH:MM:SS` 存入 SQLite。

手动触发隐藏 LLM 账号主动检查候选生成：

```bash
curl -X POST \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/proactive-check-candidate-draft \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

这个接口只会把结果写入 `metadata.account_check_candidate_draft`。它不会写入会触发发送的 `metadata.account_check_candidate`，也不会主动发微信。若 LLM 置信度低于 `PROACTIVE_ACCOUNT_CHECK_MIN_CONFIDENCE`，会返回 no-op 并清理旧 draft。

人工确认 draft 可发送后，再把它提升为 active candidate：

```bash
curl -X POST \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/proactive-check-candidate-draft/promote \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

promote 会把 `metadata.account_check_candidate_draft` 移到 `metadata.account_check_candidate`，并写入 `account_check_candidate_promoted_at`。只有提升后的 `account_check_candidate` 才会被 scheduler 的账号主动检查视为可发送候选。

如果 draft 不合适，可以直接清理：

```bash
curl -X DELETE \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/proactive-check-candidate-draft \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

清理只删除 draft，不会删除已经存在的 active `account_check_candidate`。

手动触发单账号账号主动检查，并查看是否生成内容邀请候选：

```bash
curl -X POST \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/proactive-check/run-once \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

返回体中的 `account_check` 包含本次候选发送决策和执行结果；`display.content_invitation_generated` 为 `true` 时会展示本次生成的内容邀请候选，为 `false` 时 `display.reason` 会说明未生成原因。

查看 scheduler 配置和运行状态：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/proactive/scheduler
```

查看 hidden extractor 写入的 follow-up commitments：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/commitments
```

取消不应发送的 commitment：

```bash
curl -X POST \
  http://127.0.0.1:8180/admin/commitments/com_example/cancel \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

手动触发一次 proactive run。当前会依次处理 due reminders、due commitments，再扫描 due proactive accounts。账号主动检查会调用非 LLM 决策函数：没有候选事项时返回 `no_candidate` 并跳过发送；如果 state metadata 里显式放入 `account_check_candidate`，且 route、quiet hours、daily limit 等策略通过，会通过 outbound ledger / Gateway 发送微信消息。

```bash
curl -X POST \
  'http://127.0.0.1:8180/admin/proactive/scheduler/run-once?limit=20' \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

账号主动检查发送成功后，系统会写入 `last_proactive_sent_at`，并把 active `metadata.account_check_candidate` 移到 `metadata.account_check_last_sent_candidate`，避免同一个候选被下一轮重复发送。

阶段性验证账号主动检查主动触达的最短路径：

1. 确认目标账号已经有真实微信入站消息和 `channel_bindings` route。
2. `PATCH /admin/accounts/{account_id}/proactive-state`，设置 `enabled=true`、`next_scan_at` 为过去时间。
3. `POST /admin/accounts/{account_id}/proactive-check-candidate-draft` 生成隐藏 LLM draft。
4. `GET /admin/accounts/{account_id}/proactive-state` 检查 `metadata.account_check_candidate_draft`。
5. `POST /admin/accounts/{account_id}/proactive-check-candidate-draft/promote` 人工确认提升。
6. `POST /admin/accounts/{account_id}/proactive-check/run-once` 调试单账号；或 `POST /admin/proactive/scheduler/run-once?limit=20` 触发 worker 同款扫描。
7. 若未命中 quiet hours / daily limit / route 缺失，应在微信收到主动消息，并在 `outbound_messages` 看到 `sent`。

最终联调 hidden commitment 的路径：

1. 确认目标账号有真实微信入站消息和可用 `channel_bindings`。
2. 开启该账号 proactive state：`enabled=true`。
3. 通过微信进行一轮普通聊天，内容中包含明确的未来后续事项；显式“提醒我”仍会走 reminder，不会重复抽取 commitment。
4. `GET /admin/accounts/{account_id}/commitments` 检查是否写入 `pending` commitment。
5. 到期后调用 `POST /admin/proactive/scheduler/run-once?limit=20`，或启动独立 worker。
6. 若未命中 quiet hours / daily limit / route 缺失，应在微信收到 commitment 主动消息，并看到 `outbound_messages.source=commitment`。

本地或生产也可以用独立 worker 跑循环：

```bash
.venv/bin/python scripts/run_proactive_scheduler.py
```

默认 `PROACTIVE_SCHEDULER_ENABLED=false`，FastAPI 不会自动启动 in-process scheduler。若要在 FastAPI 内启动，必须保证 `uvicorn --workers 1`；否则多个 worker 会重复扫描 due reminder / due account。账号主动 planning 间隔由 `PROACTIVE_PLANNING_INTERVAL_SECONDS` 控制。

## 本地 Debug API

`/debug/*` 接口仍保留给本地开发使用。它们同样需要 Admin/Staff Token，不应该暴露到公网。Debug 页面和 API 约定见 [调试指南](debugging.md)。

## 查看 raw payload

用于排查 OpenClaw 传入的原始上下文字段：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  'http://127.0.0.1:8180/admin/messages/raw?limit=5'

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/messages/27/raw
```

## 查看账号级 user_profile.md

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8180/admin/accounts/aid_123456789/user-profile
```

文件路径：

```text
data/user_profiles/<account_id>/user_profile.md
```

这里的 `<account_id>` 是当前代码历史命名，语义上是 AI4ALL Account ID。

## 常用排查命令

```bash
curl http://127.0.0.1:8180/health
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

Backend 日志由 uvicorn 输出。

OpenClaw 日志：

```bash
tail -120 ~/.openclaw/logs/gateway.log
tail -120 ~/.openclaw/logs/gateway.err.log
tail -120 ~/.openclaw/tmp/openclaw-501/openclaw-$(date +%F).log
```

## Companion World P1 发布运行手册

本节只适用于 M2-C。发布原则是：**先以 flag=false 部署和迁移，再导入模板、按固定截点 backfill、对账，最后才允许小流量开 flag**。正式四位角色内容和客户端最低版本必须由产品/客户端团队提供，运维不得临时编造。

注意：`COMPANION_WORLD_P1_ENABLED` 只门控 World API 与 auth 切换，**不是后台总开关**。L3 读/写/compact 由 `COMPANION_WORLD_L3_BACKGROUND_ENABLED` 独立控制，world-aware proactive 安全阀由 `COMPANION_WORLD_PROACTIVE_SAFETY_ENABLED` 独立控制。关闭 API flag 不会自动改动另外两项；完整边界以 [`../architecture/products/zhaoxi/companion_world_3_0_refactor_design.md`](../../../architecture/products/zhaoxi/companion_world_3_0_refactor_design.md) 顶部“接手说明”为准。

### 1. 发布前输入与备份

必须具备：

- 经运营签字的 UTF-8 JSON manifest，恰含 rank 1..4 四条模板；每条有稳定 `template_id`、名称、头像引用、简介、三个标签、非空 SOUL/IDENTITY persona 与 `persona_version`。
- 已确认支持 `/v1/auth/session` 返回 `account:null` 并立即进入 world bootstrap 的客户端最低版本。
- 客户端 PRD/技术说明已同步 D-05“同世界共享用户沉淀记忆”与 D-08“legacy resident 离开豁免”，不再保留相反口径。
- 生产 PG 备份和可恢复点；记录发布人、时间、代码 SHA。
- central/standalone 只运行一个 Dreaming scheduler。推荐独立 proactive 进程承担：`DREAMING_SCHEDULER_ENABLED=false`、`PROACTIVE_DREAMING_SCHEDULER_ENABLED=true`；纯 node 不运行 L3 compact。

在部署任何包含 m0025 钱包上迁的代码前，必须对**生产 PG**运行只读预检（本地 SQLite 结果不作发布依据）：

```bash
.venv/bin/python scripts/precheck_wallet_migration.py
```

命令读取当前部署环境的 `DATABASE_URL`，不要把含凭证的连接串直接写进发布记录或共享命令行。预期输出 `is_postgres = True`、退出码为 0/`PASS`，四类 BLOCK（`ambiguous_owner`、`orphan_wallet`、`owner_drift`、`primary_undefined`）均为 0；多钱包/多次赠权清单须归档到发布记录。任何 BLOCK 非零都必须先人工修数，禁止依赖 m0025 自动猜测归属。若生产已执行 m0025，仍须保存当前只读复核结果，但不得把迁移后的 PASS 倒推成“迁移前已预检”。

先确保：

```bash
COMPANION_WORLD_P1_ENABLED=false
COMPANION_WORLD_L3_BACKGROUND_ENABLED=false
COMPANION_WORLD_PROACTIVE_SAFETY_ENABLED=true
make test-unit
make test
make test-pg
git diff --check
```

### 2. flag=false 部署至 m0032

部署代码并重启后检查：

```bash
curl -fsS http://127.0.0.1:8180/health/ready
```

确认 `schema_migrations` 最大版本为 32。m0031 将真人级 `daily_limit/rpm_limit` 设为 canonical override 来源并把 reservation TTL 回收接到 central proactive scheduler；m0032 将 PG `rpm_hits.hit_at` 升为双精度，避免 60 秒滑窗误删。

此时 `/v1/worlds/*`、`/v1/conversations`、`/v1/ai-conversations/*` 应以 404 `not_found` 隐藏；旧 auth 与 legacy turn 路径保持原入口。完成 backfill 后，secondary legacy resident 的真人级 proactive 会被安全阀拦截；L3 因独立开关为 false 不读、不写、不 compact。不要在迁移后立刻开 API flag。

### 3. 导入四模板目录

脚本不会内置或打印 persona 正文；已发布 `template_id` 的内容不可原地修改，换版必须使用新 ID 并退休旧行。

```bash
.venv/bin/python scripts/import_companion_world_presets.py \
  /secure/path/companion_world_presets.json --dry-run

.venv/bin/python scripts/import_companion_world_presets.py \
  /secure/path/companion_world_presets.json
```

预期：实际导入报告 `errors=[]`、`catalog_ready=true`。再次执行应只出现 `keep_ids`，不得新增重复模板。manifest 放受控目录，不提交密钥或生产 persona 到临时日志。

**运营名池（m0049 / NAME-001，2026-07-26 起）**：每条模板可选带 `name_pool`（3–5 个已审核
实例名，去重、不含表情/控制字符）、`name_pool_version`（两者必须成对出现）、`long_summary`、
`persona_key`。这四项是**可原地更新的运营元数据**，不参与人设内容的不可变判定——生产四模板
早已上线、`template_id` 不能换，所以名池只能这样补配；被原地更新的模板会出现在报告的
`update_ids` 里。规则：

- 改名池必须同时换 `name_pool_version`，否则新老快照无法区分来源。
- 已快照过的候选**不会**因换名池而改名（`suggested_display_name` 只随首次 INSERT 落一次）。
- manifest 未提供的字段一律不动，重放一份不含名池的老 manifest 不会抹掉已配好的名池。
- `persona_key` 允许从空补上，但一旦非空就不许改值，否则报 `persona_key is immutable once assigned`。
- 不配名池不阻断任何流程：候选 `naming_status=unavailable`，客户端回落本地兜底名池。

### M4 Mailbox 签名 catalog（默认关闭）

M4 mailbox catalog 只接受运营已发布的 official/operations template，不在代码或导入器中生成角色、人设或来信正文。manifest 顶层必须严格为 `version=1`、`entries`、`signature`；`signature` 是去掉自身后，对 canonical JSON `{"entries":...,"version":1}`（UTF-8、key 排序、无多余空格）计算的 HMAC-SHA256 hex。签名必须由受控发布流程产生。

密钥只从 secret manager 注入，不写入 manifest、命令行参数、仓库或日志：

```bash
export COMPANION_WORLD_MAILBOX_MANIFEST_HMAC_SECRET='<from-secret-manager>'
```

先保持 `COMPANION_WORLD_MAILBOX_ENABLED=false`，执行验签 dry-run：

```bash
.venv/bin/python scripts/import_companion_world_mailbox_catalog.py \
  /secure/path/companion_world_mailbox_catalog.json --dry-run
```

预期 `errors=[]`；检查 `create_ids/keep_ids/retire_ids` 与运营签字清单完全一致后再 apply：

```bash
.venv/bin/python scripts/import_companion_world_mailbox_catalog.py \
  /secure/path/companion_world_mailbox_catalog.json --apply \
  --actor '<release-ticket-or-operator-id>'
```

apply 在单事务内 retire 旧 character 版本并创建新版本；已投递 letter 快照不改写，retired catalog id 不复活。重放应只出现 `keep_ids`。报告不会输出 letter body、签名或密钥。

Mailbox 使用独立 `COMPANION_WORLD_MAILBOX_ENABLED`。独立中心进程 `scripts/run_world_lifecycle_scheduler.py` 在 lifecycle evaluation=false、mailbox=true 时仍会运行 mailbox delivery/expiry；两个 flag 不互相代开。启用前至少核对：active resident `<8`、每世界 open letter `<=1`、最近投递已满 30 天、同 `character_key` 从未投递。关闭 flag 会同时隐藏 owner API并停止新投递/expiry；不会撤销已投递或未来已接受的关系。

接受来信只由 owner session 调用 `POST /v1/mailbox/letters/{letter_id}/accept`。后端会在 world 锁内重查精确 expiry、catalog/template 和 active `<10`，一次提交 runtime account、`origin='mailbox'` resident、conversation 与 accepted letter；重复调用返回同一 resident。该路径不会创建 owner binding、钱包、subscription/grant、App notification、outbound、欢迎消息或自动 turn。关闭 mailbox flag 只阻止后续 API/投递，不删除已接受 resident。

### 4. 固定 cutoff 并 backfill

在开始 backfill 前记录一个**北京墙钟、秒粒度且全程不变**的 cutoff。生产 PG 示例：

```bash
psql "$DATABASE_URL" -Atc \
  "SELECT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS')"
```

把输出保存为 `CUTOFF`。先 dry-run，从空 resume 开始；每批保存 JSON 报告中的 `next_resume_after`，反复执行直到 `scanned_users=0`：

```bash
CUTOFF='2026-07-22 12:00:00'

.venv/bin/python scripts/backfill_companion_world.py \
  --dry-run --batch-size 500 --created-before "$CUTOFF"

.venv/bin/python scripts/backfill_companion_world.py \
  --dry-run --batch-size 500 --created-before "$CUTOFF" \
  --resume-after '<上一批 next_resume_after>'
```

任何 `errors` 都阻断发布。核对 `zero_binding_users`、`mapped_users`、`mapped_bindings`、`full_capacity_users` 分布后，用**同一个 cutoff**去掉 `--dry-run` 实跑，并以相同 resume 规则跑完。脚本逐 `platform_user` 单事务，可安全重放；不得更换 cutoff 混跑。

### 5. 只读数据对账

以下 `COUNT(*)` 查询均应返回 0，override 查询均应返回空结果集；示例时间字面量必须替换为第 4 步固定值：

```sql
-- cutoff 内 active legacy binding 必须都有 legacy resident 映射。
SELECT COUNT(*)
FROM account_owner_bindings b
JOIN platform_users p ON p.id = b.platform_user_id
LEFT JOIN universe_residents r
  ON r.runtime_account_id = b.account_id AND r.origin = 'legacy'
WHERE b.status = 'active'
  AND p.created_at <= '2026-07-22 12:00:00'
  AND r.id IS NULL;

-- active/offline resident 不得缺 runtime account 或稳定 conversation。
SELECT COUNT(*)
FROM universe_residents r
LEFT JOIN accounts a ON a.id = r.runtime_account_id
LEFT JOIN ai_conversations c ON c.resident_id = r.id
WHERE r.status IN ('active','offline') AND (a.id IS NULL OR c.id IS NULL);

-- conversation 的 universe/runtime/owner 必须与 resident/world 一致。
SELECT COUNT(*)
FROM ai_conversations c
JOIN universe_residents r ON r.id = c.resident_id
JOIN universes u ON u.id = r.universe_id
WHERE c.universe_id <> r.universe_id
   OR c.runtime_account_id <> r.runtime_account_id
   OR c.owner_platform_user_id <> u.owner_platform_user_id;

-- 世界容量不得超过 10；有 legacy resident 的世界必须有 primary。
SELECT COUNT(*) FROM (
  SELECT universe_id FROM universe_residents
  WHERE status = 'active' GROUP BY universe_id HAVING COUNT(*) > 10
) over_capacity;

SELECT COUNT(*)
FROM universes u
WHERE u.legacy_primary_account_id IS NULL
  AND EXISTS (
    SELECT 1 FROM universe_residents r
    WHERE r.universe_id = u.id AND r.origin = 'legacy' AND r.status = 'active'
  );

-- L3 provenance resident 不得跨 universe。
SELECT COUNT(*)
FROM universe_memory_facts f
JOIN universe_residents r ON r.id = f.source_resident_id
WHERE f.source_resident_id IS NOT NULL AND f.universe_id <> r.universe_id;

-- 同一真人的 active residents 不得保留不同的 legacy account 配额副本。
-- m0031 运行时虽会按最严格值防绕过，任一返回行仍阻断开 flag。
SELECT u.owner_platform_user_id
FROM universes u
JOIN universe_residents r ON r.universe_id = u.id AND r.status = 'active'
JOIN accounts a ON a.id = r.runtime_account_id
GROUP BY u.owner_platform_user_id
HAVING COUNT(DISTINCT CASE WHEN a.daily_limit IS NULL THEN '__default__' ELSE CAST(a.daily_limit AS TEXT) END) > 1
    OR COUNT(DISTINCT CASE WHEN a.rpm_limit IS NULL THEN '__default__' ELSE CAST(a.rpm_limit AS TEXT) END) > 1;

-- canonical platform_user override 与 account 兼容副本必须一致。
SELECT u.owner_platform_user_id, a.id AS runtime_account_id
FROM universes u
JOIN platform_users p ON p.id = u.owner_platform_user_id
JOIN universe_residents r ON r.universe_id = u.id AND r.status = 'active'
JOIN accounts a ON a.id = r.runtime_account_id
WHERE (p.daily_limit IS NOT NULL AND (a.daily_limit IS NULL OR a.daily_limit <> p.daily_limit))
   OR (p.rpm_limit IS NOT NULL AND (a.rpm_limit IS NULL OR a.rpm_limit <> p.rpm_limit));
```

模板目录还须恰好四条 active rank 1..4，且元数据非空；最稳妥的复核是再次运行模板脚本 `--dry-run`，报告应无错误且四个 ID 全部为 keep。m0031 后新增 resident 直接读取真人 canonical override；遗留 account 副本只作兼容和对账，不再决定 turn 上限。

### 6. 开 flag 与冒烟

只有 D-14 钱包预检、模板、backfill、对账、客户端版本与口径同步、D-09 override 预检、双后端测试全部通过后，才把 central/backend 的：

```bash
COMPANION_WORLD_P1_ENABLED=true
COMPANION_WORLD_L3_BACKGROUND_ENABLED=true
COMPANION_WORLD_PROACTIVE_SAFETY_ENABLED=true
```

小流量重启后至少验证：

1. 新用户登录返回 `account:null`，bootstrap 恰有四候选，确认 1 位后只建一个 runtime/resident/conversation，且无 owner binding/新客重复赠权。
2. 老用户仍见全部 legacy residents，不自动补四候选；满 10 用户新增返回 `resident_capacity_exceeded`。
3. conversation list/history 不出现 `runtime_account_id`；Web/微信/其他 resident 消息不混入 App history。
4. 同 `client_message_id` 重试只落一条入站且 `deduplicated=true`；并发 turn 另一条返回 `turn_in_progress`。
5. resident A 蒸馏的用户事实能被同世界 B 注入，其他世界不可见；relationship/commitment 不进 L3。
6. 真人级 proactive 仅 legacy primary 允许；App-only 不发送；reminder/commitment 仍按 resident 独立。

### 7. 观测与扩量闸

首轮重点观察：

- API：`preset_catalog_not_ready`、bootstrap/confirm 成功率、`resident_capacity_exceeded`、`conversation_not_found`、`turn_in_progress`、`rate_limited`。
- 数据：runtime/resident/conversation 孤儿数（第 5 步 SQL）、active resident 容量分布、legacy primary 缺失数。
- L3：append 量、`dreaming typed memory sink failed` 日志、compact 的 `merged_groups/superseded_facts`、跨 universe 对账。
- 调度：`/admin/dreaming/scheduler` 的 `last_run.memory_compaction` 和 heartbeat；确认只有 central writer。
- 主动消息：`companion_world_human_level_proactive_blocked` 命中量与每真人实际触达数，确认无 N×。

扩量前再次运行 `make test-pg`、模板 dry-run、backfill 幂等复跑和第 5 步对账。

### 8. 回滚

发现数据隔离、孤儿、重复计费、客户端不兼容或调度多写时：

1. 立即设置 `COMPANION_WORLD_P1_ENABLED=false`、`COMPANION_WORLD_L3_BACKGROUND_ENABLED=false` 并重启 backend；停止新入口与 L3 后台行为。
2. 保留 m0032 和所有 universe/resident/conversation/L3 行，不执行删除或反向迁移。
3. 微信、legacy `/chat/*` 与既有账号继续原入口。切换后创建且没有 legacy account 的少量新用户，回退后重新登录会走旧 auth 兼容建号；必须先枚举和记录这些用户。
4. 若只是 Dreaming 重复 writer，先停多余 scheduler，保留 central 单例；不要删除 superseded L3 历史。
5. 默认保留 `COMPANION_WORLD_PROACTIVE_SAFETY_ENABLED=true`，避免 backfill 后的多居民放大真人级触达；只有明确要恢复旧 per-account 口径时才设为 false。不得删除 world 映射规避。
6. 修复后从模板预检、固定 cutoff 对账与双后端门禁重新开始，不沿用未审计的半批状态。

## Companion World M3 发布运行手册

本节覆盖文字 Feed、App 拉取式通知和 App-only 真人级主动触达。三项默认关闭、正交灰度；发布前仍须先满足上一节 P1 的模板、backfill、客户端版本和数据对账门槛。

### 1. 开关依赖与回滚边界

| 开关 | 实际门控 | 不门控 | 回滚动作 |
|---|---|---|---|
| `COMPANION_WORLD_FEED_ENABLED` | Feed API、AI world-content scheduler、Feed outbox consumer | P1 API、L3、proactive、通知 | 设为 false，并停止 `scripts/run_world_content_scheduler.py` |
| `COMPANION_WORLD_APP_INBOX_ENABLED` | 通知 API、AppInboxAdapter visible 写入 | 微信 outbound、真人级候选生成、既有通知 cleanup | 设为 false；保留 cleanup 和已存在通知行 |
| `COMPANION_WORLD_APP_ONLY_HUMAN_PROACTIVE_ENABLED` | 无真实微信路由时的真人级 App reservation/投递 | per-resident reminder/commitment、通知读取、微信 legacy 路径 | 单独设为 false，不要连带关闭 inbox |

App-only 真人级触达的有效条件是 `COMPANION_WORLD_APP_INBOX_ENABLED && COMPANION_WORLD_APP_ONLY_HUMAN_PROACTIVE_ENABLED`。World API 仍另受 `COMPANION_WORLD_P1_ENABLED` 控制；L3 与主动消息安全阀仍分别受 `COMPANION_WORLD_L3_BACKGROUND_ENABLED`、`COMPANION_WORLD_PROACTIVE_SAFETY_ENABLED` 控制。关闭 M3 开关不删除 post、outbox 或 notification 行，也不执行反向 migration。

### 2. 进程拓扑与灰度顺序

`scripts/run_world_content_scheduler.py` 必须只在一个 central-capable 实例运行。即使 PostgreSQL 的 slot claim 能阻止重复发布，也不得用多实例替代单例部署约束。outbox claim 支持 PG 多 worker，但当前脚本把生成与 outbox 串在同一单例中；不要在 aliyun1、aliyun2 厚节点各启动一份。通知 cleanup 只由 central proactive scheduler 执行，node scheduler 不重复执行。

推荐顺序：

1. 运行 m0033、部署 default-off 代码，确认三个 M3 flag 均为 false。
2. 先开 `APP_INBOX`，验证 per-resident reminder/commitment、通知拉取/红点/显式已读及 cleanup。
3. 再开 `FEED` 的用户文字 API；四个北京窗口配置有效后，只启动一份 world-content scheduler，小流量观察 AI Feed。
4. 最后开 `APP_ONLY_HUMAN_PROACTIVE`，确认真实微信 route 仍唯一优先，再逐步扩量 App-only 真人级触达。
5. Feed 审核策略未单独评审和测试前不接 moderation；当前语义保持默认直接发布。

### 3. Heartbeat 与告警字段

通过 `GET /admin/ops/status` 查看 `schedulers.heartbeats`：

- `world_content_scheduler.metadata.last_run_metrics`：`feed_slot_claimed`、`feed_slot_claim_conflict`、`feed_slot_skipped`、`feed_retry_scheduled`、`feed_retry_exhausted`、`feed_published`，以及 `outbox_pending/processing/dead/lag_seconds` 和当轮 claim/deliver/fail。
- `proactive_scheduler.metadata.last_notification_cleanup`：`cancelled_reservations`、`deleted`、`reconciled_users`、`errors`。
- `proactive_scheduler.metadata.last_m3_observability`：`human_claim_success`、`human_claim_blocked_24h`、`human_claim_blocked_inflight`、`human_speaker_cancelled`、`human_speaker_reselected`。

扩量时应阻断：heartbeat 陈旧/错误、outbox lag 持续增长、`dead>0`、cleanup 连续报错、speaker cancel 异常突增。24 小时拦截本身是正常安全阀命中，应结合成功 claim 与真人实际触达量判断，不按单次命中报警。

### 4. 只读数据对账

生产 PostgreSQL 先记录北京墙钟 `NOW_BJ` 与严格向前 24 小时的 `SINCE_24H`，替换下方示例字面量。除 outbox 状态汇总外，其余异常查询应返回空结果集。

```sql
-- 每个 universe 每个 AI slot 最多一行，单日最多 morning/evening 两行。
SELECT universe_id, ai_local_date, ai_slot, COUNT(*) AS rows_in_slot
FROM universe_posts
WHERE source_type = 'ai_feed'
GROUP BY universe_id, ai_local_date, ai_slot
HAVING COUNT(*) > 1;

SELECT universe_id, ai_local_date, COUNT(*) AS rows_in_day
FROM universe_posts
WHERE source_type = 'ai_feed'
GROUP BY universe_id, ai_local_date
HAVING COUNT(*) > 2;

-- outbox 队列状态；pending/processing 应回落，dead 必须为 0。
SELECT status, COUNT(*) AS total, MIN(created_at) AS oldest_created_at
FROM companion_world_outbox
GROUP BY status
ORDER BY status;

-- processing lease 陈旧行；300 秒须替换为部署的 claim lease。
SELECT id, idempotency_key, attempts, claimed_at, last_error
FROM companion_world_outbox
WHERE status = 'processing'
  AND claimed_at <= '2026-07-22 11:55:00';

-- 每真人未过期 visible 通知不得超过 200。
SELECT platform_user_id, COUNT(*) AS visible_total
FROM app_notifications
WHERE delivery_status = 'visible'
  AND expires_at > '2026-07-22 12:00:00'
GROUP BY platform_user_id
HAVING COUNT(*) > 200;

-- 同真人两次成功真人级 visible 的间隔不得严格小于 24 小时；恰好 24 小时允许。
SELECT a.platform_user_id, a.id AS earlier_id, b.id AS later_id,
       a.delivered_at AS earlier_at, b.delivered_at AS later_at
FROM app_notifications a
JOIN app_notifications b
  ON b.platform_user_id = a.platform_user_id
 AND b.scope = 'human' AND b.delivery_status = 'visible'
 AND b.delivered_at > a.delivered_at
 AND b.delivered_at::timestamp < a.delivered_at::timestamp + INTERVAL '24 hours'
WHERE a.scope = 'human' AND a.delivery_status = 'visible';

-- 同真人不得同时有多个未过期真人级 reservation。
SELECT platform_user_id, COUNT(*) AS live_reservations
FROM app_notifications
WHERE scope = 'human' AND delivery_status = 'reserved'
  AND claim_expires_at > '2026-07-22 12:00:00'
GROUP BY platform_user_id
HAVING COUNT(*) > 1;

-- 最近 24 小时已有 visible 时不得再有 live reservation。
SELECT DISTINCT r.platform_user_id
FROM app_notifications r
JOIN app_notifications v ON v.platform_user_id = r.platform_user_id
WHERE r.scope = 'human' AND r.delivery_status = 'reserved'
  AND r.claim_expires_at > '2026-07-22 12:00:00'
  AND v.scope = 'human' AND v.delivery_status = 'visible'
  AND v.delivered_at > '2026-07-21 12:00:00';
```

本地 SQLite 对账时，唯一需要改写的是 `::timestamp + INTERVAL '24 hours'`，替换为 `datetime(a.delivered_at, '+24 hours')`；其他查询可直接使用。示例时间不得原样用于生产。

### 5. 回滚

1. Feed 异常：关闭 `FEED` 并停止 world-content scheduler；保留 outbox，修复后由幂等 consumer 续跑。
2. 真人级触达异常：只关闭 `APP_ONLY_HUMAN_PROACTIVE`；继续提供通知读取与 per-resident obligations。
3. 通知写入/API 异常：关闭 `APP_INBOX`；cleanup 默认继续运行，只有确认 cleanup 自身有缺陷时才停 central proactive scheduler 的该步骤。
4. 任意账号隔离、N× 触达、slot 超配或 visible 超过 200 的异常都阻断扩量；先保存对账结果和代码 SHA，不删除加性数据。

## Companion World M4 发布运行手册

M4 lifecycle 与 mailbox 共用 `scripts/run_world_lifecycle_scheduler.py`，但使用独立开关和游标。部署时三个开关必须保持默认关闭：

- `COMPANION_WORLD_LIFECYCLE_EVALUATION_ENABLED=false`
- `COMPANION_WORLD_LIFECYCLE_COMMIT_ENABLED=false`
- `COMPANION_WORLD_MAILBOX_ENABLED=false`

只允许一个 central-capable 实例运行 scheduler。`GET /admin/ops/status` 的 `schedulers.heartbeats` 中，`world_lifecycle_scheduler` 必须持续更新；`metadata.enabled` 与 `metadata.mailbox_enabled` 应与部署开关一致，`last_run_metrics` 和 `last_run_mailbox_metrics` 只能包含聚合计数，不应出现 owner、resident、account 或 evidence 明细。

推荐灰度顺序：先以全 false 部署 m0034 并完成下方对账；再只开 lifecycle evaluation 做 shadow；人工核验 review queue 后才允许小流量开 commit；mailbox 先导入签名 catalog 并开放只读 API，再启动投递和 accept。任何阶段都不得跳过 P1/M3 的模板、backfill、客户端版本与对账门。

### M4 只读数据对账

除 outbox 状态汇总外，以下异常查询都应返回空结果集。

```sql
-- offline 组合事务必须同时具备 committed event、read_only conversation 和 farewell。
SELECT r.id AS resident_id, r.departure_event_id,
       e.status AS event_status, c.state AS conversation_state,
       p.id AS farewell_post_id
FROM universe_residents r
LEFT JOIN resident_lifecycle_events e ON e.id = r.departure_event_id
LEFT JOIN ai_conversations c ON c.resident_id = r.id
LEFT JOIN universe_posts p
  ON p.id = e.farewell_post_id AND p.departure_event_id = e.id
WHERE r.status = 'offline'
  AND (r.origin = 'legacy' OR e.status <> 'committed'
       OR c.state <> 'read_only' OR p.id IS NULL);

-- active resident 不得绑定 committed departure。
SELECT r.id, r.departure_event_id
FROM universe_residents r
JOIN resident_lifecycle_events e ON e.id = r.departure_event_id
WHERE r.status = 'active' AND e.status = 'committed';

-- committed event 必须有唯一 commit action。
SELECT e.id, COUNT(a.id) AS commit_actions
FROM resident_lifecycle_events e
LEFT JOIN resident_lifecycle_event_actions a
  ON a.event_id = e.id AND a.action = 'admin_committed_offline'
WHERE e.status = 'committed'
GROUP BY e.id
HAVING COUNT(a.id) <> 1;

-- 每个 world 的 active resident 不得超过 10；open letter 不得超过 1。
SELECT universe_id, COUNT(*) AS active_count
FROM universe_residents
WHERE status = 'active'
GROUP BY universe_id
HAVING COUNT(*) > 10;

SELECT universe_id, COUNT(*) AS open_count
FROM character_letters
WHERE status IN ('unread', 'read', 'deferred')
GROUP BY universe_id
HAVING COUNT(*) > 1;

-- accepted letter 必须指向同 world 的 mailbox resident 和唯一 conversation。
SELECT l.id, l.universe_id, l.accepted_resident_id,
       r.universe_id AS resident_universe_id, r.origin,
       COUNT(c.id) AS conversations
FROM character_letters l
LEFT JOIN universe_residents r ON r.id = l.accepted_resident_id
LEFT JOIN ai_conversations c ON c.resident_id = r.id
WHERE l.status = 'accepted'
GROUP BY l.id, l.universe_id, l.accepted_resident_id,
         r.universe_id, r.origin
HAVING l.accepted_resident_id IS NULL
    OR r.universe_id <> l.universe_id
    OR r.origin <> 'mailbox'
    OR COUNT(c.id) <> 1;

-- 非 accepted letter 不得携带 accepted_resident_id。
SELECT id, status, accepted_resident_id
FROM character_letters
WHERE status <> 'accepted' AND accepted_resident_id IS NOT NULL;

-- farewell outbox 汇总；dead 必须为 0，pending/processing 应持续回落。
SELECT status, COUNT(*) AS total, MIN(created_at) AS oldest_created_at
FROM companion_world_outbox
WHERE idempotency_key LIKE 'departure-farewell:v1:%'
GROUP BY status
ORDER BY status;

-- PostgreSQL：投递快照必须满足 active_count < 8。
SELECT id, universe_id, eligibility_snapshot_json
FROM character_letters
WHERE COALESCE(
  (eligibility_snapshot_json::jsonb ->> 'active_count')::integer, 999
) >= 8;
```

SQLite 执行最后一条时，把取值表达式替换为 `COALESCE(json_extract(eligibility_snapshot_json, '$.active_count'), 999)`；其余查询可直接使用。对账只读，不得通过删除 event、letter、resident 或 outbox 来“修复”结果。

### M4 回滚

1. lifecycle shadow 异常：关闭 evaluation 并停止其扫描；保留已有 candidate/audit。
2. 不可逆提交异常：立即关闭 commit；已经 committed 的 offline/read-only/farewell 不自动恢复，按 correction 流程追加审计。
3. mailbox 异常：关闭 mailbox 并停止投递；保留历史 letter 和已接受 resident，不反向删除 runtime。
4. heartbeat 陈旧、对账非空、PG 并发门禁失败或 outbox dead 非零时阻断扩量，保存代码 SHA 与查询结果后再处理。

## Companion World M5 发布运行手册

M5 限时来访与真人聊天使用 m0035 加性表，并复用 `scripts/run_world_lifecycle_scheduler.py` 做 invite/pending/active expiry。两个开关默认关闭：

```bash
COMPANION_WORLD_VISITS_ENABLED=false
COMPANION_WORLD_HUMAN_CHAT_ENABLED=false
```

`VISITS_ENABLED` 门控 invite/redeem/accept/visitor Feed 等 visit API，并决定独立进程是否运行 visit expiry 步骤；它不删除已有 visit/chat。`HUMAN_CHAT_ENABLED` 只允许新真人文字消息，关闭时 conversation/message 历史、read、self-hide、report 和 block 仍可用。两者都依赖 `COMPANION_WORLD_P1_ENABLED=true` 的 World session；M5 不依赖 M3 Feed 生成开关读取已发布历史。

### 1. 上线前阻断项与顺序

1. M5 PR #48 已合入 `main`（归档核查基线 `3403380`）；生产部署目标须包含 m0035 及后续 m0036 `app_id` 漂移修复。迁移后确认 `accounts` / `account_owner_bindings` 均有 `app_id`、`ux_owner_binding_active_user_app` 存在，且两个 M5 flag 仍为 false。
2. 客户端必须同步“24h invite → 7d pending → A 接受后独立 30d visit”，不得继续按旧“12h/兑换即生效”实现。
3. 客户端必须在 visit 终止时清除好友世界页面/媒体缓存；旧深链不得绕过服务端 `visit_id` ACL。
4. 运营/法务必须确认 `human_chat_reports.retained_until` 的合规期限与清理 SOP；未确认前 evidence fail-safe 保留，不启动自动删除。
5. 先开放 human conversation/history 读模型，再小流量开 `VISITS_ENABLED` 验证 create/redeem/pending/accept/Feed，最后开 `HUMAN_CHAT_ENABLED` 写消息。
6. 只允许一个 central-capable 实例运行 world lifecycle scheduler。`GET /admin/ops/status` 中 `configured.world_lifecycle.visits_enabled` 与部署值一致，heartbeat `last_run_visit_metrics` 只含 `scanned/expired_invites/expired_visits`。

邀请码兑换同时按真人 10 RPM、来源 IP 30 RPM 做 DB-backed 滑动窗口限流；出现 `rate_limited`、同码双花、第三/第四名额冲突或 block 后仍兑换成功时立即阻断扩量。

### 2. M5 只读数据对账

以下异常查询应全部返回空结果集。

```sql
-- 已初始化 slot 的 world 必须恰好为 1/2/3 三个固定槽。
SELECT universe_id, COUNT(*) AS slot_count,
       MIN(slot_no) AS min_slot, MAX(slot_no) AS max_slot
FROM universe_visit_slots
GROUP BY universe_id
HAVING COUNT(*) <> 3 OR MIN(slot_no) <> 1 OR MAX(slot_no) <> 3;

-- slot occupant 必须同空同非空，且只能指向 active invite 或 open visit。
SELECT s.*
FROM universe_visit_slots s
LEFT JOIN universe_invites i
  ON s.occupant_type = 'invite' AND i.id = s.occupant_id
LEFT JOIN universe_visits v
  ON s.occupant_type = 'visit' AND v.id = s.occupant_id
WHERE (s.occupant_type IS NULL) <> (s.occupant_id IS NULL)
   OR (s.occupant_type = 'invite' AND (i.id IS NULL OR i.status <> 'active'))
   OR (s.occupant_type = 'visit' AND (v.id IS NULL OR v.status NOT IN ('pending','active')))
   OR (s.occupant_type IS NOT NULL AND s.occupant_type NOT IN ('invite','visit'));

-- B 跨好友世界 pending+active 合计不得超过 3。
SELECT visitor_platform_user_id, COUNT(*) AS open_visits
FROM universe_visits
WHERE status IN ('pending','active')
GROUP BY visitor_platform_user_id
HAVING COUNT(*) > 3;

-- redeemed invite 必须与 visit 双向同锚；其他 invite 不得挂 redeemed_visit_id。
SELECT i.id, i.status, i.redeemed_visit_id, v.id AS visit_id
FROM universe_invites i
LEFT JOIN universe_visits v ON v.id = i.redeemed_visit_id
WHERE (i.status = 'redeemed' AND (
         v.id IS NULL OR v.invite_id <> i.id OR v.universe_id <> i.universe_id
         OR v.owner_platform_user_id <> i.owner_platform_user_id
         OR v.visitor_platform_user_id <> i.redeemed_by_platform_user_id))
   OR (i.status <> 'redeemed' AND i.redeemed_visit_id IS NOT NULL);

-- active visit 必须有绝对期限和恰好一个 active human conversation。
SELECT v.id, v.status, v.expires_at, COUNT(c.id) AS conversations,
       MIN(c.status) AS conversation_status
FROM universe_visits v
LEFT JOIN human_conversations c ON c.visit_id = v.id
WHERE v.status = 'active'
GROUP BY v.id, v.status, v.expires_at
HAVING v.expires_at IS NULL OR COUNT(c.id) <> 1 OR MIN(c.status) <> 'active';

-- visit 终态不得继续占 slot；已有 conversation 必须 read_only。
SELECT v.id, v.status, s.slot_no, c.status AS conversation_status
FROM universe_visits v
LEFT JOIN universe_visit_slots s
  ON s.occupant_type = 'visit' AND s.occupant_id = v.id
LEFT JOIN human_conversations c ON c.visit_id = v.id
WHERE v.status IN ('expired','rejected','cancelled','left','revoked','blocked')
  AND (s.slot_no IS NOT NULL OR (c.id IS NOT NULL AND c.status <> 'read_only'));

-- 真人消息 sender 必须是会话双方，sequence 从 1 连续且无删除空洞。
SELECT c.id AS conversation_id, COUNT(m.id) AS message_count,
       COALESCE(MAX(m.sequence_no), 0) AS max_sequence
FROM human_conversations c
LEFT JOIN human_messages m ON m.conversation_id = c.id
GROUP BY c.id
HAVING COUNT(m.id) <> COALESCE(MAX(m.sequence_no), 0);

SELECT m.id, m.conversation_id, m.sender_platform_user_id
FROM human_messages m
JOIN human_conversations c ON c.id = m.conversation_id
WHERE m.sender_platform_user_id NOT IN (
  c.owner_platform_user_id, c.visitor_platform_user_id
);

-- 任一方向 block 后，两人之间不得仍有 open visit。
SELECT b.blocker_platform_user_id, b.blocked_platform_user_id, v.id AS visit_id
FROM platform_user_blocks b
JOIN universe_visits v
  ON v.status IN ('pending','active')
 AND ((v.owner_platform_user_id = b.blocker_platform_user_id
       AND v.visitor_platform_user_id = b.blocked_platform_user_id)
   OR (v.owner_platform_user_id = b.blocked_platform_user_id
       AND v.visitor_platform_user_id = b.blocker_platform_user_id));

-- 举报必须有可解析、非空的独立 evidence snapshot；retained_until 为空表示等待合规期限配置。
SELECT id, conversation_id, reason_code, retained_until
FROM human_chat_reports
WHERE evidence_snapshot_json IS NULL OR evidence_snapshot_json = '';
```

对账只能读，不能通过删除 slot/visit/message/report 来消除异常。`self-hide` 只写 conversation 的 participant hidden 字段，`human_messages` 和 `human_chat_reports` 行数不应因此减少。

### 3. 回滚

1. invite/ACL/Feed 异常：关闭 `COMPANION_WORLD_VISITS_ENABLED` 并停止 visit expiry step；保留 m0035 和全部状态。已有 human history/report/block 仍可访问。
2. 真人发送异常：只关闭 `COMPANION_WORLD_HUMAN_CHAT_ENABLED`；新 send 返回 `human_chat_read_only`，历史/read/hide/report/block 保持可用。
3. 已 `expired/rejected/cancelled/left/revoked/blocked` 的 visit 不恢复；解除 block 也不复活旧 visit，重新来访必须新 invite。
4. 举报 evidence retention 未确认、heartbeat 陈旧、任一异常 SQL 非空或 PG 并发门禁失败时不得扩量；保存代码 SHA、查询结果和时间窗口后修复。
