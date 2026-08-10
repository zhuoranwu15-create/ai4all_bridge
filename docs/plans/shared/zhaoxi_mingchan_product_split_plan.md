# 朝夕相伴 / 鸣蝉产品域拆分计划

更新时间：2026-08-04

状态：**仓库开发与开发机验证完成，首次发布待执行。MC-00～MC-06 的代码、脚本和文档工作已收口；
MC-07 当时的 SQLite/PG 双档结果是历史验收记录。当前主测试已收敛为 PostgreSQL 单档，聚焦门禁与
隔离临时库 cleanup 演练已完成。仍待外部 Native App 切换、
生产备份/清理/启用、scheduler 上线及微信/App 真机验收；这些完成前本文不得归档。**

Owner：`shared`（同时影响 `product:zhaoxi`、`product:mingchan`、Agent Runtime、Platform 与 composition root）

关联基线：

- [核心模型与术语](../../architecture/core-model.md)
- [多产品模块化单体 ADR](../../architecture/shared/data/multi_product_modular_monolith_design.md)
- [新增产品开发清单](../../guides/adding-product.md)
- [朝夕产品 manifest](../../products/zhaoxi/README.md)
- [Companion World 3.0 ADR](../../architecture/products/mingchan/companion_world_3_0_refactor_design.md)

## 1. 背景与本次决议

历史架构把两种体验放在同一个 `zhaoxi` 产品域中：

1. Web/H5 注册、扫码绑定后，通过微信与单个 AI 进行 1:1 陪伴；
2. Native App 中进入 Companion World，与一个私人世界里的多位居民互动。

当时将它们建模为“朝夕相伴的两种业务形态”，共用 `app_id=zhaoxi`、产品 session、账号归属、
`ProductTurnServices`、工具策略、产品配置和 lifecycle。随着两端产品体验继续分化，即使只是
onboarding、Prompt、工具、主动触达、记忆或生命周期上的细微差别，也会迫使同一个产品实现不断
增加渠道条件分支，最终让两个业务互相牵制。

2026-08-04 决定把它们提升为两个独立产品：

| 产品 | `app_id` | 产品边界 | 主要入口 |
| --- | --- | --- | --- |
| 朝夕相伴 | `zhaoxi` | 微信个人 AI 陪伴业务 | Web/H5 注册与扫码、OpenClaw/微信 |
| 鸣蝉 | `mingchan` | 原 Native App / Companion World 业务 | iOS/Android Native App |

本次不是品牌文案改名，也不是把 `native` 从渠道表中单独摘出来；它是产品身份、代码 owner、数据作用域、
运行任务和发布边界的完整拆分。

## 2. 已确认前提

### 2.1 鸣蝉按全新产品建设

- 当前 App/Companion World **没有真实用户**。
- 不承接现有 App 测试用户、World、居民、消息、记忆、通知、媒体、钱包、配额、session 或 referral。
- 不做 `zhaoxi → mingchan` 用户数据迁移，不建立账号映射，不复制历史资产。
- 鸣蝉上线后从新的 `mingchan` membership、session、runtime account 和产品资产开始。

因此，原方案中“克隆居民 runtime account、搬迁历史消息和处理历史账务”的工作全部取消。

### 2.2 遗留注册信息通常不阻塞拆分

`platform_user` 是跨产品真人身份。旧 App 测试曾创建过 `platform_user` 或 `zhaoxi` membership，并不妨碍
同一真人以后创建新的 `mingchan` membership。只要鸣蝉的 session、账号、钱包和产品查询都固定带
`app_id=mingchan`，旧的 `zhaoxi` 行不会被鸣蝉读取。

但以下遗留数据可能实际阻塞“全新鸣蝉”语义，必须在启用鸣蝉前清理或证明为空：

- `universes.owner_platform_user_id` 当前全局唯一，旧 World 行会阻止同一真人重新 bootstrap；
- 旧 resident/runtime account、conversation、Feed、通知、信箱、来访、许愿和媒体行可能被 World 查询读到；
- 旧 App session audience 是 `zhaoxi`，不能继续用于鸣蝉；
- `native` channel binding、`binding_method=app_otp` 等测试账号可能仍被朝夕入口账号解析器命中。

结论：**不做业务数据迁移，但需要一次受控的遗留数据清理。** 清理是拆分验收后的独立操作，不能混入
自动 schema migration，也不能按“曾经从 App 注册”就批量删除全局真人或可能真实使用过微信的账号。

## 3. 目标与非目标

### 3.1 目标

- 生产产品注册表同时、显式注册 `zhaoxi` 与 `mingchan`。
- 两产品有独立 manifest、固定 API namespace、session audience、`ProductTurnServices`、`ToolPolicy`、
  产品资源、配置和 lifecycle。
- 朝夕代码只理解微信单 Agent 业务；鸣蝉代码只理解 Native App 与 Companion World。
- 同一手机号可同时加入两个产品，但两产品的 account、session、消息、记忆、钱包、配额和 referral 隔离。
- 继续共用形态无关 Agent Runtime 与 Platform 基础能力，不复制 Runtime，不拆微服务。
- 朝夕现有微信链路行为零回归；鸣蝉以空产品数据开始联调和上线。

### 3.2 非目标

- 不迁移或兼容旧 App 测试用户的数据。
- 不把两个产品拆成两个仓库、两个数据库或两个微服务。
- 不为了减少短期代码移动而允许 `app.products.zhaoxi` 与 `app.products.mingchan` 互相 import。
- 不把 Companion World、居民、使命或主动消息等产品语义下沉到共享 Runtime。
- 不借本次拆分重做无关业务逻辑、Prompt 效果或 UI 功能。

## 4. 目标架构

