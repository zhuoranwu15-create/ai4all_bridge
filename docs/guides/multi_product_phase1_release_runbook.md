# 多产品 Phase 1 发布与回滚手册

适用范围：MP-01～MP-06，migration `m0037`～`m0046`。生产仍只注册 `zhaoxi`；
`test_product` 仅用于测试依赖注入，不得创建生产路由、manifest 或 scheduler。

## 发布硬闸

- 生产 PostgreSQL 必须先完成备份并验证可恢复。
- migration 期间必须 drain **全部 writer**：所有 API 节点、主动消息/Dreaming/World
  scheduler、moderation worker 及临时写库脚本。只停中心节点不够。
- 不允许直接启动含 m0046 的新中心实例来替代逐闸发布。代码级 interlock 会让既有
  PostgreSQL 库的常规 `init_db()` 在跨越 m0040/m0042/m0044/m0045/m0046 前拒绝启动；只有
  `version=0` 且核心业务表不存在的真空库可一次建立最新 schema；单纯丢失/清空版本表
  不能绕过。interlock 不提供通用环境变量绕过，受控
  `migrate_multi_product_phase1.py --through ...` 是唯一放行路径。
- 2026-07-24 已知生产预检仍有 `quota_owner_fallback=40`。即使 expand 前脚本把它显示为
  WARN，也必须先治理到 0，否则 m0042 会阻断。
- 任一 BLOCK 或 migration 非零退出都立即停止，不得继续下一版本。

## 准备

在待发布代码目录准备环境，但不要启动服务。通过环境变量提供 DSN，避免把凭证写入命令历史：

```bash
export DATABASE_URL='postgresql://...'
.venv/bin/python scripts/precheck_multi_product_phase1.py
```

确认所有 BLOCK 为 0，并额外确认 `quota_owner_fallback=0`。记录发布前版本：

```sql
SELECT MAX(version) AS schema_version FROM schema_migrations;
```

首次 Phase 1 发布的预期值必须是 `36`。若不是 36，不要猜测或手改 migration 记录，先核对
已执行批次。版本仍低于 46 时直接启动新 central 会按预期被 interlock 拒绝，不表示服务代码
损坏。

## 迁移顺序

`migrate_multi_product_phase1.py` 在 PostgreSQL advisory lock 内执行，并校验起始版本；重复执行
已到达的同一目标是 no-op，跳版本或从意外版本执行会拒绝。

### 1. 身份与 session 基座

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 38
.venv/bin/python scripts/precheck_multi_product_phase1.py
```

预期：版本 38；precheck 已启用 m0045 同源的身份 reconcile，membership/session/account/
binding/resident/message 无空产品或 scope drift，不必等后续三个 expand 完成才检查身份锚点。

### 2. 计费 expand → contract

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 39
.venv/bin/python scripts/precheck_multi_product_phase1.py
```

确认所有 billing reconcile 为 0 后执行：

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 40
```

m0040 后钱包和 active subscription 唯一性已切为 `(platform_user_id, app_id)`，不得恢复旧
真人全局 writer。

### 3. 配额 expand → contract

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 41
.venv/bin/python scripts/precheck_multi_product_phase1.py
```

必须确认 `quota_owner_fallback`、daily/reservation scope drift 和 duplicate daily 全为 0：

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 42
```

m0042 会切换 daily 唯一键并迁移当前 zhaoxi RPM 窗口，旧 quota/RPM writer 此后不可恢复。

### 4. Referral expand → contract

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 43
.venv/bin/python scripts/precheck_multi_product_phase1.py
```

确认 referral code、relationship、review、reward ledger 的 NULL/重复/scope drift 全为 0：

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 44
```

m0044 将 invitee 唯一性切为 `(invitee_platform_user_id, app_id)`，不得恢复全局 invitee writer。

### 5. Phase 1 最终 contract

先在 m0044 状态运行最终 reconcile；此时脚本会自动启用 m0045 同源的完整检查：

```bash
.venv/bin/python scripts/precheck_multi_product_phase1.py
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 45
.venv/bin/python scripts/precheck_multi_product_phase1.py
```

确认所有计数为 0。m0045 不改写业务数据，只固化最终索引并登记基础 contract。

### 6. Billing 幂等键产品 contract

继续保持全部 writer drain，切换 ledger/cost 的旧全局幂等唯一约束：

```bash
.venv/bin/python scripts/migrate_multi_product_phase1.py --through 46
.venv/bin/python scripts/precheck_multi_product_phase1.py
```

最终要求所有计数为 0，且版本为 46。m0046 在生产 PostgreSQL 上不改写业务数据，只删除
`idempotency_key` 的旧全局唯一约束，并建立 `(app_id, idempotency_key)` 唯一索引。

## 启动与验收

只部署并启动最终 app-aware 代码。先启动唯一中心迁移/服务节点，健康后再启动其他厚节点；最后
恢复 scheduler/worker。不要混跑旧 writer。

最低验收矩阵：

1. zhaoxi 微信入站、回复、历史消息和绑定正常。
2. Native App OTP 登录、`/v1/me`、会话和聊天正常。
3. Web OTP 登录、onboarding、历史和绑定流程正常。
4. 钱包余额、一次聊天扣费、daily/RPM、邀请码预览及 Admin 查询均只返回 zhaoxi scope。
5. 再跑一次只读 precheck，确认无新 drift/duplicate。
6. 观察 DB error、唯一冲突、401、quota deny、重复奖励和 scheduler error 指标。

## 失败与回滚

- migration 命令失败：该版本在同一事务中回滚；保持 writer drain，修复阻断数据或代码后从同一
  检查点重试。不要手插 `schema_migrations`。
- m0040 前：m0037～m0039 为加性阶段，可回旧 zhaoxi 代码，但仍应保留新增列/数据。
- m0040 后：不回钱包/订阅全局唯一模型；保持 app-aware 代码，采用前向修复。
- m0042 后：不回真人级 daily/RPM key；保持产品化 subject，采用前向修复。
- m0044 后：不回 invitee 全局唯一模型；保持产品化 referral writer，采用前向修复。
- m0045 后：没有业务数据可反向迁移。若应用异常，关闭未来第二产品入口（当前生产本就没有），
  继续仅运行 app-aware zhaoxi，或发布修复版本。
- m0046 后：不恢复 billing 全局幂等键；保持产品组合键 writer，采用前向修复。

数据库整库恢复只用于确认必须丢弃维护窗口内全部变更的灾难恢复场景，并须与应用版本、所有节点
和外部消息重放点一起回退；不能只恢复数据库后让新旧 writer 混跑。
