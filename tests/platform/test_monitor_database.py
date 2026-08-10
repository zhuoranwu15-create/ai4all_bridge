from scripts import monitor_health


def test_database_check_flags_connection_lag_and_growth(monkeypatch):
    monkeypatch.setattr(
        monitor_health,
        "get_database_storage_stats",
        lambda: {
            "db_bytes": 150,
            "connection_usage_percent": 91,
            "replication_lag_seconds": 75,
            "replay_lag_seconds": None,
        },
    )
    error = monitor_health._check_database(
        state={"database_db_bytes": 100},
        max_connection_percent=85,
        max_replication_lag_seconds=60,
        max_growth_percent=20,
    )
    assert "connections=91.0%" in error
    assert "replication_lag=75.0s" in error
    assert "growth=50.0%" in error


def test_database_check_is_fail_safe(monkeypatch):
    monkeypatch.setattr(
        monitor_health,
        "get_database_storage_stats",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert monitor_health._check_database(
        state={}, max_connection_percent=85, max_replication_lag_seconds=60, max_growth_percent=20
    ) == "database: metrics unavailable: db down"