```text
platform_user（真人；平台全局）
├── product_membership: zhaoxi
│   ├── Web/H5 注册与微信扫码
│   ├── 微信入口 account（1:1 Agent）
│   ├── zhaoxi session / wallet / quota / referral
│   └── 朝夕 onboarding、提醒、承诺与主动陪伴
│
└── product_membership: mingchan
    ├── Native App 登录
    ├── Companion World / residents
    ├── 每位居民独立 runtime account
    ├── mingchan session / wallet / quota / referral
    └── Feed、通知、信箱、生命周期、来访、真人聊天与许愿

两个产品
    → 分别实现 ProductTurnServices / ToolPolicy / Product Lifecycle
        → 共用形态无关 Agent Runtime
            → 共用 Platform 身份、鉴权、媒体、审核、计费框架与观测
```

固定依赖方向：

```text
bootstrap → zhaoxi / mingchan
zhaoxi   → agent_runtime + platform
mingchan → agent_runtime + platform
agent_runtime / platform -X→ 任一具体产品
zhaoxi -X→ mingchan
mingchan -X→ zhaoxi
```

## 5. 代码与能力归属

### 5.1 朝夕相伴 `app/products/zhaoxi/`

保留：

- Web/H5 注册、OTP、扫码、binding intent 与 OpenClaw 登录编排；
- `/openclaw/*` bridge、微信入站身份解析和节点接入；
- 微信 1:1 Agent 的 profile、Soul、Identity、首次聊天 onboarding；
- 微信提醒、承诺、主动陪伴、内容邀请与主动偏好；
- 朝夕专属使命、关系成长、活动码和用户自建单 Agent 角色模板；
- 朝夕运营、调试与微信送达 scheduler。

需要继续收口的旧共享位置：

- `app/routers/web.py` 最终应由朝夕 manifest 组合，避免看起来像跨产品 Web API；
- `/v1/*`、`/web/*` 继续作为朝夕固定 legacy audience；
- `app/products/zhaoxi/lifecycle.py` 移除 Companion World memory、通知清理和 World 任务接线。

### 5.2 鸣蝉 `app/products/mingchan/`

从原 `zhaoxi` 产品包迁入：

- `api/app.py`、App contracts、App profile/me、App account deletion；
- `api/companion_world*.py`、`api/app_notifications.py`；
- Companion World admin 和 App 专属 media API；
- `domain/companion_world/`；
- `application/companion_world_*`、World memory sink 与 Native turn adapter；
- Companion World persistence、repository、App inbox、notifications、resident wishes；
- World content、lifecycle、mailbox、visits、wish、App media moderation jobs；
- Native App 文案、默认产品资源、人物模板与 OpenAPI snapshot。

鸣蝉必须新增自己的：

- `manifest.py`、`lifecycle.py`；
- `MINGCHAN_APP_ID = "mingchan"`；
- `MingchanTurnServices`；
- `MINGCHAN_TOOL_POLICY`；
- `/api/v1/products/mingchan/*` 固定 namespace；
- 产品级配置与 scheduler 入口。

鸣蝉实现不得继承或 import `ZhaoxiTurnServices`。若两边当前存在相同机制，应先判断它是否真的形态无关：
只有机制可下沉 Runtime/Platform，产品文案、状态机、字段和策略分别保留在两个产品内。

### 5.3 继续共享的能力

| 层 | 共享内容 |
| --- | --- |
| `app/bootstrap/` | 产品注册、部署角色、manifest/lifecycle 组合 |
| `app/platform/` | 真人身份、产品 membership/session、钱包/配额框架、审核、媒体存储、网关、观测 |
| `app/agent_runtime/` | turn、LLM、Prompt 组装机制、工具执行、session/messages、通用记忆端口 |
| `app/tools/` | 工具 schema/handler 框架；不组合具体产品工具 |
| `app/db/` | 跨产品基础表与 repository facade；产品表的调用 owner 仍在对应产品 infrastructure |

### 5.4 易混淆能力的裁决

| 能力 | 归属 | 原因 |
| --- | --- | --- |
| `character_templates` / World resident persona | 鸣蝉 | Companion World 居民模板 |
| 用户自建角色模板 + 注册邀请链接 | 朝夕 | 当前用于微信单 Agent 拉新与实例化 |
| 微信 reminder/commitment/proactive | 朝夕 | 依赖微信送达、主动触达与 1:1 账号语义 |
| App notifications / human proactive inbox | 鸣蝉 | App 内收件箱与 World 用户体验 |
| Prompt builder、tool executor | Runtime | 形态无关机制 |
| 产品 Prompt、默认人设、工具白名单 | 各产品 | 会持续分化的产品策略 |

## 6. 身份、账号与数据不变量

1. `platform_user` 继续按手机号跨产品共享，不复制真人。
2. `product_membership`、session、wallet、subscription、quota、referral 按
   `(platform_user_id, app_id)` 隔离。
3. runtime account 的 `app_id` 不可变；朝夕账号不能被鸣蝉 turn 使用，反之亦然。
4. 所有消息、记忆、profile 和文件继续以 `account_id` 隔离。
5. World/resident 只允许引用 `app_id=mingchan` 的 runtime account。
6. 微信 channel binding 只允许路由到 `app_id=zhaoxi` 的入口 account。
7. 固定 router 和服务端注册表决定产品；客户端 Header 不能动态指定 `app_id`。
8. 两个产品的注销只清理本产品 membership 与资产，不删除另一个产品的数据或全局真人，除非真人已无任何产品归属且满足平台删除策略。

## 7. API 与客户端兼容策略

### 7.1 规范入口

```text
朝夕相伴：/api/v1/products/zhaoxi/*
鸣蝉：    /api/v1/products/mingchan/*
```

反代剥离 `/api` 时分别兼容：

```text
/v1/products/zhaoxi/*
/v1/products/mingchan/*
```

### 7.2 Legacy 路由

- `/openclaw/*`、`/web/*` 固定属于朝夕。
- 鸣蝉按全新业务发布，不承接当前 App 的 `/v1/app/*`、`/v1/worlds/*`、
  `/v1/ai-conversations/*` 等 legacy 路由。
