# 产品文档

这里按产品命名空间维护当前有效的需求、用户/API 交接和产品能力说明。
目录名就是产品所有权；每个产品目录的 `README.md` 是该产品的文档 manifest，必须说明
`app_id`、产品状态、入口渠道、代码命名空间和当前兼容 API。

| 产品 | `app_id` | 状态 | 文档入口 |
| --- | --- | --- | --- |
| 朝夕相伴 | `zhaoxi` | 已启用 | [产品 manifest](zhaoxi/README.md) |
| 鸣蝉 | `mingchan` | 拆分实施中，生产未启用 | [产品 manifest](mingchan/README.md) |

鸣蝉是已经冻结边界、正在从原 App/Companion World 实现中拆出的真实产品，不属于候选占位。
Fatetell、Nooki 等候选产品尚未冻结可开发的核心 PRD，因此不建立空目录或占位能力文档。
哪个产品先具备真实需求，哪个先按[新增产品开发清单](../guides/adding-product.md)建立产品
manifest 和总 PRD，再根据实际范围添加能力、架构设计与实施计划。

跨产品的身份、钱包、配额、审核、接入节点和 API 边界不写在这里；它们属于
[`../architecture/shared/`](../architecture/shared/README.md)。形态无关的 LLM、上下文与记忆运行机制属于
[`../architecture/agent-runtime/`](../architecture/agent-runtime/README.md)。
