# AI4ALL 微信 Bot —— 常用开发命令
# 主测试固定使用 PostgreSQL：pytest-postgresql 启动临时实例并从预迁移模板克隆逐测试数据库，
# 既不连接本地 dev 库也不连接生产库；unit marker 不请求 DB，因此仍可秒级运行。

PY := .venv/bin/python
PYTEST := .venv/bin/pytest
COMPOSE := docker compose -f compose.dev.yml
LOCAL_PG_PORT ?= 55432
LOCAL_DATABASE_URL ?= postgresql://ai4all:ai4all-local@127.0.0.1:$(LOCAL_PG_PORT)/ai4all_dev
PLUM_DATABASE_URL ?= postgresql://ai4all:ai4all-local@127.0.0.1:$(LOCAL_PG_PORT)/ai4all_plum_dev
PLUM_DEV_PORT ?= 8180
PLUM_CHARACTER_MODERATION_MOCK_STATUS ?= pending_review

.PHONY: help test test-unit test-fast test-zhaoxi test-mingchan test-plum \
	test-plum-fast test-plum-db test-platform test-shared \
	test-shared-runtime test-shared-infrastructure test-shared-contracts \
	pg-local-up pg-local-init pg-local-status \
	pg-local-stop pg-local-reset run plum-local-init plum-local-run plum-local-start \
	sync-plum-tags

help:
	@echo "make test       # PostgreSQL 全量测试（预迁移模板 + 逐测试克隆）"
	@echo "make test-unit  # 纯 unit 快档（不碰 DB/FastAPI，秒级反馈）"
	@echo "make test-fast  # 跳过 integration 全栈用例（约 2x，中间档）"
	@echo "make test-zhaoxi # 朝夕产品回归"
	@echo "make test-mingchan # 鸣蝉产品回归"
	@echo "make test-plum  # Plum 产品回归"
	@echo "make test-plum-fast # Plum 快速回归（排除 integration/slow）"
	@echo "make test-plum-db # Plum 数据库回归"
	@echo "make test-platform # 跨产品平台能力回归"
	@echo "make test-shared # 共享运行时/基础设施回归"
	@echo "make test-shared-runtime # 共享 Runtime 回归"
	@echo "make test-shared-infrastructure # 共享基础设施回归"
	@echo "make test-shared-contracts # 跨产品协议/隔离回归"
	@echo "make pg-local-up     # 启动本地 PostgreSQL 16（端口 55432）"
	@echo "make pg-local-init   # 幂等创建并迁移主应用/Plum 本地库"
	@echo "make pg-local-status # 查看本地 PostgreSQL 状态"
	@echo "make pg-local-stop   # 停止本地 PostgreSQL（保留数据）"
	@echo "make pg-local-reset CONFIRM=1 # 删除并重建本地 PG 数据"
	@echo "make run        # 使用本地 PG 启动服务（端口 8180）"
	@echo "make plum-local-init # 初始化 Plum 隔离 PG 库与固定测试账号"
	@echo "make plum-local-run  # 启动 Plum 本地后端（SSE 流式，端口 8180）"
	@echo "make plum-local-start # 初始化并启动 Plum 本地测试后端（推荐入口）"
	@echo "make sync-plum-tags # 校验并同步 Plum 离线 Tag 词表到 DATABASE_URL"

# 唯一全量档：固定使用 pytest-postgresql 临时实例。
test:
	$(PYTEST) tests/ -q

# 纯 unit 快档：marker 由 conftest 按 fixture 依赖自动派生（不含 DB/FastAPI fixture）
test-unit:
	$(PYTEST) tests/ -m unit -q

# 中间档：跳过 integration（FastAPI TestClient 全栈）这一最重的用例群
test-fast:
	$(PYTEST) tests/ -m "not integration" -q

# 产品/平台快捷档与 pytest marker 一一对应，便于本地按影响面缩短反馈周期。
test-zhaoxi:
	$(PYTEST) tests/ -m zhaoxi -q

test-mingchan:
	$(PYTEST) tests/ -m mingchan -q