- 不保留 `/api/v1/products/zhaoxi/*` 下的 App/World 语义；测试客户端应尽快切到鸣蝉规范入口。
- 不兼容旧 `zhaoxi` App token、session、World ID 或其他客户端缓存；开发/测试客户端清除本地状态后
  直接注册全新的鸣蝉账号。

因此 MC-06 只做新 namespace 和新 audience 切换，不建设兼容层，也不保留双写/转发逻辑。

## 8. 配置、生命周期与任务拆分

### 8.1 配置

- 朝夕配置使用 `ZHAOXI_*` 或已有明确微信语义的配置名。
- Companion World/App 配置逐步改为 `MINGCHAN_*`，同步更新 `.env.example` 行内注释。
- 若生产部署仍依赖旧 `COMPANION_WORLD_*` 名，可提供一个发布周期的只读别名；不得长期双写两套真值。
- 产品开关必须由产品 lifecycle 消费，不由共享 startup 猜具体产品。

### 8.2 Lifecycle

朝夕 lifecycle：

- 微信 proactive；
- 朝夕 Dreaming/user-meta；
- 微信产品注册表与主动消息分类校验。

鸣蝉 lifecycle：

- Companion World memory sink/compactor；
- App notification cleanup；
- World content/lifecycle/mailbox/visit/wish jobs；
- App media signing 与回收校验。

当前全局 `configure_memory_sink(...)` 需要改为产品/turn 显式注入，避免鸣蝉 World sink 影响朝夕 turn。

### 8.3 独立进程入口

建议形成明确入口：

```text
scripts/run_proactive_scheduler.py                 # 朝夕微信主动消息
scripts/run_mingchan_world_content_scheduler.py    # 鸣蝉 Feed/outbox
scripts/run_mingchan_world_lifecycle_scheduler.py  # 鸣蝉 lifecycle/mailbox/visits
scripts/run_mingchan_wish_worker.py                # 鸣蝉异步许愿（如仍需要独立运行）
```

生产仍由 central 执行 migration 和 central-only scheduler；node 只处理归属微信账号的朝夕 turn。

### 8.4 开发机与线上机分工

当前工作区是开发机。开发阶段只允许执行可重复、可验证的仓库内操作：代码/文档拆分、隔离
PostgreSQL 测试、生成 precheck/cleanup 工具以及对本地测试数据做演练。不得从开发机
直接清理生产数据、启用生产鸣蝉、改线上反代或启动线上 scheduler。

代码合并并完成 PostgreSQL 回归后，以下动作只能在生产 central 节点按 runbook 顺序人工执行：

1. 确认当前部署版本、PG 主库连接和 central/node 角色，暂停 App/World 写入口及相关 worker；
2. 对生产 PostgreSQL 做可恢复备份，记录备份标识、表计数与朝夕关键业务基线；
3. 运行只读 precheck/cleanup `--plan`，确认无真实 App 用户、无待删账号绑定真实微信；
4. 执行 schema migration 和新版本部署，但保持 `mingchan` 注册项及 scheduler 禁用；
5. 人工复核 dry-run 输出后执行一次受控 cleanup `--apply`，再跑 reconcile；
6. 用全新测试手机号验证鸣蝉注册、World bootstrap 和居民 turn，同时回归朝夕微信链路；
7. 客户端已使用 `/api/v1/products/mingchan/*` 后才启用鸣蝉和对应 worker；
8. 观察日志、告警和关键计数，异常时关闭鸣蝉入口并按 PG 备份恢复，不回落旧 SQLite。

鸣蝉没有历史业务兼容要求。线上旧 App/World 行只作为待清理测试数据，不做转换、回填、映射或
双写；但清理仍必须保护可能共用的 `platform_user` 和真实朝夕微信数据。

## 9. 遗留数据清理方案

### 9.1 原则

- 清理前先做只读审计和备份；默认 `--plan`，显式 `--apply` 才写入。
- 清理脚本独立于 `_MIGRATIONS`，不得在服务启动时静默删除数据。
- 先清产品子表，再处理 account；最后才评估孤立 membership/platform user。
- `platform_users` 是跨产品全局身份，默认保留。
- 任一账号存在真实微信 binding、微信消息或无法证明只属于旧 App 测试数据时，不删除。

### 9.2 可整体清理的鸣蝉旧测试域

在再次确认“无真实 App 用户”并完成备份后，可按外键逆序清空旧 Companion World/App 测试数据：

- World visits、human conversations、mailbox、resident wishes、lifecycle events；
- App notifications、World outbox、Feed/posts；
- universe memory facts、AI conversations、residents；`resident_drafts` 没有 `app_id`/World 锚，默认保留并
  交给既有 TTL 回收，不能按真人 ID 猜测产品后删除；
- character templates 中仅供旧 App 测试的模板；
- universes；
- 媒体资产默认保留，仅解除旧 World 关联；后续由媒体 reconcile/回收任务单独处理。

实际表清单必须由脚本从当前 schema 和外键重新生成/核对，本文不作为可直接执行的 `DELETE` 顺序。

### 9.3 需要逐账号判定的遗留

- `channel='native'` 的 channel binding；
- `binding_method='app_otp'` 的 owner binding；
- 由旧 App fallback 创建的 `app_id=zhaoxi` 入口 account；
- 对应 profile、session/messages、memory、mission、cost 与 wallet 引用；
- 只有旧 App 测试 membership、但未来可能继续使用同一手机号的 `platform_user`。

建议新增：

```text
scripts/precheck_mingchan_clean_start.py
scripts/cleanup_legacy_app_test_data.py
```

清理前置断言：

1. 无真实 App 用户名单与业务确认记录；
2. 待删 account 无 active 微信 channel binding；
3. 待删 account 不属于需要保留的朝夕用户；
4. 删除后账务、配额、referral 与 account 外键 reconcile 为 0；
5. 清理脚本 SQLite/PG 均可 dry-run，生产只在 PG 执行一次受控 apply。

