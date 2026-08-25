"""Unit tests for the thumbnail generation service.

Tests the ``generate_thumbnail()`` function in isolation — verifies
output file creation, dimensions, format conversion, aspect ratio
preservation, and return value format.

All tests use ``tmp_path`` fixtures — never touch ``/storage/``.
Test images are generated programmatically via Pillow.
"""

from __future__ import annotations

import os

import pytest
from PIL import Image

from app.services.thumbnail import generate_thumbnail


# ── Helpers ──────────────────────────────────────────────────────────


def _create_test_jpeg(path: str, width: int, height: int) -> None:
    """Create a test JPEG image at the given path."""
    img = Image.new("RGB", (width, height), (100, 150, 200))
    img.save(path, format="JPEG", quality=95)


def _create_test_png(path: str, width: int, height: int, mode: str = "RGBA") -> None:
    """Create a test PNG image at the given path (with alpha by default)."""
    img = Image.new(mode, (width, height), (100, 150, 200, 128) if mode == "RGBA" else (100, 150, 200))
    img.save(path, format="PNG")


# ── Tests ────────────────────────────────────────────────────────────


class TestGenerateThumbnail:
    """Unit tests for generate_thumbnail()."""

    def test_generate_thumbnail_jpeg(self, tmp_path):
        """Pass a valid JPEG → thumbnail file created, is a valid JPEG, dimensions ≤ 400px."""
        blob_path = str(tmp_path / "blob")
        thumbs_dir = str(tmp_path / "thumbs")
        os.makedirs(thumbs_dir)

        _create_test_jpeg(blob_path, 800, 600)
        content_hash = "abc123def456"

        result = generate_thumbnail(blob_path, thumbs_dir, content_hash)

        # Thumbnail file exists
        expected_file = os.path.join(thumbs_dir, f"{content_hash}_400.jpg")
        assert os.path.isfile(expected_file)

        # Is a valid JPEG with correct dimensions
        with Image.open(expected_file) as thumb:
            assert thumb.format == "JPEG"
            assert max(thumb.size) <= 400

    def test_generate_thumbnail_png_to_jpeg(self, tmp_path):
        """Pass a valid RGBA PNG → thumbnail is JPEG (format conversion works)."""
        blob_path = str(tmp_path / "blob")
        thumbs_dir = str(tmp_path / "thumbs")
        os.makedirs(thumbs_dir)

        _create_test_png(blob_path, 500, 500, mode="RGBA")
        content_hash = "png_hash_test"

        result = generate_thumbnail(blob_path, thumbs_dir, content_hash)

        expected_file = os.path.join(thumbs_dir, f"{content_hash}_400.jpg")
        assert os.path.isfile(expected_file)

        with Image.open(expected_file) as thumb:
            # Must be JPEG, not PNG — alpha channel converted to RGB
            assert thumb.format == "JPEG"
            assert thumb.mode == "RGB"

    def test_generate_thumbnail_preserves_aspect_ratio(self, tmp_path):
        """Pass a 1000×500 image → thumbnail is 400×200 (aspect preserved)."""
        blob_path = str(tmp_path / "blob")
        thumbs_dir = str(tmp_path / "thumbs")
        os.makedirs(thumbs_dir)

        _create_test_jpeg(blob_path, 1000, 500)
        content_hash = "aspect_ratio_test"

        generate_thumbnail(blob_path, thumbs_dir, content_hash)

        expected_file = os.path.join(thumbs_dir, f"{content_hash}_400.jpg")
        with Image.open(expected_file) as thumb:
            w, h = thumb.size
            # Long edge should be 400, short edge should be 200 (2:1 ratio)
            assert w == 400
            assert h == 200

    def test_generate_thumbnail_returns_relative_path(self, tmp_path):
        """Return value is 'thumbs/<hash>_400.jpg' (relative, not absolute)."""
        blob_path = str(tmp_path / "blob")
        thumbs_dir = str(tmp_path / "thumbs")
        os.makedirs(thumbs_dir)

        _create_test_jpeg(blob_path, 200, 200)
        content_hash = "relative_path_test"

        result = generate_thumbnail(blob_path, thumbs_dir, content_hash)

        assert result == f"thumbs/{content_hash}_400.jpg"
        # Must NOT be an absolute path
        assert not os.path.isabs(result)
