# 后台管理说明

当前还没有 Web 后台页面。

目前的后台管理能力是 Admin API，通过 `ADMIN_TOKEN` 鉴权后用 HTTP API 操作。

## Admin 鉴权

所有 `/admin/*` 接口都需要请求头：

```http
Authorization: Bearer <ADMIN_TOKEN>
```

本地 `.env` 中配置：

```bash
ADMIN_TOKEN=replace-with-a-strong-token
```

## 查看账号

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts
```

这里的账号指微信/OpenClaw 入口账号。

当前已验证的是一个 OpenClaw 实例里的一个服务微信号入口。数据模型已经支持多个 `account_id`，但多服务微信号接入还需要后续验证。

## 查看某个账号下的用户

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/accounts/openclaw-weixin/contacts
```

这里的用户指某个服务微信号下的微信私聊用户。

## 查看用户详情

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/contacts/1
```

## 修改用户备注或状态

```bash
curl -X PATCH http://127.0.0.1:8000/admin/contacts/1 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"notes":"内部测试用户"}'
```

当前支持的状态：

- `active`
- `disabled`

## 禁用或启用用户

禁用用户：

```bash
curl -X POST http://127.0.0.1:8000/admin/contacts/1/disable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

启用用户：

```bash
curl -X POST http://127.0.0.1:8000/admin/contacts/1/enable \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

被禁用的用户再次发消息时，Backend 会返回 `status=disabled` 和 `no_reply=true`，不会调用 LLM。

## 修改用户 Profile

```bash
curl -X PATCH http://127.0.0.1:8000/admin/contacts/1/profile \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"style":"温柔、简洁、像熟悉的朋友"}'
```

当前支持字段：

- `display_name`
- `style`
- `system_prompt`
- `preferences`

Profile 当前会影响用户级 LLM Prompt。

## 查看会话

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/sessions
```

## 查看会话详情

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/admin/sessions/1
```

返回内容包括：

- 会话信息。
- 用户 profile。
- 最近消息。

## 重置会话

```bash
curl -X POST http://127.0.0.1:8000/admin/sessions/1/reset \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

这会删除该 session 下已保存的消息。

## 本地 Debug API

`/debug/*` 接口仍保留给本地开发使用。它们当前不使用 `ADMIN_TOKEN`，不应该暴露到公网。

## 常用排查命令

```bash
curl http://127.0.0.1:8000/health
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