### 9.4 清理完成标准

- Companion World/App 业务表为空，或仅保留明确登记的开发种子数据；
- 无 `zhaoxi` resident runtime account 引用；
- 无旧 App `zhaoxi` session 被鸣蝉路由接受；
- 首个鸣蝉测试用户能用既有或新建 `platform_user` 创建全新的 `mingchan` membership 和 World；
- 朝夕现有账号、消息、钱包、绑定和主动任务数量不因清理发生非预期变化。

## 10. 工作包与执行顺序

### MC-00：决策冻结与文档改版

状态：`development_complete`

目标：把“同产品双形态”改为“两个独立产品”的事实冻结下来，避免实施期间继续按旧边界新增代码。

主要工作：

- [x] 建立本文，记录背景、边界、工作包与验收条件。
- [x] 确认 `mingchan` 为稳定 `app_id`。
- [x] 确认鸣蝉不保留旧朝夕 App 公共契约；`/v1/products/mingchan/*` 仅作为反代剥离
  `/api` 后的内部等价挂载，客户端直接使用规范 namespace 并清除旧测试状态。
- [x] 新建 `docs/products/mingchan/README.md` 与总 PRD。
- [x] 将 Companion World App PRD、客户端 brief/handoff、OpenAPI 契约入口迁入鸣蝉文档目录并改 owner。
- [x] 更新 `architecture/core-model.md`、`architecture/overview.md`、`system_design.md` 的当前边界。
- [x] 把旧“朝夕双形态”结论标记为被本决策取代，避免历史计划继续指导开发。

验收：所有非归档当前文档一致说明 `zhaoxi=微信业务`、`mingchan=Native App/World 业务`。

### MC-01：产品注册与 composition root

状态：`development_complete_release_pending`

主要文件：

- `app/bootstrap/product_registry.py`
- `app/bootstrap/application.py`
- `app/bootstrap/http.py`
- `app/products/zhaoxi/manifest.py`
- `app/products/mingchan/manifest.py`（新增）
- `app/products/mingchan/lifecycle.py`（新增）

工作项：

- [x] 以 `enabled=False` 预注册 `mingchan` 并明确默认语言，迁移期间 fail closed。
- [ ] 完成验证、客户端切换与发布 precheck 后，在生产启用 `mingchan`（发布动作）；允许渠道校验已完成。
- [x] composition root 显式注册两个产品 lifecycle；鸣蝉 disabled 时不执行产品动作。
- [x] composition root 已显式组合两个产品 manifest；鸣蝉 manifest 组合完整身份/App/World 路由。
- [x] App/World API 由鸣蝉 manifest 组合完整业务路由。
- [x] 朝夕 manifest 只挂微信/Web/朝夕 admin；鸣蝉 manifest 只挂 App/World/鸣蝉 admin。
- [x] 固定两个产品的规范 namespace，禁止 Header 动态选择产品。
- [x] central/node 角色下只安装所需入口，节点不安装鸣蝉中心 API。

验收：两个产品均可独立启动、枚举路由和签发自己的 session；禁用任一产品不影响另一产品启动。

### MC-02：鸣蝉代码域实体化

状态：`development_complete`

工作项：

- [x] 建立 `app/products/mingchan/{api,application,domain,infrastructure,jobs,tools}`。
- [x] 迁移 App/Companion World 所有权明确的 API、application、domain、infrastructure、jobs 与测试。
- [x] App inbox、notification repository 与鸣蝉通知 API 使用 `app_id=mingchan`，朝夕 application
  导出面不再暴露 App inbox 类型。
- [x] 原 App notification 集成测试已切换鸣蝉固定 namespace、session audience 和配置名，保留分页、
  已读、owner 隔离、幂等投递、过期与 cleanup 覆盖。
- [x] 新建 `MingchanTurnServices` 与 `MINGCHAN_TOOL_POLICY`，居民 turn 不再依赖朝夕 turn services；
  鸣蝉产品文案资源继续随 App 契约迁移收口。
- [x] Native App session/account 创建全部固定传 `app_id=mingchan`；旧朝夕 App API 已移除。
- [x] World resident runtime account 创建全部固定传 `app_id=mingchan`。
- [x] World repository 读写校验 owner membership、World 与 resident account 产品一致。
- [x] 更新脚本、OpenAPI、Admin 和测试 import；移除 legacy carry-in/backfill 运行时接口与旧测试。
- [x] 原 App ME 设置测试中的登录、Profile、基础注销与通知偏好已切换鸣蝉 namespace、session audience、
  account/membership 和产品级持久化。
- [x] App ME 的媒体/human-chat 与 Feed/outbox 注销契约已切换鸣蝉 namespace 和 `MINGCHAN_*`
  配置，并继续覆盖真人会话媒体保留、本人资产清理和跨用户隔离。
- [x] 建立鸣蝉 resident App inbox 产品级主动投递入口；安静偏好、产品开关和 World/resident
  `app_id=mingchan` 校验均在创建通知前执行，不再依赖朝夕 proactive 或微信 outbound gateway。
- [x] `test_app_me_settings.py` 全部 18 个 Profile、注销、媒体、Feed、通知偏好和主动投递契约已完成
  鸣蝉化；注销时间响应统一输出带 `+08:00` 的客户端时间。

验收：`app/products/mingchan/` 不 import 朝夕；最小 App 登录、World bootstrap、居民 turn 均不依赖
`ZHAOXI_APP_ID`、`ZhaoxiTurnServices` 或朝夕工具 catalog。

### MC-03：朝夕产品域收缩

状态：`development_complete`

工作项：

