# Plum 最小回归验收矩阵

这份清单用于 Plum 后续业务开发的基线，不代表产品 PRD 或生产启用批准。每项先记录
现有测试证据；新增能力必须在对应行补测试后才能标记完成。

| 能力 | 当前证据 | 状态 |
| --- | --- | --- |
| 产品注册、固定 namespace、启停 | `test_mvp.py`、平台 product registry/边界测试 | 已覆盖 |
| 账号、membership、session 产品隔离 | `test_mvp.py`、`tests/shared/test_multi_product_account_resolution.py` | 已覆盖 |
| 最小文本 turn 与取消/恢复 | `test_mvp.py` | 已覆盖 |
| 固定价格 wallet 扣款、幂等、失败退款 | `test_mvp.py`、平台 billing 测试 | 已覆盖 |
| 流式 turn、取消、过期运行回收 | `test_mvp.py` | 已覆盖 |
| 本地审核红线仍启用 | `tests/platform/test_moderation_product_policy.py` | 已覆盖 |
| Plum 不调用阿里云图片/LLM provider | `tests/platform/test_moderation_worker.py` | 已覆盖 |
| 异步审核复用入队规则快照 | `tests/platform/test_moderation_worker.py` | 已覆盖 |
| 审核任务 `(app_id, idempotency_key)` 作用域 | `tests/platform/test_moderation_product_scope.py` | 已覆盖 |
| 工具白名单与产品策略 | 尚无 Plum 专属工具矩阵 | 待补 |
| 多语言文案与默认语言 | 共享 localization 测试，缺 Plum 文案目录验收 | 待补 |
| 数据删除/reset 覆盖产品资产 | 共享 wipe/reset 测试，缺 Plum 端到端资产清单 | 待补 |
| 海外、多语言审核 provider 与规则集 | 当前明确未选定 | 待产品决策 |
| 生产启用检查单、数据区域与留存 | 临时项目笔记 | 待产品决策 |

## 运行入口

```bash
make test-plum-fast
make test-plum-db
make test-plum
```

平台审核和共享隔离契约属于 Plum 的依赖基线，必要时分别运行：

```bash
pytest tests/platform/ -m 'moderation or billing' -q
make test-shared
```
