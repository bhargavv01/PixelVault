"""Background thumbnail generation service.

Generates web-friendly JPEG thumbnails (400px long edge) from uploaded
photo blobs and persists the thumbnail path back into the Bitcask log +
in-memory index.

This module respects the storage-engine single-writer invariant (#2):
    "Background workers must call back into that same write path rather
     than opening the log file directly."

The async orchestrator ``process_thumbnail_task()`` is designed to be
enqueued via FastAPI's ``BackgroundTasks`` — it runs after the upload
response is sent, in the same process, using the same ``LogWriter``
and ``IndexStore`` instances from ``app.state``.
"""

from __future__ import annotations

import asyncio
import logging
import os

from PIL import Image

from app.storage.index import IndexStore
from app.storage.log_writer import LogWriter

logger = logging.getLogger("pixel_vault.services.thumbnail")

# Directories
BLOB_DIR = "/storage/blobs"
THUMBS_DIR = "/storage/thumbs"
STORAGE_ROOT = "/storage"

# Thumbnail configuration
THUMB_MAX_SIZE = (400, 400)   # Fit within 400×400, preserving aspect ratio
THUMB_QUALITY = 80            # JPEG quality (1–95)
THUMB_SUFFIX = "_400.jpg"     # Size-tagged filename suffix


def generate_thumbnail(
    blob_path: str,
    thumbs_dir: str,
    content_hash: str,
) -> str:
    """Generate a JPEG thumbnail from an image blob.

    Uses ``Image.thumbnail()`` with ``reducing_gap=2.0`` to leverage
    Pillow's draft-mode downscaling — loads a pre-downscaled version
    of the image to reduce peak RAM usage (≈10–30 MB for a 20 MP JPEG
    instead of ≈60–80 MB for full decode).  Safe for the 4 GB RAM target.

    Args:
        blob_path: Absolute path to the original image blob.
        thumbs_dir: Absolute path to the thumbnail output directory.
        content_hash: SHA-256 hex digest of the blob (used for naming).

    Returns:
        Relative path to the generated thumbnail, e.g.
        ``"thumbs/<content_hash>_400.jpg"``.  Relative to ``/storage/``
        so it's portable in the append-only log.

    Raises:
        OSError: If the blob can't be read or the thumbnail can't be written.
        PIL.UnidentifiedImageError: If the blob is not a valid image.
    """
    thumb_filename = f"{content_hash}{THUMB_SUFFIX}"
    thumb_abs_path = os.path.join(thumbs_dir, thumb_filename)

    with Image.open(blob_path) as img:
        # Convert to RGB if necessary (e.g., RGBA PNGs, palette mode)
        # JPEG doesn't support alpha channels.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Resize in-place using draft-mode downscaling for memory safety.
        # thumbnail() modifies the image in-place and preserves aspect ratio.
        img.thumbnail(THUMB_MAX_SIZE, Image.LANCZOS, reducing_gap=2.0)

        # Save as JPEG
        img.save(thumb_abs_path, format="JPEG", quality=THUMB_QUALITY)

    # Return relative path (relative to /storage/) for log portability
    thumb_relative = f"thumbs/{thumb_filename}"

    logger.info(
        "Thumbnail generated: %s (%dx%d)",
        thumb_relative,
        img.size[0],
        img.size[1],
    )

    return thumb_relative


async def process_thumbnail_task(
    photo_id: str,
    content_hash: str,
    index: IndexStore,
    log_writer: LogWriter,
) -> None:
    """Async orchestrator for background thumbnail generation.

    Designed to be called via ``BackgroundTasks.add_task()`` after an
    upload response is sent.  All blocking I/O is offloaded to threads.

    Flow:
        1. Generate thumbnail via Pillow (in thread)
        2. Re-read current metadata from index (latest version)
        3. Append updated record with ``thumbnail_paths`` to log (in thread)
        4. Update in-memory index

    This follows the Bitcask update pattern: a new complete record for the
    same ``photo_id`` supersedes the previous one.  On startup log replay,
    the later record wins via ``IndexStore.put()``.

    Args:
        photo_id: The photo's UUID to generate a thumbnail for.
        content_hash: SHA-256 hex digest for blob lookup and thumb naming.
        index: The shared in-memory ``IndexStore`` (``app.state.index``).
        log_writer: The single ``LogWriter`` (``app.state.log_writer``).
    """
    blob_path = os.path.join(BLOB_DIR, content_hash)

    try:
        # ── Step 1: Generate thumbnail (I/O-heavy, offload to thread) ──
        thumb_relative = await asyncio.to_thread(
            generate_thumbnail, blob_path, THUMBS_DIR, content_hash
        )

        # ── Step 2: Re-read current metadata (get latest version) ──────
        current_meta = index.get(photo_id)
        if current_meta is None:
            logger.error(
                "Photo %s disappeared from index before thumbnail update.",
                photo_id,
            )
            return

        # ── Step 3: Build updated record with thumbnail_paths ──────────
        # Clear bookkeeping fields — they'll be repopulated by log_writer.append()
        updated_meta = current_meta.model_copy(
            update={
                "thumbnail_paths": [thumb_relative],
                "segment_id": "",
                "offset": 0,
                "length": 0,
            }
        )

        # ── Step 4: Append to log (in thread — sync I/O) ──────────────
        # This is the SAME write path as upload — invariant #2 satisfied.
        appended_meta = await asyncio.to_thread(log_writer.append, updated_meta)

        # ── Step 5: Update in-memory index ─────────────────────────────
        # ONLY after log append succeeds (invariant #3).
        index.put(appended_meta)

        logger.info(
            "Thumbnail metadata persisted: photo_id=%s, thumb=%s, segment=%s",
            photo_id,
            thumb_relative,
            appended_meta.segment_id,
        )

    except Exception as exc:
        # Never crash the server for a thumbnail failure.
        # The photo remains fully usable — thumbnail_paths stays [].
        logger.warning(
            "Thumbnail generation failed for photo_id=%s, hash=%s: %s",
            photo_id,
            content_hash,
            exc,
        )
