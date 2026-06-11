# 数据持有情况说明

> 版本：2026-06-11 · 维护：运维/合规
> 用途：盘点 AI4ALL 微信 Bot 系统当前实际持有的用户/个人数据——存什么、存哪里、保留多久、是否加密。
> 加密现状统一为「明文 / 待评估」，落地加密方案后续单独讨论，不在本次范围。

本说明覆盖三类存储面：后端业务数据库、账号级画像文件、Web 前端访问日志（nginx）。
OpenClaw 侧仅负责微信通道与 raw payload，先前排查未发现 IP 等原始网络信息，本说明不作为重点。

---

## 1. 后端业务数据库

- 位置：`data/ai4all.sqlite3`（标准库，SQLite）。
- 保留期：**永久**。代码中不存在针对 `messages`、daily notes、`debug_traces` 等任何业务表的滚动清理/过期删除任务；数据写入后长期保留。
- 备份：`scripts/backup_data.py` 滚动保留最近 **14 份**目录（`backup_retention_count`），更旧的自动删除。
- 加密：明文 / 待评估。

| 数据类别 | 主要表 | 是否含个人信息 | 说明 |
|---|---|---|---|
| 会话消息正文 | `messages`（含 `raw_json` 原始 payload 元数据） | 是（聊天正文、语音转写、图片识别描述） | 入站/出站消息全文；按 `session_id` 归属账号 |
| 账号与会话 | `accounts`、`sessions`、`profiles` | 是（账号标识、画像元数据） | 一账号一用户模型 |
| 手机号与验证 | `phone_verifications` | 是（**手机号**、OTP、token） | Web 注册/登录验证记录；行带 `expires_at` 但不做物理清理 |
| 微信绑定身份 | `channel_bindings`、`account_owner_bindings`、`binding_intents`、`platform_users`、`platform_user_sessions` | 是（微信身份、绑定关系、Web 会话 token） | |
| 计费与权益 | `subscriptions`、`entitlement_wallets`、`entitlement_ledger`、`cost_events`、`daily_usage` | 间接（消费/用量流水） | |
| 调试明文 | `debug_traces` | 是（prompt、messages、reply、raw request/response 明文） | 受隐私权限约束，后台默认脱敏 |
| 主动消息与记忆 | `outbound_messages`、`reminders`、`proactive_*`、`dreaming_runs`、`dreaming_memory_items`、`memory_events`、`content_invitations`、`content_invitation_preferences` | 是（关怀/承诺/记忆片段含个人事实） | |
| 工具与可观测 | `tool_invocations`、`search_provider_runs`、`analytics_events`、`scheduler_heartbeats` | 间接 | |
| 后台与审计 | `admin_users`、`admin_access_events`、`admin_plaintext_grants` | 是（管理员操作、明文查看授权记录） | |
| FAQ | `faq_messages`、`faq_message_likes` | 低 | |

> 解绑硬删除：`wipe_account_data()`（`app/db.py`）按 `account_id` 删除上述子表 + 提交后清理画像目录。新增账号/会话子表必须同步加入，否则触发 FK 失败整事务回滚（历史踩坑）。

## 2. 账号级画像文件

- 位置：`data/user_profiles/<account_id>/`。
- 内容：`SOUL.md`、`IDENTITY.md`、`USER.md`、`MEMORY.md`，以及 `memory/YYYY-MM-DD.md`（按业务日的原始文字化聊天材料 / daily notes）。
- 个人信息：是（用户画像、长期记忆、原始聊天材料）。
- 保留期：**永久**（随账号存在；解绑硬删除时连目录一并清理）。
- 加密：明文 / 待评估。

## 3. Web 前端访问日志（nginx）—— 本次调整对象

- 范围：Web 端用户**注册 / 后续登录**等请求经 nginx 反代到后端（`:8180`）时产生的访问日志。
- 日志文件：`/var/log/nginx/ai4company.access.log`（Web 前端 vhost `ai4company.top`）、`/var/log/nginx/ai4company.error.log`（Web 前端错误）、`/var/log/nginx/access.log`（全局）、`/var/log/nginx/error.log`（全局错误）。
- 记录字段（`log_format main`）：
  `$remote_addr`（**客户端 IP**）、`$http_x_forwarded_for`、`$time_local`、`"$request"`（请求行，含 `/api/web/register`、`/api/web/login`、`/api/web/sms/send-otp`、`/api/web/sms/verify-otp` 等路径）、`$status`、`$body_bytes_sent`、`$http_referer`、`$http_user_agent`（**UA**）。
  > 注：客户端打的是 `/api/web/*`，vhost 内 `rewrite ^/api/web/(.*)$ /web/$1 break` 转发到后端 `/web/*`；nginx `$request` 记的是**重写前**的原始请求行，故日志/grep 用 `/api/web/` 前缀。直接打 `/web/*` 被 vhost `return 404` 挡掉。
- **不记录**：手机号、OTP、聊天正文——这些均在 POST body 中，nginx 默认格式不落地。
- 个人信息：是（客户端 IP + UA + 访问时间 + 访问的认证动作）。
- 保留期（变更）：按敏感度拆分，原统一 `daily` + `rotate 10` = **10 天滚动**调整为——
  - Web 注册/登录访问日志 `/var/log/nginx/ai4company.access.log`：`daily` + `rotate 1095` ≈ **3 年滚动**（合规/审计长留）。
  - 其余 nginx 日志（全局 `access.log`/`error.log`、Web 前端 `ai4company.error.log`）：`daily` + `rotate 183` ≈ **半年滚动**。
- 配置版本化：`deploy/logrotate/nginx`（部署到 `/etc/logrotate.d/nginx`）。用显式文件名分两段，避免通配重叠报 `duplicate log entry`；**新增 vhost 日志须手动加入对应段**，否则不轮转、无限增长。
- 容量监控：`scripts/monitor_health.py` 新增 `disk` 检查（默认开，盯 `/var/log` 卷使用率，阈值 85%），由 `ai4all-monitor-health.timer` 周期执行，超阈值经飞书告警。
- 加密：明文 / 待评估。

## 4. 其他

- 入站媒体：图片按 `image_understanding_design` 不持久化原图（v1）；`media/inbound` 仅当轮本机使用，不复制留存。`messages.content` 仅存识别描述文本。
- OpenClaw 侧：负责微信通道、hook、收发消息与 raw payload；先前排查未见 IP 等原始网络信息，本说明不作重点。

---

## 待办

- [ ] 落地加密方案（数据库 / 画像文件 / 日志），后续单独讨论后补本说明「加密」列。
- [ ] 评估后端业务库各表的长期保留与最小化策略（当前为永久）。
- [ ] `phone_verifications` 过期记录的物理清理策略。
