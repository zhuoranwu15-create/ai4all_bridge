# 共享测试

这里放置不属于单一产品的契约测试，包括账号/渠道隔离、多产品生命周期、通用
错误与本地化协议、Runtime 中性契约及层边界检查。

运行方式：

```bash
make test-shared
pytest tests/shared/ -q
```
