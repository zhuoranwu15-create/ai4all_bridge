# 新增产品开发清单

更新时间：2026-07-25

本清单用于把一个**已冻结核心 PRD 的真实产品**接入当前模块化单体。它不是产品脚手架：
没有明确业务需求时，不创建 `app/products/<app_id>/` 空目录，不注册占位产品，也不为
Fatetell、Nooki 或其他候选产品猜测领域契约。

## 1. 开工输入

编码前至少确认以下事实：

1. 稳定的 `app_id`、产品名、入口渠道、客户端形态和固定 API namespace。
2. `product_membership` 的创建/停用规则，以及每个 membership 下入口 account、Runtime account
   的数量和所有权模型。
3. onboarding、persona、prompt context、工具、记忆和数据删除规则。
4. 钱包、订阅、配额、邀请码、新客权益与运营口径。
5. 是否需要主动消息；若需要，明确通知存储、送达渠道、退订、频控、幂等和 scheduler 真值。
6. 产品领域状态机的幂等、并发、恢复、隐私和合规要求。

以上输入不完整时，只补共享设计或产品 PRD，不创建虚假业务实现。

## 2. 文档与产品 manifest

- 在 `docs/products/<app_id>/README.md` 记录 `app_id`、状态、渠道、代码命名空间、固定 API
  与兼容入口，并建立总 PRD。
- 只为真实需求增加 `capabilities/`、`docs/architecture/products/<app_id>/`、
  `docs/plans/products/<app_id>/`；不复制朝夕文档结构。
- 在 `app/bootstrap/product_registry.py` 增加明确的 `ProductRegistration`，并显式设置
  `default_language`（当前支持 `zh-CN`、`en-US`、`ja-JP`）。生产注册表只包含实际可用的
  产品，未知、disabled 或语言配置不受支持的产品必须 fail closed。
- 在 `app/products/<app_id>/manifest.py` 集中暴露产品 routers、lifecycle 和 scheduler 组合点；
  `app/main.py` 不直接承载产品逻辑。

每个 manifest 必须声明固定常量：

- `APP_ID = "<app_id>"`
- `CANONICAL_API_PREFIX = "/api/v1/products/<app_id>"`
- 如部署反代会剥离 `/api`，兼容 `"/v1/products/<app_id>"`

不得接受 `X-App-ID` 或其他客户端输入动态决定产品；固定 router 和服务端注册表才是 audience
来源。旧 `/v1/*`、`/web/*` 是朝夕兼容面，不给新产品复制。

## 3. 身份、账号与数据隔离

- 产品 API 使用 `require_product_session(APP_ID)`，并以 `SessionPrincipal` 作为身份输入。
- session resolver 必须同时校验 audience、active membership 和有效期。
- 入口账号解析必须显式传 `(platform_user_id, app_id)`；任何未带 `app_id` 的产品级查询都应
  视为缺陷。
- 所有 Runtime 消息、memory、profile 和文件写入必须以 `account_id` 隔离；account 的
  `app_id` 与 turn 输入不一致时，在创建 session、写消息/profile、解析配额之前拒绝。
- 钱包、订阅、quota、cost、referral 与新客权益沿用 `(platform_user_id, app_id)` 隔离；
  冗余 `app_id` 写入时校验与 membership、account、wallet 一致。
- migration 必须同时支持 SQLite 与 PostgreSQL，包含存量 backfill、唯一约束、幂等键与并发
  锁序；不得删除 SQLite 开发/测试路径（它是 dev/test 默认档，不是生产退路）。

## 4. Agent Runtime 接入

- 每个 `ChannelTurnInput` 和工具 `TurnContext` 都显式携带产品 `app_id`。
- 在产品 application 层实现 `ProductTurnServices`，提供产品 session/profile 准备、
  `ProductPromptContext`、onboarding 与稳定排序的 after-turn hooks。
- 产品实现只返回中性 Runtime DTO；`app/agent_runtime/` 不 import 产品包，也不按具体
  `app_id` 字符串分支。
- 若现有 `ProductTurnServices` 不能表达真实需求，先证明需求是跨产品 Runtime 概念，再以
  最小加性契约扩展；产品领域状态不得下沉到 Runtime。
- MemorySink、主动投递与 `runtime_ownerships` 不是必选脚手架。只有产品 PRD 确实需要时才
  实现，并在那时确定所有权、事务和恢复语义。

