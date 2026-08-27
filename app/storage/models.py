"""Photo metadata model — the in-memory index value and log serialization format."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class PhotoMeta(BaseModel):
    """Represents a single photo's metadata record.

    This model is the canonical schema for:
    - The in-memory index value (IndexStore._primary values)
    - The on-disk JSONL log record (serialized/deserialized by LogWriter/LogReader)

    The `segment_id`, `offset`, and `length` fields are bookkeeping for crash-recovery
    and compaction. They are populated after the log append succeeds and are NOT part
    of the CRC32 checksum computation (they describe *where* the record landed, which
    is only known after writing).
    """

    photo_id: str = Field(description="UUID4 identifier, generated server-side at upload.")
    content_hash: str = Field(description="SHA-256 hex digest of the blob file.")
    taken_at: datetime | None = Field(default=None, description="EXIF DateTimeOriginal.")
    camera: str | None = Field(default=None, description="EXIF camera model string.")
    gps: tuple[float, float] | None = Field(default=None, description="(lat, lon) from EXIF.")
    thumbnail_paths: list[str] = Field(default_factory=list, description="Paths to generated thumbnails.")
    tombstone: bool = Field(default=False, description="Soft-delete marker. Compaction drops tombstoned records.")
    uploaded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Server-side upload timestamp (UTC).",
    )

    # Bookkeeping — populated after log append
    segment_id: str = Field(default="", description="Filename of the segment this record lives in.")
    offset: int = Field(default=0, description="Byte offset of the record within the segment.")
    length: int = Field(default=0, description="Byte length of the serialized record line.")
