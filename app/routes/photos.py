"""Photo API routes — upload, retrieval, listing.

Thin route handlers that delegate business logic to the service layer
and storage engine.  All heavy I/O is offloaded to a thread via
``asyncio.to_thread()`` to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

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
async def upload_photo(request: Request, file: UploadFile):
    """Upload a photo via multipart/form-data.

    The file is streamed to a temp location, hashed, deduplicated,
    committed to the blob store, and indexed — all in a single
    background thread.

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
