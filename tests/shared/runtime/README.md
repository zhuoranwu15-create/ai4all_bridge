# 共享 Runtime 测试

覆盖 prompt/context、turn contract、tool registry 和运行时边界；这些测试不属于任一
具体产品，但会被所有产品复用。

```bash
pytest tests/shared/runtime/ -q
```
