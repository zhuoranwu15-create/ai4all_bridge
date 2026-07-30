# 图片机审开通手册（阿里云内容安全 2.0）

适用范围：**朝夕相伴 App 侧的用户图片**（会话图片 + 图文动态），即 v1.5 S4 落地的
`media_assets` 机审链路。微信入站图片走的是另一条老链路（`content_moderation_tasks` 队列
+ worker），本文不涉及。

一句话前提：**代码上线不等于能力开启**。S4 的代码合并后机审仍然是关的，必须完成本文
第 1–3 节的人工配置才会真正送审；配置不全时链路整体不报错、不阻塞发布，图片的
`moderation_status` 恒为 `skipped`。

审核口径见开发计划的 D-7：**先发后审、仅红线**。图片发出去立即可见，机审在后台补做；
命中红线（`block` / `escalate`）时图文动态终态下架，会话图片只落审核结论、不撤回消息
（撤回能力排在 v1.6）。阿里云的 `review` 中间档**放过并记日志**，不自动删。

---

## 1. 阿里云控制台

1. 开通**内容安全增强版（内容安全 2.0）**，产品码 `green20220302`，与文本审核是同一个产品；
   文本审核已经在用的话，这一步通常已经完成，只需确认「图片审核」能力可用。
2. 在「图片审核」里确认要用的**服务场景码**（`service`）。基线检测是 `baselineCheck`；
   如果控制台里为本业务单独建了自定义场景，用那个场景码。这个值填到
   `MODERATION_IMAGE_SAFETY_MODEL`，代码不做校验——填错会被阿里云拒掉并按调用失败重试。
3. 确认 endpoint 与开通的地域一致。默认 `green-cip.cn-beijing.aliyuncs.com`（北京）。
4. 确认账户余额/后付费已开。**欠费表现为持续调用失败**，链路侧只会看到 `errors` 计数上涨，
   图片一律 fail-open 放过（见 §5）。

### RAM 权限

复用现有的内容安全 AK/SK 即可（`ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET`，
文本审核用的就是这一对）。若要单独建子账号，最小权限是内容安全的调用权限
（系统策略 `AliyunYundunGreenWebFullAccess`，或自定义只放开 `ImageModeration`）。
**不要**用主账号 AK。

---

## 2. 媒体读端点必须公网可达（唯一的硬约束）

阿里云 `ImageModeration` 的 `serviceParameters` 只接受 `imageUrl`（公网可取）或 OSS 对象，
**不收字节流**。我们的图落在中心机本地磁盘、只通过签名 URL 暴露，所以送审时服务端会
现签一个**短 TTL 的主人 scope 读 URL**（`GET /v1/media/{media_id}?scope=pu:<uid>&exp=…&sig=…`）
交给阿里云回源下载。

因此必须配置 `MEDIA_PUBLIC_BASE_URL` = 媒体读端点的公网绝对基址（如 `https://api.example.com`，
不带尾斜杠、不带路径）。校验方法：在**外网**机器上执行

```bash
curl -sI "https://api.example.com/v1/media/whatever"
```

期望拿到 4xx 的业务响应（签名缺失/无效），而不是连接超时或 nginx 502——能连通并进到应用层就够了。

两个必须注意的点：

- **签的是主人自己的 scope，不新增鉴权面**：机审用的 URL 与主人自己看图用的是同一条路径、
  同一套签名，没有为审核开任何免鉴权后门。
- **URL 带签名，因此禁止进日志**。`image_review.py` 与批处理在所有日志分支上都只打
  `media_id`，改代码时不要顺手把 `image_url` 加进去。

如果 `MEDIA_PUBLIC_BASE_URL` 留空，`media_moderation_ready()` 为 False → 批处理直接返回
`status=disabled`，**连库都不读**，图片也不会入待审队列。这是刻意的：签不出可回源的地址时，
排队只会堆一队永远处理不掉的待办。

---

## 3. 环境变量

写进中心机（有 `central` 角色的那台）的 `.env`。`.env.example` 里有同样的行内注释。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `MODERATION_IMAGE_SAFETY_ENABLED` | 是 | 图片机审总开关，置 `true` |
| `MODERATION_IMAGE_SAFETY_MODEL` | 是 | 阿里云图片审核场景码，如 `baselineCheck` |
| `ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET` | 是 | 内容安全 AK/SK（与文本审核共用） |
| `MODERATION_ALIYUN_ENDPOINT` | 是 | 默认 `green-cip.cn-beijing.aliyuncs.com` |
| `MEDIA_PUBLIC_BASE_URL` | 是 | 媒体读端点的公网基址，见 §2 |
| `MEDIA_MODERATION_INTERVAL_SECONDS` | 否 | 扫描间隔，默认 60 |
| `MEDIA_MODERATION_BATCH_SIZE` | 否 | 单轮处理上限，默认 50 |