- [x] 从朝夕 manifest、lifecycle、API、jobs 中移除 Native App/World 组合。
- [x] 保留 Web/H5 注册、扫码、OpenClaw bridge 与微信 turn；行为验证归 MC-07。
- [x] 整理朝夕 profile/onboarding/mission/proactive/tool policy，不再接收 `native` turn。
- [x] 将朝夕 Web 路由组合归入产品 manifest，顶层 composition root 只做组合。
- [x] 修正用户展示文案，朝夕只描述微信陪伴业务。

验收：朝夕包内不再出现 Companion World、resident、App inbox 或 `CHANNEL_NATIVE` 业务分支。

### MC-04：共享 Runtime/Platform 接缝收口

状态：`development_complete`

工作项：

- [x] 盘点两个产品都使用的 session/profile/memory 机制，只有形态无关部分下沉。
- [x] 去掉全局单例式 World memory sink，改为鸣蝉 turn/product 显式注入。
- [x] 为产品注册增加允许渠道校验，阻断 `zhaoxi+native` 与 `mingchan+weixin` 错配。
- [x] quota、wallet、cost、referral、account deletion 全链路使用可信 `app_id`。
- [x] 泛化层级门禁，禁止 Runtime/Platform import 任一产品、产品互相 import。

验收：新增第三产品不需要修改 Runtime 的具体产品字符串分支；两个产品的 turn 错配均在副作用前失败。

### MC-05：配置、schema 与遗留测试数据清理

状态：`development_complete_release_pending`

工作项：

- [x] 增加鸣蝉所需 schema 约束和索引，SQLite/PG 同步实现。
- [x] World root/模板等产品锚明确为鸣蝉，运行时清除隐式 `zhaoxi` 默认值。
- [x] 拆分配置与 scheduler 入口，更新 `.env.example`、systemd/deploy/runbook。
- [x] 实现只读 precheck 与受控 cleanup 脚本。
- [x] 在开发机隔离临时 SQLite/PG 环境演练 plan、apply、重复执行、保护门禁和 reconcile。
- [ ] 生产备份、dry-run、人工确认后清理旧 App/World 测试数据。

验收：鸣蝉业务表以空数据开始；朝夕生产数据对账无变化；cleanup 后所有隔离 reconcile 为 0。

### MC-06：客户端与 API 切换

状态：`backend_development_complete_external_switch_pending`

工作项：

- [x] 本仓 Native App base path、登录、token audience、错误码和 OpenAPI 切到鸣蝉。
- [x] App ME 的登录、Profile、基础注销与通知偏好回归契约切到鸣蝉固定 namespace；媒体、Feed、
  human chat 注销与主动投递相关契约均已完成切换。
- [x] App notification API 与回归契约切到 `/api/v1/products/mingchan/notifications`，并覆盖反代剥离后的
  `/v1/products/mingchan/notifications`。
- [x] 本仓 App 品牌、默认产品文案和文档入口改为鸣蝉；外部客户端资产仍待切换。
- [x] 决定不实现旧朝夕 `/v1/*` App 公共契约；保留的 `/v1/products/mingchan/*` 仅承接反代剥离
  `/api` 后的同一鸣蝉 audience。
- [ ] 客户端清除旧 `zhaoxi` App token/缓存后，以全新鸣蝉业务重新注册登录。
- [x] 本仓 Admin/运营入口按产品分组，产品任务与日志携带 `app_id`。

验收：当前测试客户端可完整登录和使用鸣蝉；任何朝夕 token 调用鸣蝉 API 返回 401，反向亦然。

### MC-07：总回归、发布与文档归档

状态：`development_validation_complete_release_pending`

工作项：

- [x] 运行两个产品聚焦测试、层级门禁和文档链接检查。
- [x] 运行 SQLite 全量回归。
- [x] 运行 PostgreSQL 全量回归。
- [ ] 微信真机验证注册、扫码、被动回复、提醒/主动消息。
- [ ] Native App 验证登录、World bootstrap、居民聊天、Feed、通知、信箱、媒体与注销。
- [x] 更新 `STATUS.md`、产品 manifest、架构 ADR、Runbook 和模块地图。
- [ ] 稳定结论全部回写后，将本文归档到 `docs/archive/deliveries/`。

验收：两个业务能独立演进、独立关闭、独立排障；不存在跨产品账号、session、记忆、账务或任务串用。

## 11. 测试矩阵

### 11.1 产品注册与 API

- 注册表启用 `zhaoxi`、`mingchan`，拒绝未知、disabled 和重复产品。
- 固定 namespace 可访问，Header 不能改变 audience。
- `zhaoxi` token 不能访问鸣蝉，`mingchan` token 不能访问朝夕。
- Legacy App 路由若保留，必须解析为鸣蝉而不是朝夕。

### 11.2 账号和文件隔离

- 同一 `platform_user` 可同时拥有两个 membership。
- 两产品 account、profile、session/messages、Soul、Identity、memory 文件互不返回。
- account/product 错配在建 session、写消息、扣 quota、写 profile 之前失败。
- World resident 不能引用朝夕 account，微信 binding 不能引用鸣蝉 account。

### 11.3 计费、配额与删除

- 两产品 wallet、subscription、新客赠权、daily/RPM quota、referral 独立。
- 一个产品扣费、清理或注销不影响另一产品。
- 鸣蝉新用户不因历史存在 `zhaoxi` membership 而丢失鸣蝉新客资格。

### 11.4 Lifecycle 与任务

- 朝夕 scheduler 只扫描朝夕账号，不扫描 World resident。
- 鸣蝉 job 只产生 App inbox/World 资产，不调用微信 Gateway。
- 两产品 Dreaming/memory sink 不串用。
- central/node 多实例下无重复调度或跨产品 claim。

### 11.5 清理与双后端

- cleanup `--plan` 不写数据，输出不含手机号、消息正文或敏感内容。
- cleanup `--apply` 可幂等重跑；中断后可继续，不能误删微信账号。
- SQLite 与 PostgreSQL 使用同一关键清理、schema 和隔离用例。
- `tests/test_layer_boundaries.py`、`tests/test_documentation_links.py` 通过。
- 因本次触及共享基础设施、路由、schema、计费、Prompt/tool 和跨模块契约，合并前运行 SQLite/PG 全量回归。

