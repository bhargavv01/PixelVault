import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
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

