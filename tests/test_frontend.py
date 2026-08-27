"""Tests for Pixel Vault frontend routes and static asset delivery."""

import os
import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient

from main import STATIC_DIR


@pytest.fixture
def client():
    """Fixture providing a TestClient for testing frontend static routes without acquiring production disk locks."""
    test_app = FastAPI()
    if os.path.isdir(STATIC_DIR):
        test_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @test_app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_frontend():
        index_file = os.path.join(STATIC_DIR, "index.html")
        if os.path.isfile(index_file):
            return FileResponse(index_file)
        return {"message": "Frontend not found"}

    return TestClient(test_app)


def test_serve_frontend_root(client):
    """GET / should serve the SPA index.html."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Pixel Vault" in response.text
    assert "main-navbar" in response.text
    assert "lightbox-modal" in response.text


def test_serve_static_css(client):
    """GET /static/css/style.css should serve the CSS stylesheet."""
    response = client.get("/static/css/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers.get("content-type", "")
    assert "--bg-base" in response.text


def test_serve_static_js(client):
    """GET /static/js/app.js should serve the Javascript application."""
    response = client.get("/static/js/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers.get("content-type", "").lower()
    assert "class GalleryManager" in response.text
