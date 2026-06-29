# AI4ALL 微信 Bot —— 常用开发命令
# 测试两档：日常用 SQLite（快），上线前用 PG（保真，与生产后端对齐）。
# PG 档由 conftest 的 AI4ALL_TEST_DB 开关触发，使用 pytest-postgresql 每测试独立临时库，
# 既不连本地 dev 库也不连生产库；ambient DATABASE_URL 由 conftest guard 清空，无需手动覆盖。

PY := .venv/bin/python
PYTEST := .venv/bin/pytest

.PHONY: help test test-pg run

help:
	@echo "make test     # 默认 SQLite 档全量测试（快，日常迭代用）"
	@echo "make test-pg  # PostgreSQL 档全量测试（保真，上线前/CI 用）"
	@echo "make run      # 本地启动服务（端口 8180）"

# 默认 SQLite/内存档：快速回归
test:
	$(PYTEST) tests/ -q

# PG 档：与生产后端对齐的强制门禁（CI 与上线前手动复跑都用这条）
test-pg:
	AI4ALL_TEST_DB=postgres $(PYTEST) tests/ -q

run:
	.venv/bin/uvicorn app.main:app --reload --port 8180
