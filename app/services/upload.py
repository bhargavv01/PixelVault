"""Photo upload pipeline — the write path.

Implements the architecture spec's write flow (steps 1–6):
    stream & hash → validate → dedup check → CAS commit → EXIF → log → index

This entire function is synchronous I/O.  The FastAPI route handler wraps
the call in ``asyncio.to_thread()`` to keep the event loop free.

CRITICAL ORDERING (storage-engine invariant #3):
    1. Write blob to temp file
    2. Hash
    3. Check for duplicate
    4. Atomic rename into blob store
    5. Append metadata record to log
    6. Update in-memory index

Never update the index before the log append has succeeded.
Never reverse blob-write and log-append.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import IO

from app.services.exif import extract_exif
from app.storage.index import IndexStore
from app.storage.log_writer import LogWriter
from app.storage.models import PhotoMeta

logger = logging.getLogger("pixel_vault.services.upload")

# Directories
BLOB_DIR = "/storage/blobs"
TMP_DIR = "/storage/tmp"

# Streaming chunk size: 1 MB (protects 4 GB RAM)
CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB

# Magic byte signatures for supported formats
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _validate_magic_bytes(file_path: str) -> str:
    """Read the first 8 bytes and verify JPEG or PNG signature.

    Args:
        file_path: Path to the file to validate.

    Returns:
        Detected media type string: ``"image/jpeg"`` or ``"image/png"``.

    Raises:
        ValueError: If the file is not a recognised image format.
    """
    with open(file_path, "rb") as f:
        header = f.read(8)

    if header[:3] == _JPEG_MAGIC:
        return "image/jpeg"
    if header[:8] == _PNG_MAGIC:
        return "image/png"

    raise ValueError(
        "Unsupported file type. Only JPEG and PNG images are accepted."
    )


def process_upload(
    file_reader: IO[bytes],
    index: IndexStore,
    log_writer: LogWriter,
) -> tuple[PhotoMeta, bool]:
    """Execute the full upload pipeline.

    This function is the SINGLE code path for writing photos into the
    system.  It enforces the strict ordering mandated by the storage
    engine invariants.

    Args:
        file_reader: A readable binary stream containing the uploaded
            file data (e.g. ``BytesIO`` from ``UploadFile.read()``).
        index: The in-memory ``IndexStore`` (``app.state.index``).
        log_writer: The single ``LogWriter`` (``app.state.log_writer``).

    Returns:
        A ``(PhotoMeta, is_new)`` tuple.  ``is_new`` is ``True`` for a
        brand-new upload and ``False`` for a deduplication hit.

    Raises:
        ValueError: If the uploaded file is not JPEG or PNG.
        OSError: If disk I/O fails (temp write, rename, etc.).
    """
    tmp_id = str(uuid.uuid4())
    tmp_path = os.path.join(TMP_DIR, f"{tmp_id}.tmp")

    try:
        # ── Step 1: Stream to temp file & compute SHA-256 ────────────
        sha256 = hashlib.sha256()

        with open(tmp_path, "wb") as tmp_file:
            while True:
                chunk = file_reader.read(CHUNK_SIZE)
                if not chunk:
                    break
                tmp_file.write(chunk)
                sha256.update(chunk)

        content_hash = sha256.hexdigest()

        logger.debug(
            "Streamed upload to %s, SHA-256=%s",
            tmp_path,
            content_hash,
        )

        # ── Step 2: Magic bytes validation ───────────────────────────
        # Raises ValueError if not JPEG/PNG — caught by finally for cleanup
        media_type = _validate_magic_bytes(tmp_path)

        logger.debug("Validated file type: %s", media_type)

        # ── Step 3: Dedup check ──────────────────────────────────────
        if index.contains_hash(content_hash):
            existing_meta = index.get_by_hash(content_hash)
            logger.info(
                "Duplicate detected: hash=%s, existing photo_id=%s",
                content_hash,
                existing_meta.photo_id if existing_meta else "?",
            )
            # Temp file cleaned up in finally block
            return (existing_meta, False)

        # ── Step 4: CAS commit (atomic rename) ──────────────────────
        blob_path = os.path.join(BLOB_DIR, content_hash)
        os.rename(tmp_path, blob_path)

        logger.info(
            "CAS commit: %s → %s",
            tmp_path,
            blob_path,
        )

        # After rename, tmp_path no longer exists — record that so
        # the finally block doesn't try to delete a non-existent file.
        tmp_path_moved = True

        # ── Step 5: EXIF extraction ─────────────────────────────────
        exif_data = extract_exif(blob_path)

        # ── Step 6: Build PhotoMeta ─────────────────────────────────
        photo_id = str(uuid.uuid4())
        meta = PhotoMeta(
            photo_id=photo_id,
            content_hash=content_hash,
            taken_at=exif_data["taken_at"],
            camera=exif_data["camera"],
            gps=exif_data["gps"],
            uploaded_at=datetime.now(timezone.utc),
        )

        # ── Step 7: Log append ──────────────────────────────────────
        # Returns meta with segment_id, offset, length populated
        updated_meta = log_writer.append(meta)

        # ── Step 8: Index update ────────────────────────────────────
        # ONLY after log append succeeds (invariant #3)
        index.put(updated_meta)

        logger.info(
            "Upload complete: photo_id=%s, hash=%s, segment=%s",
            photo_id,
            content_hash,
            updated_meta.segment_id,
        )

        return (updated_meta, True)

    finally:
        # Deterministic cleanup: delete temp file if it still exists.
        # After a successful CAS rename (step 4), the file has moved
        # to /storage/blobs/ and this is a no-op.
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.debug("Cleaned up temp file: %s", tmp_path)
            except OSError as exc:
                logger.warning(
                    "Failed to clean up temp file %s: %s", tmp_path, exc
                )
