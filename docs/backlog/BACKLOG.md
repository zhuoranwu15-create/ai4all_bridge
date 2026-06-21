# 工程 Backlog（轻量待办清单）

> 用途:集中登记**不阻塞线上、影响可控**的工程改进项,避免散落在各设计文档里被遗忘。
> 这里只放**单行条目 + 指回详情**,不写正文;详细方案留在来源文档。
> 阻塞性/线上正确性问题不进这里——直接修并在对应 runbook/design 记录。
>
> 状态图例:🔴 待排期 · 🟡 进行中 · ✅ 已完成(完成后保留一行存档,定期清理)
> 最后更新:2026-06-21

---

## 1. 部署 / 基础设施

| ID | 优先级 | 状态 | 条目 | 来源 |
|---|---|---|---|---|
| INFRA-1 | P2 | 🔴 | aliyun2 出站直连 PG 认领,去掉绕中心 HTTP `/node/outbound/claim`(降耦合/延迟) | `guides/aliyun1_aliyun2_deployment_diff.md` O4 |
| INFRA-2 | P2 | 🔴 | PG 单点无 HA;配流复制热备 + `pg_dump` PITR | `guides/p5_production_upgrade_runbook.md` §10 / `tech_design/thick_node_postgres_refactor.md` §8 |
| INFRA-3 | P3 | 🔴 | `max_connections=100` 余量核对:账号扩容前复核 Σ(各节点 `db_pool_max`)+中心+timer | deployment_diff O7 |
| INFRA-4 | P3 | 🔴 | OpenClaw 版本漂移:aliyun1 v5.28 升 6.x 时随 `patch_openclaw_accountid.sh` 补齐 4 补丁 | deployment_diff O8 / `tech_design/openclaw_patches_maintenance.md` |
| INFRA-5 | P3 | 🔴 | 厚节点改造阶段三:aliyun1 退化为纯 `central`(停跑 turn) | p5_runbook §8 |
| INFRA-6 | P3 | 🔴 | `docs/tmp/` 残留非文档(`ai4all-node.conf` / `check_daily_gate.py`)迁入 `deploy/` 或 `scripts/` | 本次文档分诊 |

## 2. 架构基线 / 可拓展性(2026-06-21 厚节点后架构审查)

| ID | 优先级 | 状态 | 条目 | 来源 |
|---|---|---|---|---|
| ARCH-1 | P1 | 🔴 | 主动消息建 category registry(仿 tools `_META` 单一事实源+启动校验),消除 category 跨 6 处手工同步 | `tech_design/baseline_optimization_alignment.md` |
| ARCH-2 | P1 | 🔴 | provider 怪癖(DSML 解析、强制 tool_choice 正则)从编排/通用 LLM 层下沉到 `llm_adapters` | 同上 |
| ARCH-3 | P2 | 🔴 | 巨型文件按职责拆分:`turn_service.py`(1848 行)`_finalize_turn`、`account_checks.py`(985)、`reactivation.py`(880) | 同上 |
| ARCH-4 | P2 | 🔴 | 工具 handler 统一签名 `handler(args, ctx)`,删除三种 `call_style` 泄漏抽象 | 同上 |
| ARCH-5 | P2 | 🔴 | 工具结果回灌 LLM 前按 token 上限截断;tool loop 加整体 wall-clock 预算(现仅 per-provider 软超时) | 同上 |
| ARCH-6 | P3 | 🔴 | 抽 `proactive/_common.py` 去重 `_extract_json_object`/`_clean_text`/`_select_route`;抽 dispatcher 模板 | 同上 |

## 3. 已完成存档

| ID | 状态 | 条目 | 完成 |
|---|---|---|---|
| O1 | ✅ | account_checks send→dispatch(远程账号主动消息发不出) | commit 6e7aba3 |
| O2 | ✅ | PG 连接池 + shutdown 关池 + central 守卫 | commit 6e7aba3 |
| O3 | ✅ | 中心每日 dreaming 扫全量,修复 aliyun2 漏扫 | commit 520f0c6 |
| O9 | ✅ | `restart_runtime.sh` 按角色分流 | commit 6e7aba3 |

---

> 维护约定:新增项追加单行;完成后移到 §3 存档区并标 commit;§3 积累过多时清理早期项。
> 详细技术方案不写在本文,放对应 design/runbook,本文只做索引与状态。
