# 解绑流程设计

## 背景

用户在用户中心（`dashboard.html`）发起解绑后，AI 将停止响应该微信号的消息。解绑分两条路径，由用户在弹窗中选择。

解绑涉及两层状态：

1. **AI4ALL 本地绑定状态**：`channel_bindings`、`binding_intents`、提醒、proactive 等本地路由和业务数据。
2. **OpenClaw 微信登录状态**：`openclaw-weixin` 插件保存的微信 Web 登录会话。

生产目标是“按用户绑定的 OpenClaw 微信账号做单账号解绑”。不能为了某个用户解绑而清空 OpenClaw 所有微信账号，否则会影响其他仍在服务中的用户。

## 数据模型链路

```
phone → platform_users → account_owner_bindings → accounts → channel_bindings → WeChat sender_id
```

入站消息路由依据：`binding_intents`（按 `channel_account_id` 查 `status='completed'` 记录）。

---

## 路径 A：解绑 + 保留记忆

**场景**：用户暂时解绑，计划重新绑定后恢复。

### DB 操作（`unbind_account_channel`）

在 DB 清理前，`POST /web/me/unbind` 会先读取该 account 当前的 `channel_bindings`，提取可识别的 OpenClaw 微信账号 ID：

- `channel_account_id`
- `session_key` 中的 `agent:main:openclaw-weixin:{account}:direct:{peer}` 片段
- `raw_identity` 里的 `channel_account_id` / `accountId` / `account_id` / `session_key`

然后对这些目标账号逐个 best-effort 调用：

```bash
openclaw channels logout --channel openclaw-weixin --account <target-account-id>
```

调用失败不会阻断本地解绑，失败原因通过 API 的 `openclaw_cleanup` 返回。

| 表 | 操作 |
|---|---|
| `channel_bindings` | DELETE WHERE account_id=? |
| `binding_intents` | UPDATE status='revoked' WHERE account_id=? AND status='completed' |
| `reminders` | UPDATE status='cancelled' WHERE account_id=? AND status='pending' |
| `proactive_commitments` | DELETE WHERE account_id=? AND status IN ('pending','scheduled') |
| `proactive_account_state` | UPDATE enabled=0 |

### 保留不动

- `account_owner_bindings`（手机号归属关系不变）
- `accounts` 记录
- `sessions` / `messages`（对话历史）
- `user_profiles/{account_id}/` 所有文件
- `dreaming_runs` / `dreaming_memory_items`

### 重新绑定后

- 历史和记忆完整可用
- 提醒**不**恢复（已取消）
- proactive 自动 re-enable（在 `_on_binding_completed` 回调中调用 `reenable_proactive_after_rebind`）

---

## 路径 B：解绑 + 清除记忆

**场景**：用户彻底清除，下次绑定重新开始。

### DB 操作（路径 A + `wipe_account_data`）

在路径 A 全部操作之后追加：

| 表 | 操作 |
|---|---|
| `messages` | DELETE（via session_id） |
| `sessions` | DELETE WHERE account_id=? |
| `dreaming_memory_items` | DELETE（via run_id） |
| `dreaming_runs` | DELETE WHERE account_id=? |
| `profiles` | DELETE WHERE account_id=? |
| `account_owner_bindings` | DELETE WHERE account_id=? |
| `accounts` | UPDATE status='deactivated' |

### 文件系统

```python
shutil.rmtree(account_profile_dir(account_id))
```

### 保留

- `platform_users`（手机号仍在，可重新注册）

### 解绑后

- 前端自动退出登录（account 已 deactivated）
- 下次同手机号重新注册 → 全新 account，重走 onboarding

---

## 实现文件

| 文件 | 改动 |
|---|---|
| `app/db.py` | 新增 `unbind_account_channel`、`wipe_account_data`、`reenable_proactive_after_rebind` |
| `app/openclaw_gateway.py` | 新增 `logout_weixin_account`，封装 OpenClaw CLI logout |
| `app/main.py` | 新增 `POST /web/me/unbind`；解绑前 best-effort 清 OpenClaw；重绑完成回调中调用 `reenable_proactive_after_rebind` |
| `app/static/dashboard.html` | 解绑按钮 + 两步确认弹窗 |

