# 共享测试

这里放置不属于单一产品的共享回归测试，按责任拆成三个子模块：

- `runtime/`：prompt/context、turn contract、tool registry 和 Runtime 边界；
- `infrastructure/`：LLM/provider、OpenClaw、ASR、TDAI、媒体和外部内容适配器；
- `contracts/`：账号隔离、session/auth、OpenAPI、错误响应、本地化和产品边界。

运行方式：

```bash
make test-shared
make test-shared-runtime
make test-shared-infrastructure
make test-shared-contracts
```
