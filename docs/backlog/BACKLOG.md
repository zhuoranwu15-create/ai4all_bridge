# 工程 Backlog（轻量待办清单）

> 用途:集中登记**不阻塞线上、影响可控**的工程改进项,避免散落在各设计文档里被遗忘。
> 这里只放**单行条目 + 指回详情**,不写正文;详细方案留在来源文档。
> 阻塞性/线上正确性问题不进这里——直接修并在对应 runbook/design 记录。
>
> 状态图例:🔴 待排期 · 🟡 进行中 · ✅ 已完成(完成后保留一行存档,定期清理)
> 最后更新:2026-06-30(SEC-1/2/4、BUG-1 已修并移入 §4 存档)
>
> 范围约定：跨产品和平台事项放在本文；朝夕专属事项放在
> [`products/zhaoxi/`](products/zhaoxi/)，共享分析和规模化评估放在
> [`shared/`](shared/)。

---

## 1. 部署 / 基础设施

| ID | 优先级 | 状态 | 条目 | 来源 |
|---|---|---|---|---|
| INFRA-1 | P2 | 🔴 | aliyun2 出站直连 PG 认领,去掉绕中心 HTTP `/node/outbound/claim`(降耦合/延迟) | `ops/platform/aliyun1_aliyun2_deployment_diff.md` O4 |
| INFRA-2 | P2 | 🔴 | PG 单点无 HA;配流复制热备 + `pg_dump` PITR | `ops/platform/p5_production_upgrade_runbook.md` §10 / `architecture/shared/data/thick_node_postgres_refactor.md` §8 |
| INFRA-3 | P3 | 🔴 | `max_connections=100` 余量核对:账号扩容前复核 Σ(各节点 `db_pool_max`)+中心+timer | deployment_diff O7 |
| INFRA-4 | P3 | 🔴 | OpenClaw 版本漂移:aliyun1 v5.28 升 6.x 时随 `patch_openclaw_accountid.sh` 补齐 4 补丁 | deployment_diff O8 / `architecture/shared/access/openclaw_patches_maintenance.md` |
| INFRA-5 | P3 | 🔴 | 厚节点改造阶段三:aliyun1 退化为纯 `central`(停跑 turn) | p5_runbook §8 |

## 2. 架构基线 / 可拓展性(2026-06-21 厚节点后架构审查)

| ID | 优先级 | 状态 | 条目 | 来源 |
|---|---|---|---|---|
| ARCH-1 | P1 | 🔴 | 主动消息建 category registry(仿 tools `_META` 单一事实源+启动校验),消除 category 跨 6 处手工同步 | `plans/products/zhaoxi/baseline_optimization_alignment.md` |
| ARCH-2 | P1 | 🔴 | provider 怪癖(DSML 解析、强制 tool_choice 正则)从编排/通用 LLM 层下沉到 `llm_adapters` | 同上 |
| ARCH-3 | P2 | 🔴 | 巨型文件按职责拆分:`turn_service.py`(1848 行)`_finalize_turn`、`account_checks.py`(985)、`reactivation.py`(880) | 同上 |
| ARCH-4 | P2 | 🔴 | 工具 handler 统一签名 `handler(args, ctx)`,删除三种 `call_style` 泄漏抽象 | 同上 |
| ARCH-5 | P2 | 🔴 | 工具结果回灌 LLM 前按 token 上限截断;tool loop 加整体 wall-clock 预算(现仅 per-provider 软超时) | 同上 |
| ARCH-6 | P3 | 🔴 | 抽 `proactive/_common.py` 去重 `_extract_json_object`/`_clean_text`/`_select_route`;抽 dispatcher 模板 | 同上 |
| ARCH-7 | P2 | 🔴 | 统一 `SafeHttpClient`(`trust_env=False`+`follow_redirects=False`+transport 层固定/复检 IP),所有出站 HTTP(web_fetch/web_search/tdai)必经此,新工具默认安全 | review SEC-6 |
| ARCH-8 | P2 | 🔴 | 不可信内容统一 projection 入口:external_content/TDAI recall/工具返回 走同一结构化标签+注入纪律白名单;注册表加 `is_external` 字段,handler 不再自声明 `wrapped` | review SEC-8 |
| ARCH-9 | P1 | 🔴 | 主动消息统一出站状态机闭环:enqueue 模式 `pending` 语义被所有上游正确处理 + `claim` 超时回收 + `max_attempts` 毒消息封顶(对齐 outbound_messages) | review BUG-1/BUG-2 |
| ARCH-10 | P2 | 🔴 | account_id 隔离从「调用层约定」升级为「DB 层强制」:`update_reminder`/`claim_due_*` 等按 id 写的函数补 `AND account_id=?` | review BUG-9 |