## 12. 风险与控制

| 风险 | 影响 | 控制 |
| --- | --- | --- |
| 只搬目录、不改 `app_id` | 鸣蝉继续读写朝夕资产 | session/account/turn 三层 audience 校验 |
| 产品间直接 import | 后续继续耦合，拆分名存实亡 | AST 门禁 + composition root 唯一组合点 |
| 把产品共性过度下沉 Runtime | Runtime 出现 World/微信语义 | 只下沉机制，不下沉状态机/文案/策略 |
| 清理旧 App 数据时误删微信用户 | 真实数据损失 | 独立 precheck、备份、dry-run、逐账号断言 |
| World 旧行未清 | 新鸣蝉用户 bootstrap 冲突或读到测试数据 | 鸣蝉启用前清空并 reconcile |
| 全局 memory sink/scheduler 未拆 | 两产品记忆或任务串用 | 产品显式注入、独立 lifecycle/worker |
| Legacy `/v1/*` 路由归属不清 | token audience 错误 | 固定由鸣蝉 manifest 挂载并写契约测试 |
| 大规模移动造成测试 import 噪声 | 回归难定位 | 按工作包移动，每批聚焦测试，禁止顺手重构 |

## 13. 待确认事项

以下事项在对应工作包开工前确认，不阻塞本文建立：

1. 鸣蝉正式支持邮箱和默认系统文案；默认语言暂定 `zh-CN`，展示名已确定为“鸣蝉”。
2. 鸣蝉首发是否启用钱包/贝壳、referral 和主动 App inbox；未启用能力不提前实现产品策略。
3. 生产旧 App/World 数据的最终清理确认人和清理时间窗口。
4. Companion World 相关历史技术文档是整体移动到 `products/mingchan`，还是保留归档副本并建立新权威入口。

## 14. 决策记录

| 日期 | 决策 | 结果 |
| --- | --- | --- |
| 2026-08-04 | 微信与 Native App 是否继续作为同一 `zhaoxi` 产品的两种形态 | 否；拆成朝夕相伴与鸣蝉两个独立产品 |
| 2026-08-04 | 朝夕相伴产品名和 `app_id` | 保持「朝夕相伴」与 `zhaoxi` |
| 2026-08-04 | 原 App 产品名和稳定产品标识 | 改为「鸣蝉」，固定 `app_id=mingchan` |
| 2026-08-04 | 是否迁移旧 App 用户和业务数据 | 否；当前无真实用户，鸣蝉按全新产品建设 |
| 2026-08-04 | 旧 App/World 测试数据处理 | 拆分验证后受控清理，不进入自动 migration |
| 2026-08-04 | 是否拆微服务或复制 Runtime | 否；继续模块化单体，共用形态无关 Runtime/Platform |
| 2026-08-04 | 迁移期间是否立即启用鸣蝉 | 否；先以 `enabled=False` 预注册，完成 App/World 作用域切换后再启用 |
| 2026-08-04 | 鸣蝉是否承接旧 App 路由、token 或客户端状态 | 否；作为全新业务，只提供新 namespace，测试客户端清除旧状态后重新注册 |
| 2026-08-04 | 开发机是否执行生产切换/清理 | 否；开发机只实现和演练，生产 central 按备份→dry-run→部署→清理→验证→启用顺序执行 |

## 15. 执行记录

### 2026-08-04：第一批（MC-00 + MC-01 起步）

- 建立鸣蝉产品/架构文档入口，校正产品目录、核心模型、总体架构、技术总平面和项目状态。
- 在 `app/products/mingchan/` 建立无运行时副作用的 manifest 骨架和固定 namespace。
- 在可信生产注册表预注册 `mingchan`，保持禁用；朝夕仍是唯一启用产品。
- 增加注册表、manifest 与脚手架边界测试。
- 本批不安装鸣蝉路由/lifecycle，不移动 App/World 业务代码，不修改 schema，不清理任何数据。

### 2026-08-04：第二批 A（MC-02 分层骨架 + world-content）

- 冻结鸣蝉无 legacy 兼容策略：不迁移旧 App token/session/数据，不保留旧 `/v1/*` App 路由。
- 新增鸣蝉生产首次启用检查单，明确开发机只实施/演练，生产 central 才执行备份、dry-run、清理、
  部署、验证与启用。
- 建立鸣蝉 `api/application/domain/infrastructure/jobs/tools` 分层包。
- 使用 `git mv` 将自包含的 `jobs/world_content` 调度域迁入鸣蝉，并更新测试 import。
- 调度入口改为 `scripts/run_mingchan_world_content_scheduler.py`；systemd 单元改为
  `ai4all-mingchan-world-content-scheduler.service`。配置变量暂保留 `COMPANION_WORLD_*`，待 MC-05
  统一切为 `MINGCHAN_*`。
- 新调度入口增加产品注册表闸门；`mingchan` 仍为 disabled 时即使旧 Feed 开关为 true 也干净退出，
  防止线上误启动后扫描 legacy World 数据。
- 其他 World API/application 仍依赖朝夕本地化、使命与 turn service，后续按依赖簇迁移；本批不制造
  产品间 import。

### 2026-08-04：第二批 B（产品 lifecycle 解耦）

- 朝夕 lifecycle 移除 Companion World memory sink/compactor、App notification cleanup、App media
  signing 校验与孤儿媒体回收接线；朝夕只保留微信 proactive、Dreaming 和 user-meta。
- 独立 `scripts/run_proactive_scheduler.py` 同步移除 World memory、App 媒体回收和机审接线，避免朝夕
  worker 继续操作鸣蝉资产。