test-plum:
	$(PYTEST) tests/ -m plum -q

test-plum-fast:
	$(PYTEST) tests/ -m "plum and not integration and not slow" -q

test-plum-db:
	$(PYTEST) tests/ -m "plum and db" -q

test-platform:
	$(PYTEST) tests/ -m platform -q

test-shared:
	$(PYTEST) tests/ -m shared -q

test-shared-runtime:
	$(PYTEST) tests/shared/runtime/ -q

test-shared-infrastructure:
	$(PYTEST) tests/shared/infrastructure/ -q

test-shared-contracts:
	$(PYTEST) tests/shared/contracts/ -q

pg-local-up:
	LOCAL_PG_PORT=$(LOCAL_PG_PORT) $(COMPOSE) up -d --wait postgres

pg-local-init:
	AI4ALL_ALLOW_AUTO_MIGRATE=1 LOCAL_DATABASE_URL="$(LOCAL_DATABASE_URL)" \
		PLUM_DATABASE_URL="$(PLUM_DATABASE_URL)" $(PY) -m scripts.init_local_postgres

pg-local-status:
	LOCAL_PG_PORT=$(LOCAL_PG_PORT) $(COMPOSE) ps

pg-local-stop:
	LOCAL_PG_PORT=$(LOCAL_PG_PORT) $(COMPOSE) stop postgres

pg-local-reset:
	@test "$(CONFIRM)" = "1" || (echo "拒绝重置：请显式传入 CONFIRM=1"; exit 1)
	LOCAL_PG_PORT=$(LOCAL_PG_PORT) $(COMPOSE) down --volumes
	$(MAKE) pg-local-up pg-local-init LOCAL_PG_PORT=$(LOCAL_PG_PORT) \
		LOCAL_DATABASE_URL="$(LOCAL_DATABASE_URL)" PLUM_DATABASE_URL="$(PLUM_DATABASE_URL)"

run:
	DATABASE_URL="$(LOCAL_DATABASE_URL)" .venv/bin/uvicorn app.main:app --reload --port 8180

sync-plum-tags:
	$(PY) -m scripts.sync_plum_tags

# Plum 前后端联调强制使用隔离的本地 PG 数据库，不读取 .env 中的 DATABASE_URL。
# 模型密钥仍从 .env 读取；PLUM_DATABASE_URL 可覆盖，但初始化脚本只接受 loopback + 固定库名。
plum-local-init:
	APP_ENV=local DATABASE_URL="$(PLUM_DATABASE_URL)" PLUM_ENABLED=true PLUM_DEV_MODE=true \
		PLUM_PUBLIC_TEST_AUTH_ENABLED=false \
		$(PY) -m scripts.seed_plum_dev

# 游客态与邮箱验证码登录是当前联调主链路，本地默认打开；
# SMTP 凭据与 PLUM_EMAIL_OTP_PEPPER 仍从 .env 读取，不写进 Makefile。
plum-local-run:
	APP_ENV=local DATABASE_URL="$(PLUM_DATABASE_URL)" PLUM_ENABLED=true PLUM_DEV_MODE=true \
		PLUM_PUBLIC_TEST_AUTH_ENABLED=false PLUM_CHAT_STREAMING_ENABLED=true \
		PLUM_CHARACTER_MODERATION_MOCK_STATUS="$(PLUM_CHARACTER_MODERATION_MOCK_STATUS)" \
		PLUM_GUEST_CHAT_ENABLED=true PLUM_EMAIL_AUTH_ENABLED=true \
		PROACTIVE_SCHEDULER_ENABLED=false DREAMING_SCHEDULER_ENABLED=false \
		USER_META_SCHEDULER_ENABLED=false \
		.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $(PLUM_DEV_PORT) --reload

# 本地测试的统一入口：启动隔离 PG、幂等建库/迁移并补齐固定测试数据。
plum-local-start: pg-local-up pg-local-init plum-local-init
	$(MAKE) plum-local-run PLUM_DATABASE_URL="$(PLUM_DATABASE_URL)" \
		PLUM_DEV_PORT="$(PLUM_DEV_PORT)"
