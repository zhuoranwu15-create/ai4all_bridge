# nginx 反代配置（仓库为单一真源）

aliyun1 的 nginx vhost 以本目录为准，安装到 `/etc/nginx/conf.d/`。改动这里后用
`scripts/deploy_nginx.sh` 同步上线，**不要**再直接手改 `/etc/nginx/conf.d/`（会与 git 漂移）。

## 文件

| 文件 | 作用 | 部署位置 |
|---|---|---|
| `ai4company.top.conf` | 公网域名 `ai4company.top` vhost：用户主页 / `/api/web` / `/api/v1` App API / `/ops/*` 运维台 + Debug UI | aliyun1 `/etc/nginx/conf.d/` |
| `ai4all-node.conf` | 内网 vhost：仅允许 aliyun2 内网访问，反代 `/openclaw/turn`、`/node/*`、健康探针 | aliyun1 `/etc/nginx/conf.d/` |

> aliyun2 不跑面向公网的 nginx（其 `conf.d` 为空）；节点流量由 aliyun1 的 `ai4all-node.conf` 经 `/node/`、`/openclaw/turn` 反代过去。

## 部署

```bash
# 预览与线上差异 + 语法预检，不写入
scripts/deploy_nginx.sh --dry-run

# 备份线上 -> 写入 -> nginx -t（失败自动回滚）-> reload
scripts/deploy_nginx.sh
```

脚本内部用 `sudo`（写 `/etc/nginx` 与 reload 需 root），运行时可能要求输入密码。

## 机密：basic auth（不在 git）

`/ops/*` 运维台和 Debug UI 受 nginx HTTP basic auth 保护，凭据文件
`/etc/nginx/.ai4all_ops.htpasswd` **是机密，不纳入 git**。首次部署或新增运维账号：

```bash
# 首次创建（-c 仅首次；追加账号去掉 -c）
sudo htpasswd -c /etc/nginx/.ai4all_ops.htpasswd <用户名>
sudo systemctl reload nginx
```

## 访问保护层次（以 `/ops/prompt_debug.html` 为例）

1. nginx basic auth（本文件的 htpasswd）——否则连页面都 401。
2. 页面内 Admin Bearer token（`/admin`、`/debug` API 均校验）。
3. 看明文 trace 还需账号在后端 `ADMIN_DEBUG_PLAINTEXT_ACCOUNT_ALLOWLIST`。

新增 Debug UI 页时注意两道放行：① 本文件约 130 行的 `/ops/(...)\.html$` 白名单正则要加页名；
② 后端 `app/main.py` 的 `_LOCAL_ONLY_DEBUG_UI_PATHS` 若包含该页，生产会 403。
