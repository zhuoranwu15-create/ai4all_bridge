# 回归测试指南

主应用和主 pytest 统一使用 PostgreSQL。测试会由 `pytest-postgresql` 启动临时实例，
先迁移一次模板库，再为 DB/integration 用例克隆隔离数据库；不会连接本地开发库或生产库。

## 常用档位

```bash
make test-unit       # 纯 unit，最快
make test-fast       # 排除 integration
make test            # PostgreSQL 仓库全集；仅显式完整回归/门禁使用
```

## 按产品运行

```bash
make test-zhaoxi
make test-mingchan
make test-plum
make test-plum-fast
make test-plum-db
make test-platform
```

## 按共享模块运行

```bash
make test-shared-runtime
make test-shared-infrastructure
make test-shared-contracts
```

也可以组合 pytest marker，例如：

```bash
pytest tests/ -m "plum and db" -q
pytest tests/ -m "zhaoxi and not integration" -q
pytest tests/platform/ -q
```

## 选择策略

- 修改 Plum、鸣蝉或朝夕的单一产品业务：默认只跑对应产品档，再跑被改动直接影响的平台/共享模块聚焦测试；
- 修改数据库、迁移、计费、审核、Runtime 或公共路由：补对应 `platform`/`shared` 档或具体契约测试，但不因此自动扩大为仓库全集；
- 同时修改多个产品：运行相关产品档的并集，再补直接受影响的共享测试；
- Coding Agent 只有在用户明确要求完整回归，或执行已明确规定的 CI、发布门禁时，才运行 `make test` / `pytest tests/`；准备提交 PR 本身不构成运行全集的理由；
- CI 门禁只保留 PostgreSQL 全量档，不重复运行 SQLite 主应用测试。
