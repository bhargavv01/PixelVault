"""Photo API routes — upload, retrieval, listing, deletion.

Thin route handlers that delegate business logic to the service layer
and storage engine.  All heavy I/O is offloaded to a thread via
``asyncio.to_thread()`` to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.services.thumbnail import STORAGE_ROOT, process_thumbnail_task
from app.services.upload import BLOB_DIR, process_upload
from app.storage.models import PhotoMeta

logger = logging.getLogger("pixel_vault.routes.photos")

router = APIRouter(tags=["photos"])

# Magic byte signatures for Content-Type inference on delivery
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _infer_media_type(blob_path: str) -> str:
    """Read the first 8 bytes of a blob to determine its media type.

    Falls back to ``application/octet-stream`` if detection fails.
    """
    try:
        with open(blob_path, "rb") as f:
            header = f.read(8)
        if header[:3] == _JPEG_MAGIC:
            return "image/jpeg"
        if header[:8] == _PNG_MAGIC:
            return "image/png"
    except OSError:
        pass
    return "application/octet-stream"


# ── POST /photos — Upload ────────────────────────────────────────────


@router.post("/photos", status_code=201)
async def upload_photo(
    request: Request,
    file: UploadFile,
    background_tasks: BackgroundTasks,
):
    """Upload a photo via multipart/form-data.

    The file is streamed to a temp location, hashed, deduplicated,
    committed to the blob store, and indexed — all in a single
    background thread.  On success, a background task is enqueued
    to generate a thumbnail and persist its path to the log.

    Returns:
        201 Created + PhotoMeta JSON for new uploads.
        200 OK + PhotoMeta JSON for duplicate uploads.
        422 Unprocessable Entity for non-JPEG/PNG files.
    """
    # Read the full file content into memory via UploadFile.
    # UploadFile already spills to disk after 1MB, so this is safe.
    # We then wrap it in BytesIO for the synchronous pipeline.
    contents = await file.read()
    file_reader = io.BytesIO(contents)

    index = request.app.state.index
    log_writer = request.app.state.log_writer

    try:
        meta, is_new = await asyncio.to_thread(
            process_upload, file_reader, index, log_writer
        )
    except ValueError as exc:
        # Magic bytes validation failed — not a JPEG/PNG
        raise HTTPException(status_code=422, detail=str(exc))

    if is_new:
        # Enqueue background thumbnail generation (runs after response)
        background_tasks.add_task(
            process_thumbnail_task,
            meta.photo_id,
            meta.content_hash,
            index,
            log_writer,
        )
        return meta.model_dump(mode="json")

    # Duplicate — override the default 201 with 200
    from fastapi.responses import JSONResponse

    return JSONResponse(
        content=meta.model_dump(mode="json"),
        status_code=200,
    )


# ── GET /photos/{photo_id} — Metadata Retrieval ─────────────────────


@router.get("/photos/{photo_id}")
async def get_photo_metadata(request: Request, photo_id: str):
    """Look up a photo's metadata by ID.

    Pure in-memory index lookup — zero disk I/O.

    Returns:
        200 OK + PhotoMeta JSON.
        404 Not Found if photo_id doesn't exist.
    """
    index = request.app.state.index
    meta = index.get(photo_id)

    if meta is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    return meta.model_dump(mode="json")


# ── GET /photos/{photo_id}/file — Binary Delivery ───────────────────


@router.get("/photos/{photo_id}/file")
async def get_photo_file(request: Request, photo_id: str):
    """Serve the original image binary for a photo.

    Looks up the content hash via the in-memory index, then serves
    the blob from ``/storage/blobs/<hash>`` using ``FileResponse``.

    Returns:
        200 OK + image binary with correct Content-Type.
        404 Not Found if photo_id doesn't exist or blob is missing.
    """
    index = request.app.state.index
    meta = index.get(photo_id)

    if meta is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    blob_path = os.path.join(BLOB_DIR, meta.content_hash)

    # Defensive check — blob should always exist if index entry exists
    if not os.path.isfile(blob_path):
        logger.error(
            "Blob missing for photo_id=%s, hash=%s",
            photo_id,
            meta.content_hash,
        )
        raise HTTPException(
            status_code=404,
            detail="Photo file not found on disk.",
        )

    media_type = _infer_media_type(blob_path)

    return FileResponse(
        path=blob_path,
        media_type=media_type,
        filename=f"{photo_id}.{'jpg' if media_type == 'image/jpeg' else 'png'}",
    )


# ── GET /photos — List with Pagination ──────────────────────────────


@router.get("/photos")
async def list_photos(
    request: Request,
    offset: int = 0,
    limit: int = 50,
):
    """List all photos with basic offset/limit pagination.

    Returns photos from the in-memory index — no disk I/O.

    Query params:
        offset: Starting index (default 0).
        limit: Maximum number of results (default 50, max 200).

    Returns:
        200 OK with ``{"photos": [...], "total": N, "offset": M, "limit": L}``.
    """
    # Clamp limit to prevent excessive response sizes
    limit = min(limit, 200)
    if offset < 0:
        offset = 0

    index = request.app.state.index
    all_photos = index.all_photos
    total = len(all_photos)

    page = all_photos[offset : offset + limit]

    return {
        "photos": [p.model_dump(mode="json") for p in page],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


# ── GET /photos/{photo_id}/thumbnail — Thumbnail Delivery ───────────


@router.get("/photos/{photo_id}/thumbnail")
async def get_photo_thumbnail(request: Request, photo_id: str):
    """Serve the thumbnail image for a photo.

    Looks up ``thumbnail_paths[0]`` from the in-memory index and
    resolves it to an absolute path under ``/storage/``.

    Returns:
        200 OK + JPEG thumbnail binary.
        404 Not Found if photo doesn't exist or thumbnail not yet generated.
    """
    index = request.app.state.index
    meta = index.get(photo_id)

    if meta is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    if not meta.thumbnail_paths:
        raise HTTPException(
            status_code=404,
            detail="Thumbnail not yet generated.",
        )

    # Resolve relative path (e.g. "thumbs/<hash>_400.jpg") to absolute
    thumb_abs_path = os.path.join(STORAGE_ROOT, meta.thumbnail_paths[0])

    # Defensive check — file should exist if thumbnail_paths is populated
    if not os.path.isfile(thumb_abs_path):
        logger.error(
            "Thumbnail file missing for photo_id=%s, path=%s",
            photo_id,
            thumb_abs_path,
        )
        raise HTTPException(
            status_code=404,
            detail="Thumbnail file not found on disk.",
        )

    return FileResponse(
        path=thumb_abs_path,
        media_type="image/jpeg",
        filename=f"{photo_id}_thumb.jpg",
    )


# ── DELETE /photos/{photo_id} — Soft-Delete ─────────────────────────


def _delete_files_from_disk(content_hash: str, thumbnail_paths: list[str]) -> None:
    """Remove the blob and thumbnail files from disk.

    Best-effort: logs warnings on failure but never raises.
    Called in a background thread to avoid blocking the event loop.
    """
    # Delete the blob
    blob_path = os.path.join(BLOB_DIR, content_hash)
    try:
        if os.path.isfile(blob_path):
            os.remove(blob_path)
            logger.info("Deleted blob: %s", blob_path)
    except OSError as exc:
        logger.warning("Failed to delete blob %s: %s", blob_path, exc)

    # Delete thumbnails
    for thumb_rel in thumbnail_paths:
        thumb_abs = os.path.join(STORAGE_ROOT, thumb_rel)
        try:
            if os.path.isfile(thumb_abs):
                os.remove(thumb_abs)
                logger.info("Deleted thumbnail: %s", thumb_abs)
        except OSError as exc:
            logger.warning("Failed to delete thumbnail %s: %s", thumb_abs, exc)


@router.delete("/photos/{photo_id}", status_code=200)
async def delete_photo(request: Request, photo_id: str):
    """Delete a photo by ID.

    Writes a tombstone record to the log (so compaction can reclaim space),
    removes the photo from the in-memory index, and deletes the blob and
    thumbnail files from disk.

    Write ordering: log append (tombstone) → index delete → disk cleanup.

    Returns:
        200 OK with the deleted photo's metadata.
        404 Not Found if photo_id doesn't exist.
    """
    index = request.app.state.index
    log_writer = request.app.state.log_writer

    meta = index.get(photo_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Photo not found.")

    # 1. Build tombstone record — a copy of the current metadata with tombstone=True
    tombstone = meta.model_copy(update={"tombstone": True})

    # 2. Append tombstone to the log (preserves write-ordering invariant)
    await asyncio.to_thread(log_writer.append, tombstone)

    # 3. Remove from in-memory index
    index.delete(photo_id)

    # 4. Delete blob + thumbnails from disk (best-effort, in background thread)
    await asyncio.to_thread(
        _delete_files_from_disk, meta.content_hash, meta.thumbnail_paths
    )

    logger.info("Deleted photo: photo_id=%s, hash=%s", photo_id, meta.content_hash)

    return {"status": "deleted", "photo": meta.model_dump(mode="json")}

