# 阿里云部署说明

更新时间：2026-06-02

本文档描述 AI4ALL Weixin Bot 当前内测形态的阿里云部署方式。目标是先稳定运行单机版本：FastAPI Backend、OpenClaw Gateway / `openclaw-weixin`、可选独立 proactive scheduler。

## 部署前提

- 服务器开放 HTTPS 入口，公网只暴露 Web onboarding 和 OpenClaw Bridge 需要访问的 Backend 地址。
- `/admin/*` 和 `/debug/*` 应通过 nginx IP allowlist、VPN 或内网访问限制保护；应用层 token 不是唯一边界。
- 生产 `.env` 必须从 `.env.production.example` 复制后在服务器本地填写，不能提交真实密钥。
- 当前标准数据库仍是 SQLite：`data/ai4all.sqlite3`。单机内测可以继续使用；不要多实例同时写同一个 SQLite 文件。
- OpenClaw、`openclaw-weixin` 和 Backend 建议部署在同一台机器上，Bridge 通过 `http://127.0.0.1:<port>` 访问 Backend，公网入口由 nginx 转发。

## 服务器初始化

```bash
cd /opt
git clone <repo-url> ai4all-weixin-bot
cd /opt/ai4all-weixin-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.production.example .env
```

编辑 `.env`，至少替换：

- `AI4ALL_BRIDGE_SECRET`
- `ADMIN_TOKEN`
- `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL`
- `ALIYUN_ACCESS_KEY_ID`、`ALIYUN_ACCESS_KEY_SECRET`
- `ALIYUN_SMS_SIGN_NAME`、`ALIYUN_SMS_TEMPLATE_CODE`
- `ALIYUN_CAPTCHA_SCENE_ID`、`ALIYUN_CAPTCHA_PREFIX`

生产环境必须设置：

```env
APP_ENV=production
PROACTIVE_SCHEDULER_ENABLED=false
DREAMING_SCHEDULER_ENABLED=false
```

`PROACTIVE_SCHEDULER_ENABLED=false` 表示 FastAPI 进程内不启动主动消息循环；推荐用独立 systemd service 运行 scheduler。

## FastAPI systemd 守护

创建 `/etc/systemd/system/ai4all-weixin-backend.service`：

```ini
[Unit]
Description=AI4ALL Weixin Bot Backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/ai4all-weixin-bot
EnvironmentFile=/opt/ai4all-weixin-bot/.env
ExecStart=/opt/ai4all-weixin-bot/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8180
Restart=always
RestartSec=3
User=ai4all
Group=ai4all

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai4all-weixin-backend
sudo systemctl status ai4all-weixin-backend
curl http://127.0.0.1:8180/health
```

查看日志：

```bash
journalctl -u ai4all-weixin-backend -f
```

## Proactive scheduler systemd 守护

推荐上线方案：scheduler 独立进程运行，FastAPI 保持 `PROACTIVE_SCHEDULER_ENABLED=false`。

创建 `/etc/systemd/system/ai4all-weixin-proactive-scheduler.service`：

```ini
[Unit]
Description=AI4ALL Weixin Bot Proactive Scheduler
After=network-online.target ai4all-weixin-backend.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/ai4all-weixin-bot
EnvironmentFile=/opt/ai4all-weixin-bot/.env
ExecStart=/opt/ai4all-weixin-bot/.venv/bin/python scripts/run_proactive_scheduler.py
Restart=always
RestartSec=3
User=ai4all
Group=ai4all

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai4all-weixin-proactive-scheduler
sudo systemctl status ai4all-weixin-proactive-scheduler
```

注意：当前代码没有跨进程 leader election。不要同时运行多个 proactive scheduler，也不要在多 worker FastAPI 内开启 `PROACTIVE_SCHEDULER_ENABLED=true`。

## nginx 反代

