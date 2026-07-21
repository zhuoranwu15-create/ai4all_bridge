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

### 1. 发布前输入与备份

必须具备：

- 经运营签字的 UTF-8 JSON manifest，恰含 rank 1..4 四条模板；每条有稳定 `template_id`、名称、头像引用、简介、三个标签、非空 SOUL/IDENTITY persona 与 `persona_version`。
- 已确认支持 `/v1/auth/session` 返回 `account:null` 并立即进入 world bootstrap 的客户端最低版本。
- 生产 PG 备份和可恢复点；记录发布人、时间、代码 SHA。
- central/standalone 只运行一个 Dreaming scheduler。推荐独立 proactive 进程承担：`DREAMING_SCHEDULER_ENABLED=false`、`PROACTIVE_DREAMING_SCHEDULER_ENABLED=true`；纯 node 不运行 L3 compact。

先确保：

```bash
COMPANION_WORLD_P1_ENABLED=false
make test-unit
make test
make test-pg
git diff --check
```

### 2. flag=false 部署 m0030

部署代码并重启后检查：

```bash
curl -fsS http://127.0.0.1:8180/health/ready
```

此时 `/v1/worlds/*`、`/v1/conversations`、`/v1/ai-conversations/*` 应以 404 `not_found` 隐藏；微信与 legacy App 路径保持原行为。不要在迁移后立刻开 flag。

### 3. 导入四模板目录

脚本不会内置或打印 persona 正文；已发布 `template_id` 的内容不可原地修改，换版必须使用新 ID 并退休旧行。

```bash
.venv/bin/python scripts/import_companion_world_presets.py \
  /secure/path/companion_world_presets.json --dry-run

.venv/bin/python scripts/import_companion_world_presets.py \
  /secure/path/companion_world_presets.json
```

预期：实际导入报告 `errors=[]`、`catalog_ready=true`。再次执行应只出现 `keep_ids`，不得新增重复模板。manifest 放受控目录，不提交密钥或生产 persona 到临时日志。

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

以下查询均应返回 0；示例时间字面量必须替换为第 4 步固定值：

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
```

模板目录还须恰好四条 active rank 1..4，且元数据非空；最稳妥的复核是再次运行模板脚本 `--dry-run`，报告应无错误且四个 ID 全部为 keep。

### 6. 开 flag 与冒烟

只有模板、backfill、对账、客户端版本、双后端测试全部通过后，才把 central/backend 的：

```bash
COMPANION_WORLD_P1_ENABLED=true
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

1. 立即设置 `COMPANION_WORLD_P1_ENABLED=false` 并重启 backend；停止扩量。
2. 保留 m0030 和所有 universe/resident/conversation/L3 行，不执行删除或反向迁移。
3. 微信、legacy `/chat/*` 与既有账号继续原路径。切换后创建且没有 legacy account 的少量新用户，回退后重新登录会走旧 auth 兼容建号；必须先枚举和记录这些用户。
4. 若只是 Dreaming 重复 writer，先停多余 scheduler，保留 central 单例；不要删除 superseded L3 历史。
5. 修复后从模板预检、固定 cutoff 对账与双后端门禁重新开始，不沿用未审计的半批状态。
