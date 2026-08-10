# 回归测试指南

主应用和主 pytest 统一使用 PostgreSQL。测试会由 `pytest-postgresql` 启动临时实例，
先迁移一次模板库，再为 DB/integration 用例克隆隔离数据库；不会连接本地开发库或生产库。

## 常用档位

```bash
make test-unit       # 纯 unit，最快
make test-fast       # 排除 integration
make test             # PostgreSQL 全量门禁
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

- 修改单一产品业务：先跑对应产品档，再跑受影响的平台/共享模块；
- 修改数据库、迁移、计费、审核、Runtime 或公共路由：至少跑对应 `platform`/`shared` 档；
- 修改跨模块契约或准备提交 PR：运行 `make test` 全量 PostgreSQL 回归；
- CI 门禁只保留 PostgreSQL 全量档，不重复运行 SQLite 主应用测试。
