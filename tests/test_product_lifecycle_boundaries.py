"""朝夕与鸣蝉 lifecycle 所有权边界。"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bootstrap.application import create_app
from app.bootstrap.product_registry import build_test_product_registry
from app.products.mingchan.lifecycle import install_lifecycle as install_mingchan
from app.products.zhaoxi.lifecycle import install_lifecycle as install_zhaoxi


def test_zhaoxi_lifecycle_no_longer_registers_companion_world_hooks():
    app = FastAPI()
    install_zhaoxi(app)
    hook_names = {
        callback.__name__
        for callback in (*app.router.on_startup, *app.router.on_shutdown)
    }
    assert not any("companion_world" in name for name in hook_names)


def test_disabled_mingchan_lifecycle_skips_media_validation(monkeypatch):
    app = FastAPI()
    install_mingchan(
        app,
        registry=build_test_product_registry(mingchan_enabled=False),
    )

    def fail_if_called() -> None:
        raise AssertionError("disabled mingchan must not validate runtime media config")

    monkeypatch.setattr(
        "app.products.mingchan.lifecycle.validate_media_signing_config",
        fail_if_called,
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 404


def test_composition_root_registers_mingchan_lifecycle():
    app = create_app()
    startup_names = {callback.__name__ for callback in app.router.on_startup}
    assert "validate_mingchan_runtime_config" in startup_names


def test_zhaoxi_process_entrypoint_no_longer_composes_world_jobs():
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_proactive_scheduler.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "build_companion_world_memory_sink",
        "compact_companion_world_memory_batch",
        "reclaim_orphan_media_batch",
        "review_pending_media_job",
    ):
        assert forbidden not in script