## 3. 安全 / 正确性 Review(2026-06-30 阶段性审查)

> 来源:厚节点 + TDAI/token 预算升级后的一次分域 code review。条目按严重度,标「已核验」为本次直接读码确认,「待核验」为 agent 报告未二次确认。
> 注:`asyncio.run` 死锁(turn_service.py:224)经核验为**误报**——bridge 路由是同步 `def`,跑在 threadpool 无 running loop,不计入。

### 安全

| ID | 优先级 | 状态 | 条目 | 位置 |
|---|---|---|---|---|
| SEC-1 | P0 | ✅ | `GET /web/binding-intents/{id}` 无鉴权+无属主校验,capability URL 泄露即可拿 qr/manual_login_command 劫持绑定 | `routers/web.py:940`(已修) |
| SEC-2 | P0 | ✅ | `POST /web/agents` 无鉴权,可为任意 platform_user_id 建账号刷配额 | `routers/web.py:897`(已修) |
| SEC-3 | P1 | 🔴 | 审核详情对 reviewer 角色返回 `snapshot_text` 用户原文未脱敏(列表有 pop,详情没有);待核验脱敏路径 | `routers/admin_moderation.py:268` |
| SEC-4 | P2 | ✅ | token 用 `==`/`!=` 非恒定时间比较;空 token 在 `APP_ENV` 漏配(默认 local)时放行 → 抽 `app/auth_utils.bearer_matches`(恒定时间+空 token 拒绝),bridge/admin/reviewer/staff/node-agent 全用 | `routers/deps.py`/`node_agent.py`(已修) |
| SEC-5 | P2 | 🔴 | `debug_update_profile` 返回 `system_prompt` 等原文未走 `_profile_for_view` | `routers/debug.py:769` |
| SEC-6 | P2 | 🔴 | SSRF 纵深:URL guard 两次 DNS 间 TOCTOU(rebinding);`web_search` httpx `follow_redirects=True` 不过 guard | `tools/_url_guard.py`/`web_search.py`(见 ARCH-7) |
| SEC-7 | P3 | 🔴 | `web_fetch` `resp.read()[:max]` 先全量入内存再切片,大响应 OOM 面 → 改 `iter_bytes` 累计截断 | `tools/web_fetch_handlers.py:117` |
| SEC-8 | P3 | 🔴 | 外部内容标记可被 handler 自声明 `externalContent.wrapped=true` 伪造,`read` 内部内容可绕过 untrusted 标记 | `tools/external_content.py:48`(见 ARCH-8) |
| SEC-9 | P3 | 🔴 | `record_content_invitation_feedback` 在无有效 invitation 时仍可写 preference 封锁任意 topic | `tools/content_invitation_handlers.py:181` |
| SEC-10 | P3 | 🔴 | FAQ 审核 LLM 异常 fail-silent:置 pending 永不发布、无人工队列、无告警 | `routers/web.py:484` |

### 正确性 / 健壮性

