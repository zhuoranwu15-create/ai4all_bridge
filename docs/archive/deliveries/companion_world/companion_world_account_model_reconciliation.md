# 账号模型对齐：PR #44（M1）× main #42（App/账号收敛）

> 状态：**已与产品对齐、方案冻结 = B（2026-07-21）**。本文是把 PR #44 并入主干前唯一需拍板的口径记录，供实现与评审引用。
> 关联：ADR D-02/D-07/D-09/D-14、[`../../../architecture/designs/companion_world_3_0_refactor_design.md`](../../../architecture/products/mingchan/companion_world_3_0_refactor_design.md)；main 已合并的 #42「App/账号收敛」。

---

## 1. 冻结的账号模型（产品口径 → 代码口径）

| 产品说法 | 代码承载 | 基数 |
|---|---|---|
| 一真人 = 一手机号 | `platform_user` | 1 |
| 同一 App 内「一人多**号**」不存在 | 用户账号（用户面登录身份） | 一 App ≤ 1 |
| 一人可以有多个 **agent** | 每个居民 = 一行 runtime `account`（隔离的 Soul/记忆/关系，ADR D-02） | 1–10 |
| 一人一个世界，居民属于世界 | `universes`（一 owner 一条）+ `universe_residents.runtime_account_id` | 世界 1 / 居民 N |
| 一个钱包 | `entitlement_wallets` 锚 `platform_user_id`（M1/D-14） | 1 |

**核心澄清（本次对齐的要害，须写进代码注释与文档）：**
`account_owner_bindings` 是 **微信接入（形态 A）独有** 的产物——它是「微信渠道把一个 `account` 绑到某真人」的记录，**不是通用的"用户账号"机制**。

- 纯朝夕相伴用户（无微信）**没有任何 owner_binding**：其身份锚 = `platform_user` + 那一个 `universe`；居民是世界里的 agent（runtime account），**都不发 owner_binding**。
- 「account」一词两义（用户账号 / AI 关系运行容器）是历史命名债，是本次困惑的根，需在关键处显式注释消歧。

---

## 2. 决策：B —— 居民不发 binding，钱包/配额解析走「世界归属」

「某个 `account` 属于哪个真人（去花哪个钱包、记哪套配额）」的解析链，从只认 owner_binding 扩为**两段 fallback**：

```
account
  ├─(1) account_owner_bindings（微信形态 A：最早 active binding 的 platform_user）
  └─(2) 世界归属（朝夕相伴：universe_residents.runtime_account_id → universes.owner_platform_user_id）
        └─(3) 都无 → 回退 account_id 本身（孤儿号按号强制，行为不变）
```

**为什么 B 不破 #42**：居民账号**从不创建 active owner_binding** → #42 的唯一索引 `ux_owner_binding_active_user_app(platform_user_id, app_id) WHERE status='active'` 永不被触发，「一 App 一号」始终成立。#42 与 M1 在用户面本就一致，B 只是把「居民如何挂到真人钱包」这条内部接线补上，二者无实质冲突。

（否决 A = 给居民也发 binding、再让索引区分主/居民：需动 #42 的索引与建号语义，改动面大且把"binding=微信独有"这条清晰语义再次搅浑。）

---

## 3. 改动面（PR #44 并主干前）

**① 解析收口（核心，最小改动）**
- 新增 canonical 解析 `resolve_owner_platform_user_id(cursor, account_id)`：owner_binding →（无则）universe 归属 →（无则）None。
- `_resolve_quota_subject`（`accounts.py:1327`）与 `get_platform_user_id_for_account`（`billing.py:1651`）路由到它；配额侧末段仍回退 account_id（孤儿语义不变）。
- grep 扫 `billing.py` 钱包 ensure/charge 内联的 owner_binding「account→真人」读点（1177/1398/1968 等），确认全部经此收口，无遗漏冷路径（本次 codex 5 findings 的同类根因，避免再漏扫）。

**② 居民内部建号路径（= M1-5，PR #44 需要它来支撑测试与后续 M2-C）**
- 新增原语：建一行 `account`（隔离运行容器）+ `universe_residents` 映射，**不发 owner_binding、不赠新客贝壳、不占用户账号容量**（容量真相仍 = `count_active_residents`，D-07）。
- 与用户注册入口 `create_ai4all_account_for_user`（#42 收敛管的那条）彻底分开：前者建 agent，后者建用户账号。

**③ 测试夹具**
- M1 那 13 个「一人多号共享钱包/配额」用例，第 2…N 个 account 改走 ② 的内部路径，不再用 `create_ai4all_account_for_user` 伪造 → 既不撞 #42、又忠实覆盖「居民共享真人钱包」。

**④ 命名消歧文档（本次对齐用户明确要求）**
- 代码：`account_owner_bindings` 表定义处 + 两个解析函数 docstring 注明「owner_binding = 微信接入独有；朝夕相伴居民经世界归属解析，不发 binding」。
- 文档：ADR D-02/D-14 落地说明 + 本文 §1 的一词两义澄清。

**⑤ 机械合并（已探明，非设计项）**
- 迁移版本号：#42 占 22/23/24，PR #44 的 M1 迁移改号 **25–29**（以 HEAD 为 `_core.py` 基底改号 + 重建 registry，勿 `git checkout origin/main -- _core.py` 整体覆盖）。
- `CHANNEL_APP 'app' → 'native'`（#42 已改，随合并对齐）。

---

## 4. 验证与门禁

- 双档全绿：`make test`（SQLite）+ `make test-pg`（PG）。
- PG 并发（§9/D-12）：跨居民并发扣款共享一钱包不丢更新 / `idempotency_key` 恰扣一次（M1 已有 `test_billing_concurrency_pg.py`，补一例「居民账号（经世界解析）与微信号（经 binding 解析）同真人并发共享钱包」）。
- 回归零破：无 universe 归属的存量微信号解析路径不变（走 fallback ①）。

## 5. 回滚

纯加性 fallback + 新增内部原语 + 测试夹具改写，不改 #42、不动用户注册收敛。回滚 = 撤解析 fallback 与内部原语。与线上无数据耦合（朝夕相伴居民尚未上线）。
