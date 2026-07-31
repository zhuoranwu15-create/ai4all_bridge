# 贝壳运营发放 Runbook

运营赠送和客服补偿统一使用 `scripts/grant_shells.py`，禁止直接更新
`entitlement_wallets.balance_shell_micros`。

## 记录位置

一次成功发放会同时留下两类记录：

- `entitlement_ledger`：余额变化的财务事实。运营赠送使用
  `entry_type=credit`、`source_type=manual_grant`；客服补偿使用
  `source_type=compensation`。
- `admin_access_events`：运营动作审计。`action` 为
  `wallet.manual_grant` 或 `wallet.compensation`，记录操作者、原因、脚本入口，
  并在 `metadata_json` 保存产品、真人、操作 ID、幂等键、金额及发放前后余额。

不另建“运营送贝壳”业务表：否则会与不可变 ledger 形成两套余额事实源。运营批次使用稳定的
`operation_id/source_id` 关联，每个产品用户必须使用独立幂等键；重放同一操作只修复缺失审计，
不会再次增加余额。

## 单账号操作

先预览：

```bash
.venv/bin/python scripts/grant_shells.py \
  --account aid_123456789 \
  --amount 1000 \
  --reason "运营低余额关怀赠送" \
  --operation-id ops-low-balance-20260728-pu_xxx \
  --dry-run
```

确认账号、产品、真人、发放前后余额无误后执行：

```bash
.venv/bin/python scripts/grant_shells.py \
  --account aid_123456789 \
  --amount 1000 \
  --reason "运营低余额关怀赠送" \
  --operation-id ops-low-balance-20260728-pu_xxx \
  --yes
```

## 批量发放约束

1. 候选集必须明确 `app_id`、钱包状态、用户状态、产品 membership 状态和余额阈值；按
   `(platform_user_id, app_id)` 去重，不能按微信账号数重复发放。
2. 先对完整候选集 dry-run，核对数量、异常数、发放总额和发放后余额范围。
3. 使用一个可读批次前缀，并为每位产品用户拼接独立后缀，例如
   `ops-low-balance-20260728-{platform_user_id}`。
4. 正式执行后按批次 `source_id` 前缀复查 ledger 数、credit 合计、审计数和余额；任何失败只重放
   原幂等操作，不生成新批次 ID。
5. 低余额筛选与发放之间可能发生聊天扣费；结果复查以 ledger 的
   `balance_after_shell_micros` 和关联 `admin_access_events.metadata_json` 中的
   `balance_before_shell_micros` 为准。