---

## 前端交互流程

```
[解绑] 按钮（微信绑定卡片右上角）
  └→ Step 1 弹窗：
      "保留记忆 · 解绑"     ← 路径 A
      "清除全部 · 解绑"     ← 路径 B
      "取消"
          ↓
     Step 2 二次确认（路径 B 用红色危险按钮）
          ↓
     POST /web/me/unbind { keep_memories: bool }
          ↓
     路径 A：刷新绑定状态；如 OpenClaw 清理失败，展示提示
     路径 B：自动退出登录
```

---

## API

```
POST /web/me/unbind
Authorization: Bearer <session_token>
Content-Type: application/json

{ "keep_memories": true }   // false = 路径 B
```

**Response**

```json
{
  "status": "ok",
  "keep_memories": true,
  "account_id": "acct_xxx",
  "openclaw_cleanup": {
    "status": "unsupported",
    "attempts": [
      {
        "channel": "openclaw-weixin",
        "account_id": "d075590ddfc2-im-bot",
        "status": "unsupported",
        "error": "Channel \"openclaw-weixin\" does not support logout."
      }
    ]
  },
  "stats": {
    "channel_bindings_deleted": 1,
    "binding_intents_revoked": 1,
    "reminders_cancelled": 2,
    "proactive_commitments_deleted": 0
  }
}
```

`openclaw_cleanup.status`：

| 值 | 含义 |
|---|---|
| `ok` | OpenClaw logout 全部成功 |
| `skipped` | 本地没有可识别的 OpenClaw 微信账号 |
| `unsupported` | 插件不支持 logout，本地解绑已完成但 OpenClaw 会话可能仍在 |
| `partial_failed` | 部分账号 logout 成功，部分失败 |
| `failed` | 尝试过 logout，但全部失败 |

---

## OpenClaw 临时方案与限制

短期内，如果 OpenClaw / `openclaw-weixin` 不提供 per-account logout / delete 能力，只能把 OpenClaw 侧清理作为临时运维方案处理。

### 当前插件能力

截至 2026-05-30，本机 `openclaw-weixin` 只暴露：

```text
Actions: send, broadcast
```

以下标准命令不可用或会失败：

```bash
openclaw channels logout --channel openclaw-weixin --account <account-id>
openclaw channels remove --channel openclaw-weixin --account <account-id>
openclaw channels remove --channel openclaw-weixin --account <account-id> --delete
```

### 临时手工清理原则

如果必须手工清理 OpenClaw 微信登录态，只允许清理目标用户对应的单个 OpenClaw 微信账号，例如：

```text
d075590ddfc2-im-bot
```

必须避免：

- 清空整个 `~/.openclaw/openclaw-weixin/accounts.json`
- 删除 `~/.openclaw/openclaw-weixin/accounts/` 下所有账号文件
- 为一个用户解绑而影响其他微信账号

正确临时流程应是：

1. 从 AI4ALL 的 `channel_bindings.channel_account_id` 找到目标 OpenClaw 微信账号。
2. 备份 OpenClaw 状态文件。
3. 只从 `~/.openclaw/openclaw-weixin/accounts.json` 移除该目标账号。
4. 只移走该目标账号对应文件：
   - `<account-id>.json`
   - `<account-id>.sync.json`
   - `<account-id>.context-tokens.json`
5. 重启 OpenClaw Gateway。
6. 验证其他 OpenClaw 微信账号仍在 `channels status` 中。

### 产品化依赖

长期正确方案依赖 OpenClaw / `openclaw-weixin` 提供 per-account logout 或 per-account delete 能力。AI4ALL 后端已经按单账号目标调用和返回 `openclaw_cleanup` 设计；一旦插件支持 logout，解绑流程无需清理全局状态文件即可完成彻底解绑。