- 新建 `app/products/mingchan/lifecycle.py`，并由 composition root 显式注册；`mingchan` disabled
  时跳过全部鸣蝉启动校验和任务。
- 鸣蝉启用后先承接媒体签名校验；World memory、通知清理、媒体回收/机审将在对应 application/job
  迁入时逐项接线，启用前不得存在能力空窗。

### 2026-08-04：第二批 C（Companion World persistence 迁移）

- 使用 `git mv` 将 World、lifecycle、mailbox、visits、human chat、App notifications 与 resident wishes
  的 SQLite/PG persistence 原语迁入 `app/products/mingchan/infrastructure/persistence/`。
- `app.db` 过渡 façade 改为从鸣蝉 owner 懒加载这些公共原语；尚未迁移的旧 World
  application/repository 只经该 façade 调用，不产生 `zhaoxi → mingchan` 直接 import。
- 鸣蝉 persistence 内部依赖改为包内相对 import；边界测试与 persistence 聚焦测试改认新的代码 owner。
- 本批只移动代码 owner，不修改 schema、SQL 或现有数据；`companion_world.py` 中旧 App 账号范围仍暂用
  `ZHAOXI_APP_ID`，必须在 Native session/account factory 迁入鸣蝉时原子切为 `MINGCHAN_APP_ID`。
- 开发机不执行 cleanup 或生产数据操作；鸣蝉继续 disabled，旧 API 路由本批不变。
- 验证：SQLite Companion World/通知/居民账号/多产品相关回归 `304 passed, 28 skipped`；临时
  PostgreSQL persistence 聚焦回归 `108 passed, 1 skipped`；分层与文档链接 `19 passed`。

### 2026-08-04：第二批 D（鸣蝉产品身份基座）

- `ProductRegistry` 增加“已注册”读取；FastAPI 产品 session dependency 在模块声明时只校验注册，
  请求时重新校验启用状态。鸣蝉模块可在生产 disabled 状态安全加载，但任何请求仍 fail closed。
- 测试注册表显式启用 `mingchan`；新增双向 token audience 隔离、全新手机号只创建鸣蝉 membership、
  鸣蝉居民账号归属和 disabled 生产依赖测试。
- 新增 `app/products/mingchan/application/identity.py`，固定完成 OTP→`mingchan` membership→鸣蝉
  session，不创建朝夕 membership、微信 binding 或旧单 Agent 账号。
- 新增 `app/products/mingchan/infrastructure/accounts.py`，所有鸣蝉 resident runtime account 创建
  固定传 `MINGCHAN_APP_ID`。
- 共享居民 account factory 去掉隐式 `zhaoxi` 默认值；现有朝夕 repository/mailbox 和测试工厂均
  显式传 `ZHAOXI_APP_ID`，为后续迁移时逐调用点切换建立编译/测试门禁。
- 本批不挂载鸣蝉 API router、不启用生产产品、不修改 schema 或数据。
- 验证：SQLite 身份/账号/World 聚焦回归 `70 passed, 9 skipped`，Companion World + 多产品扩展
  回归 `343 passed, 33 skipped`；临时 PostgreSQL 身份与账号回归 `24 passed`。

### 2026-08-04：第二批 E（鸣蝉最小身份 HTTP 闭环）

- 新增鸣蝉独立身份 API 契约与 router，提供已验证 OTP token 换取 session、读取 `/me` 和注销当前
  session；登录只创建/恢复 `mingchan` membership，不预建朝夕账号或居民账号。
- composition root 开始显式安装鸣蝉 public router；仅挂
  `/api/v1/products/mingchan/*` 与反代剥前缀后的 `/v1/products/mingchan/*`，不增加旧
  `/v1/auth/session` 兼容路由。
- router 支持测试注入已启用注册表；生产注册表中的鸣蝉仍为 disabled，任何身份请求都在消费 OTP
  或写 membership/session 前返回 `503`。朝夕 token 与鸣蝉 token 在真实 HTTP dependency 上双向隔离。
- 本批尚未迁移 OTP 发送/校验端点、World bootstrap 或其他 App API；客户端暂不能据此完成全链路切换，
  不得启用生产鸣蝉。
- 开发机未执行 schema、数据迁移、cleanup、生产配置或产品启用。
- 验证：SQLite 身份/API/朝夕 App 聚焦 `26 passed`；层级与多产品回归 `26 passed, 2 skipped`；临时
  PostgreSQL 身份/API/membership 回归 `17 passed`。

### 2026-08-04：第二批 F（共享 OTP + 鸣蝉完整身份入口）

- 将 captcha 校验、每手机号小时限流、验证码创建/发送、并发串行、错误重发保护、尝试次数与
  verified token 签发提炼为 `app/platform/auth/phone_otp.py`。该服务只证明手机号，不创建产品
  membership/session，也不依赖任一产品包或 `app.db` 兼容 façade。
- 朝夕 `/web/sms/*` 与旧 App `/auth/otp/*` 继续通过原 API adapter 使用同一服务，HTTP 状态、公开
  错误码映射、限流和单次 token 行为保持不变。
- 鸣蝉新增 `/auth/otp/send`、`/auth/otp/verify`，仅挂固定产品 namespace；已验证
  OTP→`mingchan` membership→鸣蝉 session 的完整 HTTP 闭环不再 import 或调用朝夕 Web 路由。
- 鸣蝉 disabled 时 router dependency 在 captcha、短信和 DB 写入前返回 `503`；生产注册表仍保持
  disabled，本批不启用产品。
- 本批未修改 schema、数据和生产配置，未执行迁移或 cleanup；World bootstrap 和其余 App API 仍待迁移。
- 验证：SQLite OTP/鸣蝉身份/朝夕 App 聚焦 `59 passed`；层级、manifest 与文档回归 `20 passed`；临时
  PostgreSQL OTP/鸣蝉身份回归 `48 passed`。

### 2026-08-04：第二批 G（鸣蝉 World onboarding 垂直切片）

