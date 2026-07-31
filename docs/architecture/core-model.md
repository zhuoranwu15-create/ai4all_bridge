# AI4ALL 核心模型与术语

更新时间：2026-07-31

本文定义跨产品开发时必须统一使用的概念、作用域和所有权。它回答“什么是产品、形态、渠道、
真人和 Agent Runtime”，不重复部署拓扑、详细数据模型或朝夕产品规则。

- 当前系统与生产拓扑见[总体架构](overview.md)。
- 表结构和技术平面见[系统设计](system_design.md)。
- 新产品落地步骤见[新增产品开发清单](../guides/adding-product.md)。

## 1. 演进主线

AI4ALL 最初只有一种业务形态：用户在朝夕相伴官网完成手机号注册和微信扫码，随后在微信私聊
中与一位独立 AI 聊天。后端围绕这条链路逐步增加了账号隔离、Soul、会话、记忆、提醒、主动
消息、搜索、审核、权益和运营能力。

朝夕 Native App 引入了更丰富的产品形态：一个真人可以拥有私人世界和多位居民，每位居民仍
通过底层 Agent Runtime 与用户聊天，同时增加 Feed、通知、信箱、生命周期、访问和真人会话等
朝夕专属领域能力。这个变化促使后端重构为：

```text
多渠道入口
    → 产品 API / 产品领域
        → 形态无关 Agent Runtime
            → 跨产品 Platform 与共享 PostgreSQL
```

未来其他围绕 AI 聊天的业务也进入同一后端，但必须以独立 `app_id`、产品边界和数据作用域接入，
不能把朝夕的 Companion World、居民或主动消息假设当成所有产品的默认模型。

## 2. 产品、形态与渠道

| 概念 | 定义 | 当前实例 | 不是 |
| --- | --- | --- | --- |
| 平台（platform） | 承载多个产品的共享后端、Runtime 和基础服务 | AI4ALL Backend | 面向用户展示的单一产品名 |
| 产品（product） | 有独立业务目标、`app_id`、membership、契约和产品 owner 的业务容器 | 朝夕相伴 `zhaoxi` | 微信、iOS 或一个页面 |
| 业务形态（experience/form） | 同一产品内围绕核心价值形成的一套用户体验和领域组合 | 微信单 Agent 陪伴、Native App Companion World | 自动获得独立数据边界的新产品 |
| 渠道（channel） | 消息或操作进入/离开系统的协议与投递能力 | WeChat/OpenClaw、Web/H5、Native App | 产品领域本身 |
| 产品领域（product domain） | 只有该产品理解的业务概念和规则 | 朝夕的 universe、resident、relationship、mailbox | 可直接下沉 Runtime 的通用概念 |

当前生产注册表只启用 `zhaoxi`。候选产品只有在核心 PRD 冻结后才能创建自己的产品 manifest、
代码命名空间和生产入口。

## 3. 身份与状态作用域

| 锚点 | 作用域 | 当前职责 | 隔离要求 |
| --- | --- | --- | --- |
| `platform_user_id` | 真人级 | 手机号身份、真人资料，以及需要跨 Agent 聚合但仍按产品约束的主体 | 不能用任一 Agent `account_id` 代替真人 |
| `app_id` | 产品级 | 产品注册、membership、session、钱包/配额/邀请等产品隔离维度 | 所有跨产品资产必须显式带 `app_id` 或经可信产品上下文解析 |
| `(platform_user_id, app_id)` | 真人在某产品内 | `product_memberships`、产品 session 和真人级产品资产 | 同一真人在不同产品中的权益和状态不能串用 |
| runtime `account_id` | 单 Agent / 单关系运行时 | L1/L2、Soul、session/messages、Prompt、工具和 Agent 独立记忆 | 所有 Agent 数据查询必须带正确 `account_id` |
| `universe_id` | 朝夕私人世界 | Companion World 的居民集合、L3 typed facts、Feed、通知、信箱与访问 | 这是朝夕产品锚点，不能作为通用 Platform/Runtime 主键 |
| `resident_id` | 朝夕世界中的居民实体 | 将产品居民映射到独立 runtime account，并承载生命周期等领域状态 | resident 与 runtime account 不能混成同一领域对象 |
| channel binding / raw identity | 通道级 | 微信账号、sender、路由节点和投递能力到业务身份的可信映射 | 原始通道 ID 不能直接作为业务账号主键 |

三个最容易混淆的关系：

1. 一个真人可以进入多个产品，因此 `platform_user_id` 不等于某个产品 membership。
2. 一个真人在朝夕 App 中可以拥有多位居民，因此 `platform_user_id` 不等于 runtime `account_id`。
3. 一个 runtime account 可以通过不同渠道被访问，因此渠道身份不等于 `account_id`。

## 4. 代码所有权与依赖方向

| Owner | 代码位置 | 应承载的内容 | 禁止反向泄漏 |
| --- | --- | --- | --- |
| Composition root | `app/bootstrap/` | 注册可信产品、组合角色、路由和 lifecycle | 不承载产品业务规则 |
| Platform | `app/platform/`、共享 DB/repository | 身份、鉴权、配额、钱包、审核、媒体、网关、观测 | 不依赖 `app.products.zhaoxi` |
| Agent Runtime | `app/agent_runtime/` | turn、Prompt、LLM、上下文、工具执行与形态无关记忆接缝 | 不出现产品字符串分支或 Companion World 类型 |
| Product | `app/products/<app_id>/` | 产品 API、领域、应用服务、基础设施 adapter、工具策略和 lifecycle | 不 import 其他产品实现 |

固定依赖方向是：

```text
API adapter → product domain/application → AgentRuntimePort → runtime implementation
                             └────────────→ shared Platform
```

微信兼容形态可以由朝夕 API adapter 直接组合 Runtime；Companion World 必须先由朝夕产品域解析
owner、resident、ACL 和世界上下文，再通过中性端口调用 Runtime。Runtime 不得反向读取 World。

## 5. 文档归属规则

- 跨产品都成立的技术规则写入 `architecture/shared/`。
- 与产品形态无关的聊天执行机制写入 `architecture/agent-runtime/`。
- 只有朝夕理解的产品领域写入 `architecture/products/zhaoxi/`。
- 用户价值、流程和验收写入 `products/<app_id>/`，不能用技术设计替代 PRD。
- 某渠道上的产品体验仍归对应产品；只有协议、接入节点或共享认证属于 shared。
- 当前进度只写 `STATUS.md`，完成计划回写稳定事实后归档。

判断一个新能力应放在哪里时，先问：如果第二产品不用 Companion World，它是否仍能原样使用？
能原样使用的候选 shared/Runtime；需要朝夕语义、字段或状态机的留在 `product:zhaoxi`。
