import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
import uvicorn

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

    # Rebuild in-memory index from log segments (runs in thread to avoid blocking)
    index = IndexStore()
    count = await asyncio.to_thread(index.load_from_log)
    logger.info("Index rebuilt: %d records loaded.", count)

    # Initialize the single log writer (acquires flock)
    log_writer = LogWriter()

    # Store on app.state so route handlers can access via request.app.state
    app.state.index = index
    app.state.log_writer = log_writer

    yield

    # Shutdown phase
    logger.info("Shutting down Pixel Vault API server...")
    app.state.log_writer.close()
    logger.info("Log writer closed.")


app = FastAPI(
    title="Pixel Vault",
    description="Private Photo Cloud Server",
    version="0.1.0",
    lifespan=lifespan,
)


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
