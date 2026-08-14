"""Contract tests for the reserved creator TXT import interface."""
import asyncio
from tempfile import SpooledTemporaryFile

import pytest
from fastapi import FastAPI, HTTPException, UploadFile

from app.products.plum.api import imports
from app.products.plum.manifest import install_public_routes


def test_creator_text_import_route_is_reserved():
    assert any(
        route.path == "/creator/imports/text" and "POST" in route.methods
        for route in imports.router.routes
    )


def test_creator_text_import_route_is_mounted_on_public_prefixes():
    app = FastAPI()
    install_public_routes(app)
    paths = {route.path for route in app.routes}

    assert "/api/v1/products/plum/creator/imports/text" in paths
    assert "/v1/products/plum/creator/imports/text" in paths


def test_reserved_text_import_closes_file_without_reading_or_parsing():
    stream = SpooledTemporaryFile()
    stream.write(b"A future character source")
    stream.seek(0)
    upload = UploadFile(filename="character.txt", file=stream)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(imports.reserve_text_import(upload, None))  # type: ignore[arg-type]

    assert caught.value.status_code == 501
    assert caught.value.detail == "creator_text_import_not_implemented"
    assert upload.file.closed
