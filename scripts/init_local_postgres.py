"""Provision and migrate the loopback-only PostgreSQL databases used in development."""
from __future__ import annotations

import os
from typing import Dict

from psycopg import connect, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.config import settings
from app.db import init_db
from app.db._backend import close_pg_pool
from app.db._core import AUTO_MIGRATE_ENV, auto_migrate_allowed


MAIN_DATABASE_ENV = "LOCAL_DATABASE_URL"
PLUM_DATABASE_ENV = "PLUM_DATABASE_URL"
DEFAULT_MAIN_DATABASE_URL = (
    "postgresql://ai4all:ai4all-local@127.0.0.1:55432/ai4all_dev"
)
DEFAULT_PLUM_DATABASE_URL = (
    "postgresql://ai4all:ai4all-local@127.0.0.1:55432/ai4all_plum_dev"
)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validated_conninfo(database_url: str, expected_database: str) -> Dict[str, str]:
    """Parse a development DSN and reject remote or unexpectedly named databases."""
    try:
        params = conninfo_to_dict(database_url)
    except Exception as exc:
        raise ValueError(f"invalid PostgreSQL DSN for {expected_database}: {exc}") from exc

    host = str(params.get("host") or "").strip().lower()
    hostaddr = str(params.get("hostaddr") or "").strip().lower()
    database = str(params.get("dbname") or "").strip()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing non-loopback PostgreSQL host {host!r}; local initialization "
            "only accepts localhost/127.0.0.1/::1"
        )
    if hostaddr and hostaddr not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing non-loopback PostgreSQL hostaddr {hostaddr!r}; local "
            "initialization only accepts localhost/127.0.0.1/::1"
        )
    if database != expected_database:
        raise ValueError(
            f"refusing database {database!r}; expected local development database "
            f"{expected_database!r}"
        )
    return params


def _ensure_database(database_url: str, database_name: str) -> None:
    """Create one approved local development database when it does not exist."""
    _validated_conninfo(database_url, database_name)
    maintenance_url = make_conninfo(database_url, dbname="postgres")
    with connect(maintenance_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database_name,)
        ).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
            print(f"created database {database_name}")
        else:
            print(f"database {database_name} already exists")


def _migrate_database(database_url: str, database_name: str) -> None:
    """Run the application migration chain against one validated local database."""
    _validated_conninfo(database_url, database_name)
    original_url = settings.database_url
    close_pg_pool()
    settings.database_url = database_url
    try:
        init_db()
    finally:
        close_pg_pool()
        settings.database_url = original_url
    print(f"migrated database {database_name}")


def main() -> None:
    """Create and migrate both local application databases idempotently."""
    if not auto_migrate_allowed():
        raise SystemExit(
            f"local initialization requires {AUTO_MIGRATE_ENV}=1 in this subprocess"
        )

    database_urls = {
        "ai4all_dev": os.getenv(MAIN_DATABASE_ENV, DEFAULT_MAIN_DATABASE_URL),
        "ai4all_plum_dev": os.getenv(PLUM_DATABASE_ENV, DEFAULT_PLUM_DATABASE_URL),
    }
    # Validate every target before making the first database change.
    for database_name, database_url in database_urls.items():
        _validated_conninfo(database_url, database_name)
    for database_name, database_url in database_urls.items():
        _ensure_database(database_url, database_name)
        _migrate_database(database_url, database_name)


if __name__ == "__main__":
    main()