| ID | 优先级 | 状态 | 条目 | 位置 |
|---|---|---|---|---|
| BUG-2 | P1 | 🔴 | `claim_due_reminder`/`claim_due_proactive_commitment` 卡 `sending` 无超时回收,崩溃即永久丢失(对比 outbound_messages 有 claimed_at 回收) | `db/proactive.py:881,1176` |
| BUG-3 | P1 | 🔴 | TDAI 解绑 `namespace_wipe` fire-and-forget,失败仅 warning 无重试/补偿 → 记忆残留,重绑可召回,违反隔离不变量 | `tdai_client.py`/`routers/web.py:1118` |
| BUG-4 | P2 | 🔴 | 计费 `_apply_wallet_ledger_in_conn` check-then-act 非原子,PG `READ COMMITTED` 同 idempotency_key 并发可双扣余额 → wallet 行 `FOR UPDATE` 或 `ON CONFLICT DO NOTHING`+看 rowcount | `db/billing.py:713` |
| BUG-5 | P2 | 🔴 | weekly 周期提醒当天触发(`days_ahead==0`)落 `<=0` 分支 +7 实际 +14 天,每周变隔周 | `reminder_utils.py:35` |
| BUG-6 | P2 | 🔴 | 出站红线拦截后仍计费且推进 `turn_count`(条件缺 `not moderation_blocked`),配额/onboarding 计数偏差 | `turn_service.py:1626,1674` |
| BUG-7 | P2 | 🔴 | dreaming scheduler 多进程/多节点仅靠 node 分片+进程单例,运维配错即重复扫描→重复 carryover/记忆行 → business_day 级 DB 互斥锁 | `dreaming_scheduler.py` |
| BUG-8 | P3 | 🔴 | after-turn 5 处 `call_soon_threadsafe` 无 try/except,shutdown 期 in-flight 请求遇 closed loop 抛 RuntimeError→500 → 封装 `_schedule_background` 兜底 | `turn_service.py:1755+` |
| BUG-9 | P3 | 🔴 | `update_reminder`/`cancel_reminder` DB 层缺 `account_id` 约束(handler 层已 check,纵深缺失) | `db/proactive.py:1001`(见 ARCH-10) |
| BUG-10 | P3 | 🔴 | `_select_route` 取第一条 channel binding 无确定性排序,多 channel 账号可能发错渠道 | `proactive/_common.py:23` |
| BUG-11 | P3 | 🔴 | `capture_turn` 每次新建 `httpx.AsyncClient`,高并发 background 任务下连接/端口耗尽 → 复用模块级连接池 | `tdai_client.py:131` |
| BUG-12 | P3 | 🔴 | `context_summarizer` `window_oldest_id` 取跨 session 全局 min,老 session 压低致滚动摘要不触发(功能默认关) | `context_summarizer.py:130` |

## 4. 产品 / Onboarding 体验

| ID | 优先级 | 状态 | 条目 | 来源 |
|---|---|---|---|---|
| OB-1 | P2 | 🔴 | 绑定后 bot 主动问候(Path A)常失效(拿不到用户 wxid);调研 wxid 回填能否实现「绑定即主动问候」,当前退化为用户先开口(Path B) | `backlog/onboarding_welcome_wxid_followup.md` |

## 5. 已完成存档

| ID | 状态 | 条目 | 完成 |
|---|---|---|---|
| O1 | ✅ | account_checks send→dispatch(远程账号主动消息发不出) | commit 6e7aba3 |
| O2 | ✅ | PG 连接池 + shutdown 关池 + central 守卫 | commit 6e7aba3 |
| O3 | ✅ | 中心每日 dreaming 扫全量,修复 aliyun2 漏扫 | commit 520f0c6 |
| O9 | ✅ | `restart_runtime.sh` 按角色分流 | commit 6e7aba3 |
| SEC-1 | ✅ | `GET /web/binding-intents/{id}` 加 `_require_session`+属主校验(非属主 404),前端 onboarding 注入 token | 2026-06-30 review |
| SEC-2 | ✅ | `POST /web/agents` 加 `_require_session`+`platform_user_id` 属主校验(他人 403) | 2026-06-30 review |
| SEC-4 | ✅ | 抽 `app/auth_utils.bearer_matches`(恒定时间比较+空 token 永不通过),deps 4 处+node_agent 全用 | 2026-06-30 review |
| BUG-1 | ✅ | 多机 enqueue content_invitation 拉活状态机断裂(pending 时误 release 而非 invited,每 tick 重复 claim/release,邀请态无法收敛):发起侧 pending/sending 保留 sending 态,新增 node_outbound_result 回调推进 invited/release(对齐 ARCH-9 出站闭环) | `proactive/reactivation.py`+`routers/bridge.py`,2026-06-30 review |

---

> 维护约定:新增项追加单行;完成后移到 §4 存档区并标 commit;§4 积累过多时清理早期项。
> 详细技术方案不写在本文,放对应 design/runbook,本文只做索引与状态。