上面 5 个必填项**缺任意一个即整体未启用**（不是降级，是不启用）。改完 `.env` 后重启
主动调度器进程（重启方式见 [`README.md`](README.md) 指向的部署文档），机审步骤挂在它的
每个 tick 上。

**只在中心机挂载**：待审队列是中心库里的 App 内容，送审 URL 也必须指向中心机的媒体读端点，
纯 node 挂它只会签出自己取不到的地址。`scripts/run_proactive_scheduler.py` 已按
`settings.has_central_role` 判定，运维不需要额外操作。

---

## 4. 验证

配完之后按顺序验三步。

**第一步：能力是否识别到。** 手动跑一轮（强制跳过节流）：

```bash
curl -sX POST "http://127.0.0.1:8180/admin/proactive/scheduler/run-once" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python -m json.tool
```

看返回里的 `run.media_moderation`：

- `{"status": "disabled", ...}` → §3 的 5 个必填项还有缺口，或者这台机器没有 central 角色。
- `{"status": "ok", "scanned": 0, ...}` → 配置已生效，只是队列里没有待审图片。

**第二步：真送一张图。** 用测试账号在 App 里发一条带图动态，然后再跑一次 run-once，
期望 `scanned=1`、`passed=1`。库里核对：

```sql
SELECT id, moderation_status, moderation_attempts
FROM media_assets ORDER BY created_at DESC LIMIT 5;
```

正常图片应从 `pending` 变成 `passed`。日志里对应一行
`aliyun image moderation done data_id=… level=pass`（logger `ai4all.moderation.image_review`）。

**第三步：确认红线路径。** 用一张确定会被阿里云判 `block` 的图走同样流程，期望
`rejected=1`、该动态在主人 Feed 里立刻不可见、`universe_posts.status='deleted'` 且
`terminal_reason='moderation'`。这一步会真删内容，**只在测试账号上做**。

---

## 5. 状态机与失败语义（排障时看这一节）

`media_assets.moderation_status` 就是状态机本身，App 侧内容**不进** `content_moderation_tasks`
队列（那张表的 `account_id` 外键指向 `accounts`，而 App 内容属于 `platform_users`）。

| 值 | 含义 |
| --- | --- |
| `skipped` | 默认值。未开机审时的一切图片，以及重试耗尽后 fail-open 放过的图片 |
| `pending` | 已发出、待审（只有图片、且只在机审配好时才会入队） |
| `passed` | 审过放行（含阿里云 `review` 中间档） |
| `rejected` | 命中红线，已执行下架动作 |

结案是 `pending → 终态` 的原子写（`WHERE moderation_status='pending'`），所以多进程重复扫描
最多多打一次云接口，不会出现两次下架。

失败语义，按排障时最常遇到的顺序：

- **云调用失败**（欠费、AK 失效、场景码错、网络超时、阿里云回源取不到我们的图）：图片留在
  `pending`，`moderation_attempts` +1，下一轮重试。**次数在调用之前就加**，所以进程崩在
  半路也照样计数。
- **重试耗尽**（`moderation_attempts >= 3`）：**fail-open 记 `skipped`**，不是 `rejected`。
  机审自己坏了不该删用户内容。这行随即离开待审索引——**因此云侧配错的表现是"图片全被放过"，
  不是"队列越堆越长"**。判据只能靠日志 `media moderation gave up media_id=… -> skipped`
  与 run-once 返回里的 `exhausted` 计数，值得做告警。
- **下架失败**（判了红线但 outbox 写不进去）：**不结案**，留在 `pending` 下一轮重试，
  `takedown_errors` +1。宁可重试（下架幂等，首次写入者胜出）也不能留下"已判红线却还在线上"。
- **`review` 中间档**：放过并写 `passed`，日志留 `media moderation passed with review level`
  加命中的 categories，供运营事后人工处置。

run-once / 每 tick 的返回都是同一组计数：`scanned / passed / rejected / errors / exhausted /
takedown_errors`（logger `ai4all.media.moderation` 也会汇总打一行）。

---

## 6. 关掉

把 `MODERATION_IMAGE_SAFETY_ENABLED` 置 `false`（或清空 `MEDIA_PUBLIC_BASE_URL`）并重启调度器。
此后新图不再入队，已在 `pending` 的图片会一直留在 `pending`——不影响任何用户可见行为
（先发后审，图早就可见了）；重新开启后会被继续扫到。
