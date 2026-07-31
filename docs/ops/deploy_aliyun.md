# 阿里云部署说明

更新时间：2026-07-31

本文档描述 AI4ALL 当前阿里云部署基线：aliyun1 以 `central,node` 承担控制面、中心 PG、
本机微信 turn 和 central-only scheduler；aliyun2 以厚 `node` 本地处理归属微信账号的 turn，并通过
内网直连 aliyun1 PG。详细机器差异见
[aliyun1 / aliyun2 部署差异](platform/aliyun1_aliyun2_deployment_diff.md)。

## 部署前提

- 服务器开放 HTTPS 入口，公网只暴露 Web onboarding 和 OpenClaw Bridge 需要访问的 Backend 地址。
- `/admin/*` 和 `/debug/*` 应通过 nginx IP allowlist、VPN 或内网访问限制保护；应用层 token 不是唯一边界。
- 生产 `.env` 必须从 `.env.example` 复制后在服务器本地填写，不能提交真实密钥。
- 生产必须配置 PostgreSQL `DATABASE_URL`。SQLite 只用于本地开发/测试，不是生产回滚路径。
- 每个微信接入节点上的 OpenClaw、`openclaw-weixin` 与 Backend 同机部署，Bridge 通过
  `http://127.0.0.1:8180` 调本节点 Backend；只有 central 节点挂 Web/App/Admin 公网入口。

## 服务器初始化

```bash
cd /opt
git clone <repo-url> ai4all-weixin-bot
cd /opt/ai4all-weixin-bot

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
```

如果服务器系统 `python3` 版本过旧，先安装或指定 Python 3.11+ 创建 venv。不要用裸
`python` / `python3` 启动服务，统一使用 `.venv/bin/python`、`.venv/bin/pip`、
`.venv/bin/uvicorn`：

```bash
python3 --version
.venv/bin/python --version
.venv/bin/python -m pip check
```

编辑 `.env`，至少替换：

- `AI4ALL_BRIDGE_SECRET`
- `ADMIN_TOKEN`
- `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_ACTIVE_FAMILY`（模型由 family×tier 内置矩阵解析，不再单配 `LLM_MODEL`；详见 [LLM family×tier 设计](../architecture/agent-runtime/llm_family_tier_design.md)）
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

如果仓库实际部署路径不是 `/opt/ai4all-weixin-bot`，同步替换
`WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`。本次阿里云节点实测路径为
`/home/jack/ai4all_bridge`，服务用户为 `jack`。

**中心角色（`AI4ALL_ROLE` 含 `central`）的单元还必须带一行**：

```ini
Environment=AI4ALL_ALLOW_AUTO_MIGRATE=1
```

这是 PG schema 迁移的 opt-in 闸（`app/db/_core.py` 的 `AUTO_MIGRATE_ENV`）：PG 上存在待执行
迁移时，只有带该变量的进程可以应用，其余进程直接报错退出。目的是挡住「开发机 = 生产机」
场景下，自测时随手跑一条命令就把未评审的迁移写进生产库（2026-07-16 / 07-29 / 07-30 各发生
过一次）。**绝不能把它写进 `.env`**——`.env` 会被同目录任何手跑的 python 进程读到，闸门等于
不存在。节点角色（`AI4ALL_ROLE=node`）本就跳过 `init_db`，不需要该变量。

确需在部署之外手工迁移生产库时，显式加前缀：
`AI4ALL_ALLOW_AUTO_MIGRATE=1 .venv/bin/python -c "from app.db import init_db; init_db()"`。
本机自测请改用 SQLite（`DATABASE_URL="" .venv/bin/pytest …`）或临时库。

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai4all-weixin-backend
sudo systemctl status ai4all-weixin-backend
curl http://127.0.0.1:8180/health
curl http://127.0.0.1:8180/health/ready
```

`/health/ready` 在非 local 环境会检查生产关键配置：`LLM_API_KEY`、
`AI4ALL_BRIDGE_SECRET`、`ADMIN_TOKEN`，并验证当前数据库连接、`data/user_profiles`
和 `data/system` 可用。首次部署如果这里返回 503，先修 `.env`、PG 连接或目录权限，
不要继续接入公网流量。

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

## 健康监控 systemd timer

确认 `.env` 已配置 `FEISHU_ALERT_WEBHOOK_URL` 后，建议用 systemd timer 每分钟运行轻量监控。
仓库已提供通用 systemd 模板：`deploy/systemd/ai4all-monitor-health.service`
和 `deploy/systemd/ai4all-monitor-health.timer`。

创建 `/etc/systemd/system/ai4all-monitor-health.service`：

```ini
[Unit]
Description=AI4ALL health monitor