## 5. 工具策略

- 共享工具 schema/handler 使用 `app/tools/`；产品专属 schema、handler 与策略放在
  `app/products/<app_id>/tools/`。
- 为产品构造独立 `ToolRegistry` 与 `ToolPolicy(app_id=APP_ID, ...)`，显式列出可见和可执行
  工具；不修改共享默认 catalog 来“顺便”开放产品工具。
- executor 必须继续校验工具存在、产品允许以及 `TurnContext.app_id == ToolPolicy.app_id`。
- 产品首轮工具选择、功能 flag 和渠道能力 gating 都属于该产品 policy。

## 6. 生命周期与主动任务

- 产品 startup/shutdown 由 manifest 暴露，再由 `app/bootstrap/` composition root 组合。
- 产品对用户暴露的审核理由、错误消息和固定降级话术必须从 `default_language` 对应的服务端文案目录
  解析；HTTP `detail`、工具 `error_code`、领域 status/reason 等机器字段保持稳定英文，客户端和业务逻辑
  不得匹配展示文案。新增用户文案时需同时补齐产品支持的语言并增加回落测试。
- Web 页面标题、按钮、标签、占位符和动态状态也必须从产品语言目录解析；服务端下发选项时采用
  `key/code + label` 结构，`key/code` 作为协议和持久化值，`label` 只负责展示。FAQ 等长文案应由
  服务端按产品语言下发，静态文件只可保留默认语言降级内容。
- 固定欢迎语等会写入用户历史的内容，在创建时按产品默认语言生成；切换默认语言不迁移既有记录。
  Prompt、人设正文与用户提交内容不应为了 UI 翻译被机械替换，应单独评估生成语义。
- scheduler 默认作为独立进程；多实例场景必须定义 claim/lease、幂等键和重放行为。
- 不需要主动消息的产品不实现 scheduler、通知表或 `ProactiveDeliveryAdapter`。
- 若需要主动消息，先按真实载荷泛化仍含 Companion World 语义的共享协议，再实现该产品
  adapter；不得让 Runtime import 产品类型。

## 7. 最小验收矩阵

优先运行与新产品接入面直接相关的聚焦测试：

1. 注册表拒绝未知/disabled/重复 `app_id`，生产注册表无测试产品。
2. 固定 namespace 可访问；其他产品 session 访问时返回 401，不能由 Header 绕过。
3. account/product 错配在 turn 副作用前失败，消息、profile、quota、onboarding 均无写入。
4. 最小真实 turn 使用该产品 `ProductTurnServices` 成功，不依赖朝夕默认实现。
5. 产品只能看到和执行其 `ToolPolicy` 允许的工具，不能调用其他产品工具。
6. 同一真人在两个产品的 session、入口账号、钱包、配额、referral 和新客权益互不影响。
7. account-scoped message、memory 和文件在不同 `account_id` 间不串读。
8. 涉及 schema/持久化时，对 SQLite 与 PostgreSQL 运行同一关键隔离和并发用例。
9. `tests/test_layer_boundaries.py` 与 `tests/test_documentation_links.py` 通过。

真实产品若接入记忆、主动消息或复杂领域状态机，还必须追加对应幂等、并发、恢复和数据删除
用例；不能用中性 `test_product` 的公共基座测试替代产品端到端验收。

## 8. 合并前检查

- `app/agent_runtime/`、`app/platform/`、`app/tools/` 不出现产品 import 或具体产品字符串分支。
- 产品包之间无互相 import；只有 composition root 同时看见共享实现与产品 manifest。
- `app/main.py` 保持薄入口；根 `app/turn_service.py`、`app/prompt_builder.py`、
  `app/reminder_utils.py` 不新增业务定义。
- 账号、文件和 DB 查询保持 `account_id` 隔离，产品级资产保持 `app_id` 隔离。
- 外部 API 兼容面、双数据库后端、部署入口和回滚路径已在聚焦测试中验证。
- 实施结果回写产品 manifest、相关 ADR 与 `docs/STATUS.md`，完成的执行计划再归档。

架构背景见[多产品模块化单体 ADR](../architecture/shared/data/multi_product_modular_monolith_design.md)，
当前公共基座的实施边界见[多产品实施计划](../plans/shared/multi_product_modular_monolith_implementation_plan.md)。
