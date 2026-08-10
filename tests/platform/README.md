# 平台测试

这里放置跨产品共享的平台能力回归测试，例如 PostgreSQL 后端、审核、计费、配额、
产品注册和运行时健康检查。产品 API 和领域行为测试放在对应的产品目录；跨产品
隔离契约仍归入 shared 或保留在顶层，避免重复测试。

运行方式：

```bash
make test-platform
pytest tests/platform/ -q
```
