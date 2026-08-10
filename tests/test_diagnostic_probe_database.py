"""诊断脚本的 PostgreSQL probe 数据库门控。"""

import pytest


@pytest.mark.parametrize(
    "module_name",
    ["scripts.diagnose_reactivation", "scripts.diagnose_content_invitations"],
)
def test_write_probe_requires_explicit_isolated_database(module_name):
    module = __import__(module_name, fromlist=["_switch_to_probe_database"])

    with pytest.raises(ValueError, match="require --probe-database-url"):
        module._switch_to_probe_database(None, requires_isolation=True)


@pytest.mark.parametrize(
    "module_name",
    ["scripts.diagnose_reactivation", "scripts.diagnose_content_invitations"],
)
def test_probe_database_must_be_postgres_and_must_differ_from_app(monkeypatch, module_name):
    module = __import__(module_name, fromlist=["_switch_to_probe_database"])
    monkeypatch.setattr(module.settings, "database_url", "postgresql://app/current")

    with pytest.raises(ValueError, match="must be a PostgreSQL"):
        module._switch_to_probe_database("sqlite:///tmp/probe.db", requires_isolation=True)
    with pytest.raises(ValueError, match="must differ"):
        module._switch_to_probe_database("postgresql://app/current", requires_isolation=True)
