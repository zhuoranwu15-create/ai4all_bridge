"""Safety checks for the local PostgreSQL provisioning entry point."""
import pytest

from scripts import init_local_postgres as local_pg
from scripts import seed_plum_dev as plum_seed


def test_validated_conninfo_accepts_expected_loopback_database():
    params = local_pg._validated_conninfo(
        "postgresql://ai4all:local@127.0.0.1:55432/ai4all_dev",
        "ai4all_dev",
    )

    assert params["host"] == "127.0.0.1"
    assert params["dbname"] == "ai4all_dev"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://ai4all:local@db.example.com:5432/ai4all_dev",
        "postgresql://ai4all:local@127.0.0.1:55432/production",
        "postgresql:///ai4all_dev",
        (
            "postgresql://ai4all:local@localhost:55432/ai4all_dev"
            "?hostaddr=203.0.113.10"
        ),
    ],
)
def test_validated_conninfo_rejects_unsafe_target(database_url):
    with pytest.raises(ValueError):
        local_pg._validated_conninfo(database_url, "ai4all_dev")


def test_main_requires_migration_opt_in_before_provisioning(monkeypatch):
    monkeypatch.setattr(local_pg, "auto_migrate_allowed", lambda: False)
    called = []
    monkeypatch.setattr(local_pg, "_ensure_database", lambda *args: called.append(args))

    with pytest.raises(SystemExit, match="AI4ALL_ALLOW_AUTO_MIGRATE=1"):
        local_pg.main()

    assert called == []


def test_main_validates_all_targets_before_provisioning(monkeypatch):
    monkeypatch.setattr(local_pg, "auto_migrate_allowed", lambda: True)
    monkeypatch.setenv(
        local_pg.MAIN_DATABASE_ENV,
        "postgresql://ai4all:local@127.0.0.1:55432/ai4all_dev",
    )
    monkeypatch.setenv(
        local_pg.PLUM_DATABASE_ENV,
        "postgresql://ai4all:local@remote.example:5432/ai4all_plum_dev",
    )
    called = []
    monkeypatch.setattr(local_pg, "_ensure_database", lambda *args: called.append(args))

    with pytest.raises(ValueError, match="non-loopback"):
        local_pg.main()

    assert called == []


def test_plum_seed_rejects_remote_database_before_migration(monkeypatch):
    monkeypatch.setattr(plum_seed.settings, "app_env", "local")
    monkeypatch.setattr(plum_seed.settings, "plum_dev_mode", True)
    monkeypatch.setattr(
        plum_seed.settings,
        "database_url",
        "postgresql://ai4all:local@db.example.com:5432/ai4all_plum_dev",
    )
    migrated = []
    monkeypatch.setattr(plum_seed, "init_db", lambda: migrated.append(True))

    with pytest.raises(SystemExit, match="loopback ai4all_plum_dev"):
        plum_seed.main()

    assert migrated == []