- 新增鸣蝉独立 World onboarding 领域服务与 SQLite/PostgreSQL repository，提供 home world bootstrap、
  初始候选列表、居民确认和居民列表；不调用朝夕 legacy carry-in，也不 import 朝夕产品包。
- 鸣蝉 manifest 在两个固定 namespace 下安装
  `/worlds/home/bootstrap`、`/worlds/home/resident-candidates`、
  `/worlds/home/residents/confirm` 和 `/worlds/home/residents`；全部使用鸣蝉 session audience 和独立
  错误信封。
- 每次 owner 读写都要求 active `mingchan` membership；居民激活固定通过鸣蝉 account adapter 创建
  `app_id=mingchan` 的 runtime account，读取时再次校验产品归属，发现错配即 fail closed。
- bootstrap/confirm 在单事务内执行并锁定 World；初始模板目录必须满足连续 rank、完整展示元数据和
  persona seed，居民数量限制为 1–10。API 不暴露 persona seed 或 runtime account id。
- 本批只迁移 onboarding 最小闭环。居民欢迎消息和 `resident_intro` Feed 仍依赖旧朝夕领域文案，尚未
  迁入；后续须在鸣蝉内建立独立内容资源，不得通过 `mingchan → zhaoxi` import 复用。
- `universes.owner_platform_user_id` 仍为全局唯一且 World root/初始模板尚无 `app_id` 产品锚；生产鸣蝉
  继续 disabled。生产启用前必须由 MC-05 完成旧 App/World 测试数据 cleanup 或 schema 产品锚定，
  否则同一真人的旧 World 行可能阻塞全新 bootstrap。
- PostgreSQL 全量回归暴露 `profile_storage.list_filenames()` 依赖数据库默认 collation，导致 SQLite/PG
  对大小写文件名顺序不一致；已在共享 persistence 返回边界统一按 Unicode 码点排序，不改数据或
  schema。
- 开发机未执行 schema、数据迁移、cleanup、生产配置或产品启用。
- 验证：SQLite 鸣蝉 World/身份聚焦 `18 passed`，旧 Companion World 回归 `35 passed`；层级与文档
  链接 `19 passed`，`git diff --check` 通过；临时 PostgreSQL 鸣蝉 World/身份/居民账号回归
  `12 passed`。最终全量回归：SQLite `2038 passed, 38 skipped`，临时 PostgreSQL
  `2069 passed, 7 skipped`。

### 2026-08-04：最终开发批次（MC-01～MC-06 收口）

- 完成 App/World API、领域、application、persistence、jobs、工具策略与测试归属迁移；朝夕 manifest、
  lifecycle、profile 和 proactive scheduler 不再组合或扫描鸣蝉资产。
- 鸣蝉 World/read/主动范围固定校验 `universes.app_id=mingchan` 与 resident account 产品；移除旧微信
  resident carry-in、legacy primary 主动路由、backfill 脚本及对应旧测试。历史 schema 列只供 cleanup
  识别，不进入鸣蝉运行时模型。
- World lifecycle worker 接管鸣蝉 notification cleanup、孤儿媒体回收与媒体审核维护；朝夕 scheduler
  移除这些 App/World 维护职责。
- 增加默认只读、显式 `--apply` 的鸣蝉 clean-start precheck/cleanup 工具；保护全局真人、朝夕微信
  binding/messages、runtime account、账务、无产品锚草稿和媒体资产，生产执行仍需备份与人工确认。
- 删除已被正式鸣蝉实现替代的 `legacy_*` 领域副本；L3 compact 与所有保留产品参数的 persistence
  入口固定拒绝非 `mingchan` 作用域。
- 将 App PRD、客户端 handoff/brief、Companion World ADR 与 OpenAPI snapshot 迁入鸣蝉 owner 目录；
  OpenAPI 导出器和契约测试切到 `/api/v1/products/mingchan/*`。
- 仓库内开发至此完成。该批当时按用户要求未运行验证，验证结果已在下一节统一补齐。
- 仍未完成且不属于开发机开发：外部 Native App 仓库切换、生产备份/cleanup、`mingchan` 生产启用、
  scheduler 上线与微信/App 真机验收。完成这些事项前本文保持活动状态，不归档。

### 2026-08-04：开发机审查与验证收口

- 再次审查拆分边界并修复：鸣蝉 human notification 的 `app_id` 串域、居民 onboarding 多语言参数错误、
  turn 渠道/产品错配检查晚于 registry 与副作用、双产品 `422` handler 互相覆盖、lifecycle 单个维护步骤
  拖垮整次 tick，以及朝夕 web-search debug 使用了不允许的伪渠道。
- 增加 cleanup 双后端测试，覆盖只读 plan、只删朝夕 legacy World/模板、保留鸣蝉控制组、重复 apply
  幂等，以及 resident 绑定真实微信时整批拒绝且零写入。该段记录的是 2026-08-04 当时的过渡实现；
  当前 CLI 和主应用均只接受 PostgreSQL，不再存在 `DATABASE_PATH` 回落。
- 聚焦结果：鸣蝉/Companion World 扩展组 `317 passed, 27 skipped`；受影响回归组
  `122 passed, 2 skipped`；cleanup 在 SQLite、临时 PostgreSQL 均通过。层级边界、文档链接、OpenAPI、
  产品互相 import 门禁和静态检查通过。
- 全量结果：SQLite `2000 passed, 38 skipped`；临时 PostgreSQL `2031 passed, 7 skipped`。新增的 cleanup
  用例在全量后单独以 SQLite/PG 两档通过；未因文档和 CLI 参数保护补丁重跑整套全量。
- 开发机隔离库完成 SQLite CLI precheck/apply/二次 apply 演练，临时 PG 通过同等函数级/事务级测试；
  未连接生产、未操作标准 `data/ai4all.sqlite3`、未启用鸣蝉，也未启动线上 worker。
