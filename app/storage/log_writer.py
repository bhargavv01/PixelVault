"""Append-only log writer for the Bitcask-style storage engine.

This module is the SINGLE WRITER for the metadata log. Only the API server's
write path should call `LogWriter.append()`. Background workers must route
updates through the API — never open a log segment for append directly.

I/O is intentionally synchronous. The caller (FastAPI route) wraps calls in
`asyncio.to_thread()` to avoid blocking the event loop.
"""

from __future__ import annotations

import fcntl
import glob
import json
import logging
import os
import re
import zlib

from app.storage.models import PhotoMeta

logger = logging.getLogger("pixel_vault.storage.log_writer")

# Segment filename pattern: segment_NNNN.log
_SEGMENT_PATTERN = re.compile(r"^segment_(\d{4})\.log$")


def _compute_crc32(record_dict: dict) -> int:
    """Compute CRC32 over the canonical JSON bytes of a record (excluding crc32 field).

    The record is serialized with `sort_keys=True` and `ensure_ascii=False` to
    guarantee a deterministic byte representation for checksum verification.
    """
    canonical_bytes = json.dumps(record_dict, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return zlib.crc32(canonical_bytes) & 0xFFFFFFFF  # Ensure unsigned 32-bit


class LogWriter:
    """Append-only log writer with segment rotation and single-writer enforcement.

    Args:
        log_dir: Path to the log directory (default: /storage/logs).
        max_segment_bytes: Size threshold for segment rotation (default: 64MB).
    """

    def __init__(
        self,
        log_dir: str = "/storage/logs",
        max_segment_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._log_dir = log_dir
        self._max_segment_bytes = max_segment_bytes
        self._segment_file = None
        self._segment_counter: int = 0
        self._segment_name: str = ""
        self._lock_file = None

        # Acquire single-writer lock
        self._acquire_writer_lock()

        # Open or create the active segment
        self._open_active_segment()

        logger.info(
            "LogWriter initialized: segment=%s, log_dir=%s",
            self._segment_name,
            self._log_dir,
        )

    def _acquire_writer_lock(self) -> None:
        """Acquire an exclusive flock on the writer lock file.

        Raises RuntimeError if another process already holds the lock.
        """
        lock_path = os.path.join(self._log_dir, ".writer.lock")
        self._lock_file = open(lock_path, "w")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError(
                f"Cannot acquire writer lock at {lock_path}. "
                "Another process is already writing to the log."
            ) from exc

    def _discover_latest_segment(self) -> int:
        """Scan log_dir for existing segment files and return the highest counter.

        Returns 0 if no segments exist.
        """
        highest = 0
        for name in os.listdir(self._log_dir):
            match = _SEGMENT_PATTERN.match(name)
            if match:
                counter = int(match.group(1))
                if counter > highest:
                    highest = counter
        return highest

    def _open_active_segment(self) -> None:
        """Open the active segment file for appending, or create the first one."""
        existing_counter = self._discover_latest_segment()

        if existing_counter == 0:
            # No segments exist — create the first one
            self._segment_counter = 1
        else:
            # Check if the latest segment has room
            self._segment_counter = existing_counter
            segment_path = os.path.join(
                self._log_dir, f"segment_{self._segment_counter:04d}.log"
            )
            if os.path.exists(segment_path) and os.path.getsize(segment_path) >= self._max_segment_bytes:
                # Current segment is full — rotate to next
                self._segment_counter = existing_counter + 1

        self._segment_name = f"segment_{self._segment_counter:04d}.log"
        segment_path = os.path.join(self._log_dir, self._segment_name)

        # Open in binary append mode — NEVER "w" or "r+"
        self._segment_file = open(segment_path, "ab")
        logger.debug("Opened segment file: %s", segment_path)

    def _rotate_segment(self) -> None:
        """Close the current segment and open a new one with an incremented counter."""
        if self._segment_file:
            self._segment_file.close()

        self._segment_counter += 1
        self._segment_name = f"segment_{self._segment_counter:04d}.log"
        segment_path = os.path.join(self._log_dir, self._segment_name)

        self._segment_file = open(segment_path, "ab")
        logger.info("Rotated to new segment: %s", self._segment_name)

    def append(self, meta: PhotoMeta) -> PhotoMeta:
        """Append a metadata record to the active segment.

        The record is serialized to JSON with a CRC32 checksum, written as a
        single line, and flushed + fsynced to guarantee durability.

        Args:
            meta: The photo metadata to persist.

        Returns:
            A copy of `meta` with `segment_id`, `offset`, and `length` populated.
        """
        if self._segment_file is None:
            raise RuntimeError("LogWriter is closed.")

        # Build the record dict WITHOUT bookkeeping fields and crc32
        record = meta.model_dump(mode="json")
        # Remove bookkeeping fields — they describe WHERE the record lands,
        # which we only know after writing
        record.pop("segment_id", None)
        record.pop("offset", None)
        record.pop("length", None)

        # Compute CRC32 over the canonical JSON bytes (without crc32 field)
        crc = _compute_crc32(record)

        # Inject crc32 into the record for serialization
        record["crc32"] = crc

        # Serialize the full record (including crc32) to a single JSON line
        line_bytes = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"

        # Record the byte offset BEFORE writing
        offset = self._segment_file.tell()

        # Write → flush → fsync (durability guarantee)
        self._segment_file.write(line_bytes)
        self._segment_file.flush()
        os.fsync(self._segment_file.fileno())

        length = len(line_bytes)

        # Return a copy of meta with bookkeeping fields populated
        updated_meta = meta.model_copy(
            update={
                "segment_id": self._segment_name,
                "offset": offset,
                "length": length,
            }
        )

        logger.debug(
            "Appended record: photo_id=%s, segment=%s, offset=%d, length=%d",
            meta.photo_id,
            self._segment_name,
            offset,
            length,
        )

        # Check if rotation is needed
        current_size = self._segment_file.tell()
        if current_size >= self._max_segment_bytes:
            self._rotate_segment()

        return updated_meta

    def close(self) -> None:
        """Close the active segment file and release the writer lock."""
        if self._segment_file:
            self._segment_file.flush()
            os.fsync(self._segment_file.fileno())
            self._segment_file.close()
            self._segment_file = None
            logger.info("Closed segment file: %s", self._segment_name)

        if self._lock_file:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
            logger.info("Released writer lock.")

    def __del__(self) -> None:
        """Safety net — ensure resources are released if close() isn't called."""
        self.close()
