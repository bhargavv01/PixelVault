"""Admin routes for storage engine maintenance.

These endpoints are intended for manual/ops use, not end-user clients.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from app.storage.compactor import Compactor

logger = logging.getLogger("pixel_vault.routes.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/compact")
async def trigger_compaction(request: Request):
    """Manually trigger compaction of all closed segments.

    Runs compaction in a background thread to avoid blocking the event loop.
    Compaction is I/O-heavy — on a mechanical HDD, it may take several
    seconds per segment. A post-compaction snapshot is taken automatically.

    Returns:
        JSON with compaction results for each processed segment.
    """
    index = request.app.state.index
    log_writer = request.app.state.log_writer

    compactor = Compactor(
        log_dir="/storage/logs",
        index=index,
        log_writer=log_writer,
    )

    results = await asyncio.to_thread(compactor.compact_all_closed)

    return {
        "status": "ok",
        "segments_compacted": len(results),
        "results": [r.to_dict() for r in results],
    }


@router.post("/snapshot")
async def trigger_snapshot(request: Request):
    """Manually trigger an index snapshot.

    Saves the current in-memory index to ``index-snapshot.bin`` for fast
    startup recovery.

    Returns:
        JSON with snapshot status and photo count.
    """
    index = request.app.state.index
    log_writer = request.app.state.log_writer

    await asyncio.to_thread(
        index.save_snapshot,
        "/storage/logs",
        log_writer.active_segment_name,
        log_writer.active_segment_offset,
    )
    log_writer.reset_snapshot_counter()

    return {
        "status": "ok",
        "photos_in_index": index.count,
    }
