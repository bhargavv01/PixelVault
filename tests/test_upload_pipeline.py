"""Integration tests for the Photo Upload & Retrieval Pipeline.

Tests the full HTTP round-trip through FastAPI's TestClient:
    POST /photos → GET /photos/{id} → GET /photos/{id}/file → GET /photos

All tests use isolated temporary directories — never touch /storage/.
Test images are generated programmatically (minimal valid JPEG/PNG headers).
"""

from __future__ import annotations

import io
import os
import struct
import zlib
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.storage.index import IndexStore
from app.storage.log_writer import LogWriter


# ── Minimal valid image generators ───────────────────────────────────


def _make_jpeg_bytes(pixel_value: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    """Create a minimal valid JPEG file in memory.

    Generates a tiny 1x1 pixel JPEG using Pillow.
    Vary pixel_value to produce distinct hashes.
    """
    from PIL import Image

    buf = io.BytesIO()
    img = Image.new("RGB", (1, 1), pixel_value)
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png_bytes() -> bytes:
    """Create a minimal valid PNG file in memory.

    Builds a 1x1 white pixel PNG from scratch using the PNG spec:
    signature + IHDR + IDAT + IEND chunks.
    """
    def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        """Build a PNG chunk: length + type + data + CRC32."""
        chunk = chunk_type + data
        return (
            struct.pack(">I", len(data))
            + chunk
            + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"

    # IHDR: 1x1, 8-bit RGB, no interlace
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = _png_chunk(b"IHDR", ihdr_data)

    # IDAT: single row, filter byte 0, then R G B = 255 255 255
    raw_data = b"\x00\xff\xff\xff"
    compressed = zlib.compress(raw_data)
    idat = _png_chunk(b"IDAT", compressed)

    # IEND
    iend = _png_chunk(b"IEND", b"")

    return signature + ihdr + idat + iend


def _make_text_bytes() -> bytes:
    """Create a plain text file (not an image) for rejection tests."""
    return b"This is not an image file. Just plain text content."


# ── Test fixtures ────────────────────────────────────────────────────


@pytest.fixture()
def storage_dirs(tmp_path):
    """Create isolated storage directories for a single test.

    Returns a dict with paths to blobs/, logs/, tmp/, thumbs/.
    """
    blobs = tmp_path / "blobs"
    logs = tmp_path / "logs"
    tmp = tmp_path / "tmp"
    thumbs = tmp_path / "thumbs"

    blobs.mkdir()
    logs.mkdir()
    tmp.mkdir()
    thumbs.mkdir()

    return {
        "blobs": str(blobs),
        "logs": str(logs),
        "tmp": str(tmp),
        "thumbs": str(thumbs),
    }


@pytest.fixture()
def test_client(storage_dirs):
    """Create a TestClient with fully isolated storage.

    Each test gets a FRESH FastAPI app (no production lifespan) with its
    own IndexStore and LogWriter pointing at temp dirs.  This avoids the
    production lifespan which would rebuild the index from /storage/logs/.
    """
    from fastapi import FastAPI
    from app.routes.photos import router as photos_router

    log_dir = storage_dirs["logs"]
    blob_dir = storage_dirs["blobs"]
    tmp_dir = storage_dirs["tmp"]

    # Build fresh index and writer per test
    index = IndexStore()
    writer = LogWriter(log_dir=log_dir)

    # Create a fresh app per test — no lifespan, no /storage/ dependency
    test_app = FastAPI()
    test_app.include_router(photos_router)
    test_app.state.index = index
    test_app.state.log_writer = writer

    # Patch directory constants in both modules that reference them
    with (
        patch("app.services.upload.BLOB_DIR", blob_dir),
        patch("app.services.upload.TMP_DIR", tmp_dir),
        patch("app.routes.photos.BLOB_DIR", blob_dir),
    ):
        with TestClient(test_app, raise_server_exceptions=False) as client:
            yield client

        writer.close()


# ── Upload Tests ─────────────────────────────────────────────────────


class TestUploadJPEG:
    """Test JPEG upload flow."""

    def test_upload_jpeg_success(self, test_client):
        """Upload a valid JPEG → 201 Created with metadata."""
        jpeg_bytes = _make_jpeg_bytes()
        response = test_client.post(
            "/photos",
            files={"file": ("test.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )

        assert response.status_code == 201
        data = response.json()
        assert "photo_id" in data
        assert "content_hash" in data
        assert len(data["content_hash"]) == 64  # SHA-256 hex
        assert data["uploaded_at"] is not None


class TestUploadPNG:
    """Test PNG upload flow."""

    def test_upload_png_success(self, test_client):
        """Upload a valid PNG → 201 Created with metadata."""
        png_bytes = _make_png_bytes()
        response = test_client.post(
            "/photos",
            files={"file": ("test.png", io.BytesIO(png_bytes), "image/png")},
        )

        assert response.status_code == 201
        data = response.json()
        assert "photo_id" in data
        assert len(data["content_hash"]) == 64


class TestDeduplication:
    """Test upload deduplication via content hash."""

    def test_upload_duplicate_returns_200(self, test_client):
        """Upload the same file twice → first 201, second 200 with same photo_id."""
        jpeg_bytes = _make_jpeg_bytes()

        # First upload
        resp1 = test_client.post(
            "/photos",
            files={"file": ("photo.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )
        assert resp1.status_code == 201
        photo_id_1 = resp1.json()["photo_id"]

        # Second upload — same bytes
        resp2 = test_client.post(
            "/photos",
            files={"file": ("photo_copy.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )
        assert resp2.status_code == 200
        photo_id_2 = resp2.json()["photo_id"]

        # Same photo — dedup detected
        assert photo_id_1 == photo_id_2


class TestUploadRejection:
    """Test invalid file upload handling."""

    def test_upload_invalid_file_rejected(self, test_client):
        """Upload a non-image file → 422 Unprocessable Entity."""
        text_bytes = _make_text_bytes()
        response = test_client.post(
            "/photos",
            files={"file": ("notes.txt", io.BytesIO(text_bytes), "text/plain")},
        )

        assert response.status_code == 422
        assert "Unsupported file type" in response.json()["detail"]


# ── Metadata Retrieval Tests ─────────────────────────────────────────


class TestGetMetadata:
    """Test GET /photos/{photo_id} endpoint."""

    def test_get_metadata(self, test_client):
        """Upload then retrieve metadata → 200 with correct data."""
        jpeg_bytes = _make_jpeg_bytes()
        upload_resp = test_client.post(
            "/photos",
            files={"file": ("test.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )
        photo_id = upload_resp.json()["photo_id"]

        # Retrieve metadata
        meta_resp = test_client.get(f"/photos/{photo_id}")
        assert meta_resp.status_code == 200

        data = meta_resp.json()
        assert data["photo_id"] == photo_id
        assert data["content_hash"] == upload_resp.json()["content_hash"]

    def test_get_metadata_not_found(self, test_client):
        """GET metadata for non-existent photo → 404."""
        response = test_client.get("/photos/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404


# ── Binary Delivery Tests ────────────────────────────────────────────


class TestGetFile:
    """Test GET /photos/{photo_id}/file endpoint."""

    def test_get_file(self, test_client):
        """Upload then download file → 200 with identical binary content."""
        jpeg_bytes = _make_jpeg_bytes()
        upload_resp = test_client.post(
            "/photos",
            files={"file": ("test.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
        )
        photo_id = upload_resp.json()["photo_id"]

        # Download the file
        file_resp = test_client.get(f"/photos/{photo_id}/file")
        assert file_resp.status_code == 200
        assert file_resp.headers["content-type"] == "image/jpeg"

        # Binary content must match exactly
        assert file_resp.content == jpeg_bytes

    def test_get_file_not_found(self, test_client):
        """GET file for non-existent photo → 404."""
        response = test_client.get(
            "/photos/00000000-0000-0000-0000-000000000000/file"
        )
        assert response.status_code == 404


# ── List / Pagination Tests ──────────────────────────────────────────


class TestListPhotos:
    """Test GET /photos listing endpoint."""

    def test_list_photos_empty(self, test_client):
        """GET /photos with no uploads → empty list."""
        response = test_client.get("/photos")
        assert response.status_code == 200
        data = response.json()
        assert data["photos"] == []
        assert data["total"] == 0

    def test_list_photos_pagination(self, test_client):
        """Upload N photos, paginate with offset/limit."""
        # Upload 3 distinct images (vary a pixel to get different hashes)
        photo_ids = []
        for i in range(3):
            jpeg_bytes = _make_jpeg_bytes(pixel_value=(i * 50, i * 50, i * 50))
            resp = test_client.post(
                "/photos",
                files={"file": (f"img_{i}.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")},
            )
            assert resp.status_code == 201
            photo_ids.append(resp.json()["photo_id"])

        # Full list
        full_resp = test_client.get("/photos")
        assert full_resp.json()["total"] == 3

        # Page: offset=0, limit=2
        page_resp = test_client.get("/photos?offset=0&limit=2")
        page_data = page_resp.json()
        assert len(page_data["photos"]) == 2
        assert page_data["total"] == 3

        # Page: offset=2, limit=2 — should get 1 remaining
        page_resp2 = test_client.get("/photos?offset=2&limit=2")
        page_data2 = page_resp2.json()
        assert len(page_data2["photos"]) == 1


# ── Cleanup Tests ────────────────────────────────────────────────────


class TestTempCleanup:
    """Test that temp files are cleaned up on failure."""

    def test_temp_file_cleanup_on_invalid_upload(self, test_client, storage_dirs):
        """Upload an invalid file → temp file must not remain in /storage/tmp/."""
        text_bytes = _make_text_bytes()
        test_client.post(
            "/photos",
            files={"file": ("bad.txt", io.BytesIO(text_bytes), "text/plain")},
        )

        # /storage/tmp/ should be empty — temp file cleaned up by finally block
        tmp_dir = storage_dirs["tmp"]
        remaining = os.listdir(tmp_dir)
        assert remaining == [], f"Orphaned temp files: {remaining}"


# ── EXIF Tests ───────────────────────────────────────────────────────


class TestExifExtraction:
    """Test that EXIF metadata is extracted from uploaded images."""

    def test_exif_extraction_from_jpeg(self, test_client):
        """Upload a JPEG with EXIF data → metadata has taken_at and camera."""
        from PIL import Image
        import PIL.ExifTags

        # Build a JPEG with EXIF using Pillow's native exif support
        img = Image.new("RGB", (1, 1), (128, 128, 128))
        exif = img.getexif()

        # Tag 272 = Model, Tag 36867 = DateTimeOriginal
        exif[272] = "TestCamera X100"
        exif[36867] = "2024:06:15 14:30:00"

        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes())
        jpeg_with_exif = buf.getvalue()

        resp = test_client.post(
            "/photos",
            files={"file": ("exif_test.jpg", io.BytesIO(jpeg_with_exif), "image/jpeg")},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["camera"] == "TestCamera X100"
        assert data["taken_at"] is not None
        assert "2024" in data["taken_at"]
