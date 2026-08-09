# AI4ALL 微信 Bot —— 常用开发命令
# 测试两档：日常用 SQLite（快），上线前用 PG（保真，与生产后端对齐）。
# PG 档由 conftest 的 AI4ALL_TEST_DB 开关触发，使用 pytest-postgresql 每测试独立临时库，
# 既不连本地 dev 库也不连生产库；ambient DATABASE_URL 由 conftest guard 清空，无需手动覆盖。

PY := .venv/bin/python
PYTEST := .venv/bin/pytest
PLUM_DEV_DB ?= data/plum_dev.sqlite3
PLUM_DEV_PORT ?= 8180

.PHONY: help test test-unit test-fast test-pg run plum-local-init plum-local-run

help:
	@echo "make test       # 默认 SQLite 档全量测试（日常迭代用）"
	@echo "make test-unit  # 纯 unit 快档（不碰 DB/FastAPI，秒级反馈）"
	@echo "make test-fast  # 跳过 integration 全栈用例（约 2x，中间档）"
	@echo "make test-pg    # PostgreSQL 档全量测试（保真，上线前/CI 用）"
	@echo "make run        # 本地启动服务（端口 8180）"
	@echo "make plum-local-init # 初始化 Plum 隔离 SQLite 与固定测试账号"
	@echo "make plum-local-run  # 启动 Plum 本地后端（SSE 流式，端口 8180）"

# 默认 SQLite/内存档：快速回归
test:
	$(PYTEST) tests/ -q

# 纯 unit 快档：marker 由 conftest 按 fixture 依赖自动派生（不含 DB/FastAPI fixture）
test-unit:
	$(PYTEST) tests/ -m unit -q

# 中间档：跳过 integration（FastAPI TestClient 全栈）这一最重的用例群
test-fast:
	$(PYTEST) tests/ -m "not integration" -q

# PG 档：与生产后端对齐的强制门禁（CI 与上线前手动复跑都用这条）
test-pg:
	AI4ALL_TEST_DB=postgres $(PYTEST) tests/ -q

run:
	.venv/bin/uvicorn app.main:app --reload --port 8180

# Plum 前后端联调默认不读取 .env 中的 DATABASE_URL，避免误连已有 PostgreSQL。
# 模型密钥仍从 .env 读取；PLUM_DEV_DB 可在命令行覆盖以切换隔离数据库文件。
plum-local-init:
	APP_ENV=local DATABASE_URL= DATABASE_PATH="$(PLUM_DEV_DB)" PLUM_ENABLED=true PLUM_DEV_MODE=true \
		PLUM_PUBLIC_TEST_AUTH_ENABLED=false \
		$(PY) scripts/seed_plum_dev.py

plum-local-run:
	APP_ENV=local DATABASE_URL= DATABASE_PATH="$(PLUM_DEV_DB)" PLUM_ENABLED=true PLUM_DEV_MODE=true \
		PLUM_PUBLIC_TEST_AUTH_ENABLED=false PLUM_CHAT_STREAMING_ENABLED=true \
		PROACTIVE_SCHEDULER_ENABLED=false DREAMING_SCHEDULER_ENABLED=false \
		USER_META_SCHEDULER_ENABLED=false \
		.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $(PLUM_DEV_PORT) --reload
