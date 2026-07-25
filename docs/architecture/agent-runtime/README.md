# Agent Runtime 架构

本目录记录形态与产品无关的 Agent Runtime：上下文、Prompt/Turn 编排、LLM provider、
短长期记忆、Dreaming、工具调用及 TDAI 接入。

Runtime 不得依赖 `app/products/*` 的类型、导入或 `app_id` 分支。产品侧通过已定义的端口、
加性 context 和 after-turn 接缝接入；产品语义保留在
[`../products/`](../products/README.md)。