[Service]
Type=oneshot
WorkingDirectory=/opt/ai4all-weixin-bot
EnvironmentFile=/opt/ai4all-weixin-bot/.env
Environment=MONITOR_READY_URL=http://127.0.0.1:8180/health/ready
Environment=MONITOR_SCHEDULERS=proactive_scheduler:90
Environment=MONITOR_CHECK_OPENCLAW=true
Environment=MONITOR_OPENCLAW_CHANNEL=openclaw-weixin
ExecStart=/opt/ai4all-weixin-bot/.venv/bin/python scripts/monitor_health.py
User=ai4all
Group=ai4all
```

创建 `/etc/systemd/system/ai4all-monitor-health.timer`：

```ini
[Unit]
Description=Run AI4ALL health monitor every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
Unit=ai4all-monitor-health.service

[Install]
WantedBy=timers.target
```

如果仓库实际部署路径不是 `/opt/ai4all-weixin-bot`，同步替换
`WorkingDirectory`、`EnvironmentFile`、`ExecStart`、`User` 和 `Group`。本次阿里云节点实测路径为
`/opt/workspace/ai4all_bridge`，服务用户为 `jack`。

启用前先 dry-run：

```bash
cd /opt/ai4all-weixin-bot
MONITOR_SCHEDULERS=proactive_scheduler:90 \
MONITOR_CHECK_OPENCLAW=true \
.venv/bin/python scripts/monitor_health.py --dry-run
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai4all-monitor-health.timer
sudo systemctl list-timers --all | grep ai4all-monitor-health
journalctl -u ai4all-monitor-health.service -n 50 --no-pager
```

## nginx 反代

> **真源在仓库**：aliyun1 的实际 vhost 已纳入 git，见 [`deploy/nginx/`](../../deploy/nginx/)
> （`ai4company.top.conf` 公网域名、`ai4all-node.conf` 内网节点入口）。改动后用
> `scripts/deploy_nginx.sh`（备份 → 写入 → `nginx -t` 失败自动回滚 → reload）同步上线，
> **不要直接手改 `/etc/nginx/conf.d/`**，否则与 git 漂移。basic auth 凭据
> `.ai4all_ops.htpasswd` 是机密、不在 git，创建方式见 [`deploy/nginx/README.md`](../../deploy/nginx/README.md)。
> 下方代码块仅为结构示例。

示例只展示核心策略，证书可用阿里云证书服务或 certbot 管理。公网路径约定：

- `/`：用户主页，反代到 Backend `/ui/home.html`。
- `/user/dashboard.html`：用户中心，反代到 Backend `/ui/dashboard.html`。
- `/api/web/*`：用户侧前端 API，反代到 Backend `/web/*`。
- `/v1/media/*`：App 媒体读端点，反代到 Backend 同名路径。签名三元组即凭据、不读
  `Authorization`；服务端签发的读 URL 是 origin 相对路径，所以这条必须直连后端，
  不能落到官网 SPA catch-all（否则返回 200 HTML，客户端与图片机审都取不到图）。
- `/api/health`：公开基础存活检查，反代到 Backend `/health`。
- `/api/health/ready`：生产 readiness，只允许本机、监控或办公网访问。
- `/ops/*`：运营后台、Debug UI、Admin/Debug API 和 Swagger，必须通过 allowlist/VPN 保护。
- 旧公网路径 `/ui/*`、`/web/*`、`/health*`、`/admin/*`、`/debug/*`、`/docs` 不再作为公网入口。

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.example;

    location = / {
        proxy_pass http://127.0.0.1:8180/ui/home.html;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /user/dashboard.html {
        proxy_pass http://127.0.0.1:8180/ui/dashboard.html;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /site.css {
        proxy_pass http://127.0.0.1:8180/ui/site.css;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /user/site.css {
        proxy_pass http://127.0.0.1:8180/ui/site.css;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /brand.png {
        proxy_pass http://127.0.0.1:8180/ui/brand.png;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /api/web/ {
        rewrite ^/api/web/(.*)$ /web/$1 break;
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /api/health {
        proxy_pass http://127.0.0.1:8180/health;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /api/health/ready {
        allow <your-office-ip>;
        deny all;
        proxy_pass http://127.0.0.1:8180/health/ready;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /ops {
        return 302 /ops/;
    }

    location = /ops/ {
        allow <your-office-ip>;
        deny all;
        proxy_pass http://127.0.0.1:8180/ui/index.html;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ~ ^/ops/(index|ops|account|realtime_inbound|reminder_debug|onboarding_debug|proactive_debug|web_search_debug)\.html$ {
        allow <your-office-ip>;
        deny all;
        rewrite ^/ops/(.*)$ /ui/$1 break;
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ~ ^/ops/(style\.css|admin\.js)$ {
        allow <your-office-ip>;
        deny all;
        rewrite ^/ops/(.*)$ /ui/$1 break;
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /ops/admin/ {
        allow <your-office-ip>;
        deny all;
        rewrite ^/ops/admin/(.*)$ /admin/$1 break;
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /ops/debug/ {
        allow <your-office-ip>;
        deny all;
        rewrite ^/ops/debug/(.*)$ /debug/$1 break;
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ^~ /ops/openclaw/ {
        allow <your-office-ip>;
        deny all;
        rewrite ^/ops/openclaw/(.*)$ /openclaw/$1 break;
        proxy_pass http://127.0.0.1:8180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location = /ops/docs {
        allow <your-office-ip>;
        deny all;
        proxy_pass http://127.0.0.1:8180/docs;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        sub_filter_once off;
        sub_filter '/openapi.json' '/ops/openapi.json';
    }

    location = /ops/openapi.json {
        allow <your-office-ip>;
        deny all;
        proxy_pass http://127.0.0.1:8180/openapi.json;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location ~ ^/(ui|web|admin|debug|openclaw)/ {
        return 404;
    }

    location ~ ^/(health|docs|openapi\.json)$ {
        return 404;
    }
}
```

改完先验证再 reload：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

公网路径验收示例：

```bash
for url in \
  https://your-domain.example/ \
  https://your-domain.example/user/dashboard.html \
  https://your-domain.example/api/health \
  https://your-domain.example/api/web/me \
  https://your-domain.example/api/health/ready \
  https://your-domain.example/ui/home.html \
  https://your-domain.example/web/me \
  https://your-domain.example/health \
  https://your-domain.example/admin/me \
  https://your-domain.example/docs \
  https://your-domain.example/ops/index.html; do
  code=$(curl -s -o /tmp/ai4all_pathcheck.out -w '%{http_code}' "$url")
  printf '%s %s\n' "$code" "$url"
done
```

预期：

- `/`、`/user/dashboard.html`、`/api/health` 返回 200。
- `/api/web/me` 未登录返回 401。
- `/api/health/ready` 和 `/ops/*` 在非 allowlist 来源返回 403。
- 旧公网路径 `/ui/*`、`/web/*`、`/health`、`/admin/*`、`/debug/*`、`/docs` 返回 404。

从服务器本机验证 `/ops/*` allowlist 映射时，绕过代理直连本机 443：

```bash
curl --noproxy '*' -k \
  --resolve your-domain.example:443:127.0.0.1 \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  https://your-domain.example/ops/admin/me
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

> ⚠️ 本文出现的 `openclaw gateway restart` 都会让该机**全部**微信账号同时重连。账号量上来后这是风控高危操作（见 [`production_runbook.md` 规模化运维红线](production_runbook.md#规模化运维红线必读)）：仅在部署/补丁等必要时执行，且避开活跃时段；日常运维优先单账号粒度，不要把它当常规步骤。

避免手工复制 secret 出错，可以直接从 `.env` 同步：

```bash
bridge_secret=$(awk -F= '$1=="AI4ALL_BRIDGE_SECRET" {print $2}' .env)
openclaw config set plugins.entries.ai4all-openclaw-bridge.config.secret "$bridge_secret"
openclaw gateway restart
```

验证：

```bash
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe
```

预期 runtime inspect 中能看到 `before_agent_reply` hook。

## openclaw-weixin QR 登录补丁

> **前置（换机首部署）**：`@tencent-weixin/openclaw-weixin` 是官方微信渠道插件，需先随 OpenClaw 渠道安装流程装好（`openclaw channels status --probe` 能看到 `openclaw-weixin` 即已安装）。当前线上版本 **pin 在 2.4.4**（安装清单是精确版本号，不会自动升级）。装好插件后，再依次应用本节（QR）和下一节（解绑登出）两个补丁。

当前仓库已经跟踪以下补丁和说明文件：

- `patches/openclaw-weixin-gateway-methods-runtime.patch`
- `docs/architecture/shared/access/openclaw_weixin_gateway_qr_patch.md`

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

## openclaw-weixin 解绑登出补丁（logoutAccount）

> ⚠️ **本节是部署/升级时的重点关注项。** 解绑（`/web/me/unbind`）需要 OpenClaw gateway 的 `logoutAccount` 才能删除微信账号文件、停止该 bot 长轮询。官方 `@tencent-weixin/openclaw-weixin@2.4.4` **不含** `logoutAccount`，必须热补丁；否则解绑只清 AI4ALL 侧，weixin 账号文件 + 索引残留 = **孤儿 bot 仍在线收消息**。

当前仓库已跟踪：

- `patches/openclaw-weixin-logout-account-runtime.patch`
- `docs/architecture/shared/access/openclaw_weixin_gateway_logout_patch.md`（完整原理、稳健部署流程、回滚、对账法）

**部署/重放（插件升级后必做）：**

```bash
PLUGIN=~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin
# 0) 先确认是否已丢失（升级后大概率为 0）
grep -c logoutAccount $PLUGIN/dist/src/channel.js
# 1) 整库备份 → 2) 打补丁 → 3) 语法预检 → 4) 重启（详见专门文档 §5）
tar czf ~/.openclaw/_logout_bak_$(date +%s).tgz -C ~/.openclaw openclaw-weixin
( cd $PLUGIN && patch -p1 < /opt/ai4all-weixin-bot/patches/openclaw-weixin-logout-account-runtime.patch )
node --check $PLUGIN/dist/src/channel.js
systemctl --user restart openclaw-gateway.service
```

**⚠️ 切勿从 workspace `openclaw-weixin`（v2.4.3）`npm run build` 覆盖线上 dist。** 腾讯只发布了 2.4.4 的 npm 产物、未推 2.4.4 源码；v2.4.4 dist 比 v2.4.3 源码多 20 个模块（streaming、voice-outbound、batch-session、buttons…），build 覆盖会静默大规模回退。在上游发布含 logout 的 2.4.4+ 源码前，**热补丁是唯一正确路径**。

**验证：** `openclaw channels logout --channel openclaw-weixin --account <已确认孤儿 bot>` 不再报 `does not support logout`，且对应 `~/.openclaw/openclaw-weixin/accounts/{bot}.*` 文件消失、`accounts.json` 索引除名。**勿对在用账号测试**——删前先按专门文档 §6 对账法确认无活绑定。

## 数据备份

发布、重启或迁移前使用统一脚本备份。生产 PG 分支会执行 `pg_dump -Fc` 并校验 dump 目录；
不要再手工复制历史 SQLite 文件：

```bash
cd /opt/ai4all-weixin-bot
.venv/bin/python scripts/backup_data.py --dry-run
.venv/bin/python scripts/backup_data.py
```

生产已使用定时备份；异机 `pg_dump`、流复制与恢复演练状态见
[PG 备份与切换跟踪](pg_backup_failover_tracking.md)。

恢复时必须停止两台机器上的业务写入和 central-only scheduler，再恢复到 PostgreSQL 并完成一致性
核对。`scripts/restore_data.py` 仅支持 SQLite 开发档，不得用于生产恢复。

```bash
sudo systemctl stop ai4all-weixin-proactive-scheduler
sudo systemctl stop ai4all-weixin-backend
```

恢复数据库和 system context 后再按 central/node 角色启动服务。

## 发布检查

```bash
.venv/bin/pytest tests/ -v
curl http://127.0.0.1:8180/health
curl http://127.0.0.1:8180/health/ready
curl https://your-domain.example/api/health
.venv/bin/python scripts/send_mock_turn.py --url http://127.0.0.1:8180 --text "hello"
.venv/bin/python scripts/check_prompt.py --url http://127.0.0.1:8180 --account aid_806382741 --token "$ADMIN_TOKEN"
openclaw plugins inspect ai4all-openclaw-bridge --runtime
openclaw channels status --probe

# 补丁生效校验（换机首部署 / 插件升级后必查）——任一为 0 即补丁丢失，需按对应节重打
PLUGIN=~/.openclaw/npm/projects/tencent-weixin-openclaw-weixin-*/node_modules/@tencent-weixin/openclaw-weixin
grep -c gatewayMethods $PLUGIN/dist/src/channel.js   # QR 登录补丁，期望 >0
grep -c logoutAccount  $PLUGIN/dist/src/channel.js   # 解绑登出补丁，期望 >0（应为 4）
```

真实冒烟：

1. 打开 `https://your-domain.example/` 并完成手机号、验证码、短信 OTP。
2. 扫码绑定，确认 `binding_intent` 完成且写入 `channel_bindings`。
3. 发送微信私聊，确认 `/openclaw/turn` 路由到预创建的 `aid_...` 并回复。
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

数据回滚必须先停止所有 Backend 和 scheduler，保留当前 PG 现场，再按 PostgreSQL 备份/主备流程
恢复。禁止删除 `DATABASE_URL` 回落历史 SQLite 快照。
