"""Pure contracts for Creation draft routes and migration registration."""
from fastapi import FastAPI

from app.db._core import _MIGRATIONS
from app.products.plum.api.creation import router
from app.products.plum.manifest import install_public_routes


def test_creation_draft_routes_are_registered_and_mounted():
    routes = {(route.path, tuple(sorted(route.methods))) for route in router.routes}
    assert ("/creator/works", ("POST",)) in routes
    assert ("/creator/works/{work_id}", ("GET",)) in routes
    assert ("/creator/works/{work_id}", ("PATCH",)) in routes
    assert ("/creator/works/{work_id}", ("DELETE",)) in routes
    assert ("/creator/works/{work_id}/publish", ("POST",)) in routes

    app = FastAPI()
    install_public_routes(app)
    paths = {route.path for route in app.routes}
    assert "/api/v1/products/plum/creator/works" in paths
    assert "/api/v1/products/plum/creator/works/{work_id}/publish" in paths
    assert "/api/v1/products/plum/creator/characters" not in paths


def test_portrait_zoom_schema_migration_is_latest():
    version, migration = _MIGRATIONS[-1]
    assert version == 82
    assert migration.__name__ == "_migration_0082_plum_portrait_zoom"


def test_published_portrait_route_is_registered():
    from app.products.plum.api.media import router as media_router

    assert any(
        route.path == "/characters/{character_id}/portrait" and "GET" in route.methods
        for route in media_router.routes
    )
