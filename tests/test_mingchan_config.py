"""鸣蝉配置命名与旧 Companion World 环境变量兼容测试。"""

from app.config import Settings


def test_mingchan_environment_name_has_priority_over_legacy_alias(monkeypatch):
    """新旧变量并存时只以 MINGCHAN_* 为配置真值。"""

    monkeypatch.setenv("MINGCHAN_FEED_ENABLED", "true")
    monkeypatch.setenv("COMPANION_WORLD_FEED_ENABLED", "false")

    config = Settings(_env_file=None)

    assert config.mingchan_feed_enabled is True
    assert config.companion_world_feed_enabled is True


def test_legacy_environment_name_remains_read_only_compatible(monkeypatch):
    """发布窗口内仅配置旧变量时仍能初始化同一个鸣蝉字段。"""

    monkeypatch.delenv("MINGCHAN_MAILBOX_ENABLED", raising=False)
    monkeypatch.setenv("COMPANION_WORLD_MAILBOX_ENABLED", "true")

    config = Settings(_env_file=None)

    assert config.mingchan_mailbox_enabled is True
    assert config.companion_world_mailbox_enabled is True


def test_legacy_runtime_attribute_mutates_mingchan_field_only():
    """旧测试注入不会形成第二套独立配置状态。"""

    config = Settings(_env_file=None, mingchan_visits_enabled=False)

    config.companion_world_visits_enabled = True

    assert config.mingchan_visits_enabled is True
    assert config.companion_world_visits_enabled is True
