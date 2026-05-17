# 下一步开发步骤：账号级 Profile 验收与管理

更新时间：2026-05-16

## 当前已完成

已经走通：

```text
一个 OpenClaw 实例
-> 两个个人微信账号同时在线
-> Bridge 从 sessionKey 解析真实微信账号级 account_id
-> Backend 按 account_id 隔离 Soul、会话、记忆和配置
-> 每个 account_id 自动创建 user_profile.md
```

已验证账号：

```text
86f866663cf9-im-bot
53de8b76fd98-im-bot
```

## Step 1：确认 OpenClaw 多账号状态

确认 session 隔离配置：

```bash
openclaw config get session.dmScope
```

期望：

```text
per-account-channel-peer
```

确认微信账号在线：

```bash
openclaw channels status --probe
cat ~/.openclaw/openclaw-weixin/accounts.json
```

## Step 2：查看 raw payload

```bash
curl 'http://127.0.0.1:8000/debug/messages/raw?limit=5'
curl http://127.0.0.1:8000/debug/messages/<message_db_id>/raw
```

Admin 版本：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  'http://127.0.0.1:8000/admin/messages/raw?limit=5'

curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/messages/<message_db_id>/raw
```

raw 中会包含：

- `ctx.sessionKey`
- `ai4all_bridge.account_candidates`
- `ai4all_bridge.resolved_account_id`

## Step 3：查看账号级 user_profile.md

```bash
curl http://127.0.0.1:8000/debug/accounts/<account_id>/user-profile
```

Admin 版本：

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/<account_id>/user-profile
```

文件位置：

```text
data/user_profiles/<account_id>/user_profile.md
```

## Step 4：端到端验收

至少准备两个微信账号 A/B：

- A 发起 5 轮连续文本对话。
- B 发起 5 轮连续文本对话。
- A/B 对话上下文不能串线。
- A/B 分别修改 `user_profile.md` 后，回复风格能明显区分。
- Backend 重启后历史消息仍可查询。
- Backend 重启后 `user_profile.md` 仍被读取。

建议验证方式：

1. 在账号 A 的 `user_profile.md` 中写入“称呼我为 A，回复非常简洁”。
2. 在账号 B 的 `user_profile.md` 中写入“称呼我为 B，回复更活泼”。
3. 两个账号分别发送“你记得我是谁吗？”
4. 确认回复不串 profile。

## Step 5：下一轮开发重点

- 增加账号级 profile 编辑 API。
- 增加账号启用/禁用能力。
- 增加账号列表中 profile 路径和最近活跃时间。
- 将现有 `contacts` 兼容 API 逐步迁移为账号级 API。

## 暂缓事项

- Web 后台。
- 语音 ASR。
- 长期记忆产品化。
- 公开 SaaS onboarding。
- 群聊 bot。
