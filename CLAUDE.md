# AI4ALL 微信 Bot — Claude 入口

@AGENTS.md

`AGENTS.md` 是 Codex、Claude 等 Coding Agent 共用的唯一开发规范与模块地图；本文件不再复制
运行、测试和目录说明，避免两份规则长期漂移。

特别注意：Plum 上下文中用户只说“启动服务”时，使用 `make plum-local-start`。这是当前本地
测试配置的可维护封装，不适用于生产；选项不再满足任务时，先检查并修改 `Makefile` 和
`AGENTS.md`，不要直接退回通用 `uvicorn` 命令。

补充约定：优先使用中文答复，其次英文。

开始任务前按顺序阅读：

1. [`AGENTS.md`](AGENTS.md)
2. [`docs/README.md`](docs/README.md)
3. 与需求对应的 [`docs/plans/`](docs/plans/README.md) 或
   [`docs/architecture/`](docs/architecture/README.md) 文档
