"""PG 迁移 opt-in 闸（app.db._core._guard_unattended_pg_migrations）。

背景：生产机同时是开发机，`.env` 指向生产 PG，任何在仓库目录里手跑的 python 进程都会读到
它，于是自测顺手跑一条触发 init_db 的命令就能把未评审的迁移写进生产库。闸门要求：只有
显式带 ``AI4ALL_ALLOW_AUTO_MIGRATE=1`` 的受控部署进程可以应用待执行迁移。
"""

import pytest

from app.db._core import (
    AUTO_MIGRATE_ENV,
    _guard_unattended_pg_migrations,
    auto_migrate_allowed,
    connect,
)
import app.db._core as _core


@pytest.mark.parametrize(
    "value, allowed",
    [("1", True), ("true", True), ("TRUE", True), ("on", True), ("yes", True),
     ("0", False), ("false", False), ("", False), ("  ", False)],
)
def test_auto_migrate_allowed_parses_env(monkeypatch, value, allowed):
    monkeypatch.setenv(AUTO_MIGRATE_ENV, value)
    assert auto_migrate_allowed() is allowed


def test_auto_migrate_denied_when_env_absent(monkeypatch):
    monkeypatch.delenv(AUTO_MIGRATE_ENV, raising=False)
    assert auto_migrate_allowed() is False


def test_guard_allows_when_no_pending_migrations(monkeypatch, fresh_db):
    """库已追平时闸门不拦——生产上大多数重启走的就是这条路。"""
    monkeypatch.delenv(AUTO_MIGRATE_ENV, raising=False)
    with connect() as conn:
        _guard_unattended_pg_migrations(conn)


def test_guard_blocks_pending_migrations_without_opt_in(monkeypatch, fresh_db):
    """有待执行迁移且没 opt-in → 直接抛错，且错误里带上版本号和正确做法。"""
    monkeypatch.delenv(AUTO_MIGRATE_ENV, raising=False)
    monkeypatch.setattr(
        _core, "_MIGRATIONS", _core._MIGRATIONS + [(999_999, lambda conn: None)]
    )
    with connect() as conn:
        with pytest.raises(RuntimeError) as err:
            _guard_unattended_pg_migrations(conn)
    message = str(err.value)
    assert "999999" in message
    assert AUTO_MIGRATE_ENV in message


def test_guard_allows_pending_migrations_with_opt_in(monkeypatch, fresh_db):
    """受控部署（systemd 单元带 opt-in）照常应用迁移，行为不变。"""
    monkeypatch.setenv(AUTO_MIGRATE_ENV, "1")
    monkeypatch.setattr(
        _core, "_MIGRATIONS", _core._MIGRATIONS + [(999_999, lambda conn: None)]
    )
    with connect() as conn:
        _guard_unattended_pg_migrations(conn)
