"""Guards for the PG-only pytest database infrastructure."""
import psycopg

from app.db._core import _MIGRATIONS


def test_postgres_template_is_migrated_once_to_latest_schema(postgresql_proc):
    """The session template must be complete before per-test databases are cloned."""
    with psycopg.connect(
        host=postgresql_proc.host,
        port=postgresql_proc.port,
        user=postgresql_proc.user,
        password=postgresql_proc.password,
        dbname=postgresql_proc.template_dbname,
    ) as conn:
        latest = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]

    assert latest == max(version for version, _apply in _MIGRATIONS)


def test_fresh_db_always_uses_isolated_postgres_clone(fresh_db):
    assert fresh_db.database_url.startswith("postgresql://")
    assert fresh_db.database_path.endswith("test.db")  # non-DB file setting remains isolated