示例只展示核心策略，证书可用阿里云证书服务或 certbot 管理。

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ~ ^/(admin|debug)/ {
        allow <your-office-ip>;
        deny all;
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```

## OpenClaw Bridge 配置

安装或更新 Bridge：

```bash
cd /opt/ai4all-weixin-bot
openclaw plugins install ./openclaw-bridge --force
openclaw config set plugins.entries.ai4all-openclaw-bridge.config.backendUrl http://127.0.0.1:8180
openclaw config set plugins.entries.ai4all-openclaw-bridge.config.secret '<same value as AI4ALL_BRIDGE_SECRET in .env>'
openclaw gateway restart
```

验证：

```bash
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

预期 runtime inspect 中能看到 `before_agent_reply` hook。

## openclaw-weixin QR 登录补丁

当前仓库已经跟踪以下补丁和说明文件：

- `patches/openclaw-weixin-gateway-methods-runtime.patch`
- `docs/tech_design/openclaw_weixin_gateway_qr_patch.md`

补丁作用：给已安装的 `@tencent-weixin/openclaw-weixin` 增加 `gatewayMethods = ["web.login.start", "web.login.wait"]` 元数据，使 OpenClaw host 能发现二维码登录 provider。补丁不修改微信登录协议、不修改二维码生成、不修改账号保存逻辑。

如果服务器上安装的 `openclaw-weixin` 官方包尚未包含该修复，需要在运行时安装包目录重放补丁。典型路径：

```text
~/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin
```

重放步骤：

```bash
cd ~/.openclaw/npm/node_modules/@tencent-weixin/openclaw-weixin
patch -p1 < /opt/ai4all-weixin-bot/patches/openclaw-weixin-gateway-methods-runtime.patch
openclaw gateway restart
```

如果 `patch` 提示已经应用过，检查 `src/channel.ts` 和 `dist/src/channel.js` 是否已经包含：

```text
web.login.start
web.login.wait
gatewayMethods
```

验证 QR provider：

```bash
openclaw gateway call web.login.start \
  --json \
  --timeout 45000 \
  --params '{"accountId":"bind-smoke-prod-001","force":false,"timeoutMs":35000,"verbose":false}'
```

预期返回 `qrDataUrl` 和 `sessionKey`。如果仍返回 `web login provider is not available`，说明补丁没有应用到 OpenClaw runtime 实际加载的安装包。

## 数据备份

发布、重启或迁移前先备份：

```bash
cd /opt/ai4all-weixin-bot
mkdir -p data/backups
sqlite3 data/ai4all.sqlite3 ".backup 'data/backups/ai4all_$(date +%Y%m%d_%H%M%S).sqlite3'"
tar -czf "data/backups/user_profiles_$(date +%Y%m%d_%H%M%S).tar.gz" data/user_profiles data/system
```

建议加 cron 定时备份，并定期把 `data/backups` 同步到阿里云 OSS 或另一台机器。

恢复时先停止写入进程：

```bash
sudo systemctl stop ai4all-weixin-proactive-scheduler
sudo systemctl stop ai4all-weixin-backend
```

恢复数据库和用户上下文后再启动服务。

## 发布检查

```bash
.venv/bin/pytest tests/ -v
curl http://127.0.0.1:8180/health
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

真实冒烟：

1. 打开 Web onboarding 页面并完成手机号、验证码、短信 OTP。
2. 扫码绑定，确认 `binding_intent` 完成且写入 `channel_bindings`。
3. 发送微信私聊，确认 `/openclaw/turn` 路由到预创建的 `acct_...` 并回复。
4. 设置一个近期提醒，确认 scheduler 到点发送主动微信消息。
5. Admin 后台能查看账号、绑定、消息脱敏信息，不能默认看到明文。

## 回滚

代码回滚：

```bash
cd /opt/ai4all-weixin-bot
git fetch --all
git checkout <previous-known-good-commit>
.venv/bin/pip install -r requirements.txt
sudo systemctl restart ai4all-weixin-backend
sudo systemctl restart ai4all-weixin-proactive-scheduler
```

数据回滚必须先停止 Backend 和 scheduler，再恢复 SQLite 与 `data/user_profiles` / `data/system` 备份。
