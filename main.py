import asyncio
from contextlib import asynccontextmanager
import logging

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from app.routes.admin import router as admin_router
from app.routes.photos import router as photos_router
from app.storage.index import IndexStore
from app.storage.log_writer import LogWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pixel_vault")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup phase
    logger.info("Starting Pixel Vault API server...")

    # Rebuild in-memory index — tries snapshot first, falls back to full replay
    index = IndexStore()
    count = await asyncio.to_thread(index.load_from_snapshot_and_log)
    logger.info("Index ready: %d unique photos loaded.", count)

    # Initialize the single log writer (acquires flock)
    log_writer = LogWriter()

    # Store on app.state so route handlers can access via request.app.state
    app.state.index = index
    app.state.log_writer = log_writer

    yield

    # Shutdown phase
    logger.info("Shutting down Pixel Vault API server...")

    # Save final snapshot for fast next startup
    await asyncio.to_thread(
        index.save_snapshot,
        "/storage/logs",
        log_writer.active_segment_name,
        log_writer.active_segment_offset,
    )
    log_writer.reset_snapshot_counter()
    logger.info("Index snapshot saved.")

    app.state.log_writer.close()
    logger.info("Log writer closed.")


app = FastAPI(
    title="Pixel Vault",
    description="Private Photo Cloud Server",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(photos_router)
app.include_router(admin_router)

# Mount static asset files (CSS, JS, icons)
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend():
    """Serve the single-page application frontend."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index_file):
        return FileResponse(index_file)
    return {"message": "Pixel Vault API is running. Frontend not found."}


@app.get("/health")
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )

