"""Plum controlled Tag configuration, synchronization, and read API."""
from __future__ import annotations

import json

import pytest
from fastapi import Response

from app.db import connect
from app.products.plum.infrastructure.tags import (
    DEFAULT_TAG_CONFIG_PATH,
    TagConfigError,
    list_active_creator_tags,
    load_tag_config,
    sync_tag_config,
)


def _write_config(path, tags) -> None:
    path.write_text(
        json.dumps({"version": 1, "tags": tags}),
        encoding="utf-8",
    )


def _tag(tag_id="tag_romance", code="romance", name="Romance", status="active"):
    return {
        "id": tag_id,
        "code": code,
        "display_name": name,
        "status": status,
        "sort_order": 10,
    }


def test_v1_tag_config_contains_twenty_unique_active_tags():
    tags = load_tag_config(DEFAULT_TAG_CONFIG_PATH)

    assert len(tags) == 20
    assert len({tag.id for tag in tags}) == 20
    assert len({tag.code for tag in tags}) == 20
    assert all(tag.status == "active" for tag in tags)


@pytest.mark.parametrize(
    ("tags", "error"),
    [
        ([_tag(), _tag(name="Duplicate")], "tag_id_duplicate"),
        (
            [_tag(), _tag("tag_other", "romance", "Other")],
            "tag_code_duplicate",
        ),
        (
            [_tag(), _tag("tag_other", "other", "romance")],
            "tag_display_name_duplicate",
        ),
        ([_tag(tag_id="romance")], "tag_id_invalid"),
        ([_tag(status="deleted")], "tag_status_invalid"),
    ],
)
def test_tag_config_rejects_unsafe_values(tmp_path, tags, error):
    path = tmp_path / "tags.json"
    _write_config(path, tags)

    with pytest.raises(TagConfigError, match=error):
        load_tag_config(path)


def test_tag_config_requires_integer_version_one(tmp_path):
    path = tmp_path / "tags.json"
    path.write_text(json.dumps({"version": True, "tags": [_tag()]}), encoding="utf-8")

    with pytest.raises(TagConfigError, match="tag_config_version_unsupported"):
        load_tag_config(path)


def test_tag_sync_is_idempotent_updates_mutable_fields_and_keeps_omissions(fresh_db, tmp_path):
    first = tmp_path / "first.json"
    _write_config(
        first,
        [
            _tag(),
            {**_tag("tag_fantasy", "fantasy", "Fantasy"), "sort_order": 20},
        ],
    )
    assert sync_tag_config(first) == {
        "configured": 2,
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
    }
    assert sync_tag_config(first)["unchanged"] == 2

    second = tmp_path / "second.json"
    _write_config(second, [_tag(name="Romantic", status="disabled")])
    result = sync_tag_config(second)

    assert result["updated"] == 1
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, display_name, status FROM plum_tags ORDER BY id"
        ).fetchall()
    assert [(row["id"], row["display_name"], row["status"]) for row in rows] == [
        ("tag_fantasy", "Fantasy", "active"),
        ("tag_romance", "Romantic", "disabled"),
    ]
    assert [tag["id"] for tag in list_active_creator_tags()] == ["tag_fantasy"]


def test_tag_sync_rejects_identity_changes_atomically(fresh_db, tmp_path):
    original = tmp_path / "original.json"
    _write_config(original, [_tag()])
    sync_tag_config(original)

    conflict = tmp_path / "conflict.json"
    _write_config(conflict, [_tag("tag_other", "romance", "Other")])
    with pytest.raises(TagConfigError, match="tag_code_identity_conflict"):
        sync_tag_config(conflict)

    assert [tag["id"] for tag in list_active_creator_tags()] == ["tag_romance"]


def test_creator_tags_route_returns_repository_items(monkeypatch):
    from app.products.plum.api import creation as api

    items = [
        {
            "id": "tag_romance",
            "code": "romance",
            "display_name": "Romance",
            "sort_order": 10,
        }
    ]
    monkeypatch.setattr(api, "list_active_creator_tags", lambda: items)

    result = api.creator_tags(Response(), None)

    assert result == {"status": "ok", "items": items}
    assert any(
        route.path == "/creator/tags" and "GET" in route.methods
        for route in api.router.routes
    )
