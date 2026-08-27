"""In-memory index for the Bitcask-style storage engine.

Holds full metadata records in RAM — reads are pure hashmap lookups with zero
disk I/O. The index is rebuilt from log segments on startup and updated
incrementally on each write.

Thread safety: `put()` is guarded by a threading.Lock. Reads (`get`,
`contains_hash`, `get_by_hash`) are lock-free — Python's GIL guarantees
dict reads are atomic for our single-writer, multi-reader pattern.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading

from app.storage.log_reader import LogReader
from app.storage.models import PhotoMeta

logger = logging.getLogger("pixel_vault.storage.index")

# Must match the pattern used by LogWriter / LogReader
_SEGMENT_PATTERN = re.compile(r"^segment_(\d{4})\.log$")

# Snapshot format version — bump when schema changes
_SNAPSHOT_VERSION = 1


class IndexStore:
    """In-memory photo metadata index with primary and deduplication lookups.

    Data structures:
        _primary:    photo_id (str) -> PhotoMeta  (the authoritative index)
        _hash_index: content_hash (str) -> photo_id (str)  (dedup lookup)
    """

    def __init__(self) -> None:
        self._primary: dict[str, PhotoMeta] = {}
        self._hash_index: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, meta: PhotoMeta) -> None:
        """Insert or replace a metadata record in the index.

        Since the log is append-only, a later record for the same photo_id
        supersedes an earlier one. This is how updates work in Bitcask.

        Thread-safe: guarded by a lock to prevent concurrent put() calls
        from corrupting the dict (belt-and-suspenders with the single-writer
        invariant).
        """
        with self._lock:
            self._primary[meta.photo_id] = meta
            self._hash_index[meta.content_hash] = meta.photo_id

    def get(self, photo_id: str) -> PhotoMeta | None:
        """Look up a photo by its ID. Pure dict lookup — no disk I/O."""
        return self._primary.get(photo_id)

    def contains_hash(self, content_hash: str) -> bool:
        """Check if a blob with this content hash exists in the index.

        Used for deduplication during upload. O(1) via secondary index.
        """
        return content_hash in self._hash_index

    def get_by_hash(self, content_hash: str) -> PhotoMeta | None:
        """Look up a photo by its content hash. Returns None if not found."""
        photo_id = self._hash_index.get(content_hash)
        if photo_id is None:
            return None
        return self._primary.get(photo_id)

    def delete(self, photo_id: str) -> PhotoMeta | None:
        """Remove a photo from the in-memory index.

        Deletes the entry from both ``_primary`` and ``_hash_index``.
        Thread-safe: guarded by the same lock as ``put()``.

        Args:
            photo_id: The photo to remove.

        Returns:
            The removed ``PhotoMeta``, or ``None`` if not found.
        """
        with self._lock:
            meta = self._primary.pop(photo_id, None)
            if meta is not None:
                # Only remove from hash_index if it still points to this photo_id
                # (another photo with the same hash could exist — edge case)
                if self._hash_index.get(meta.content_hash) == photo_id:
                    del self._hash_index[meta.content_hash]
        return meta

    # ── Snapshot: save ────────────────────────────────────────────────

    def save_snapshot(
        self,
        log_dir: str,
        current_segment_name: str,
        current_segment_offset: int,
    ) -> None:
        """Dump the full index to disk as a checkpoint.

        The snapshot captures a consistent view of the in-memory index at a
        specific write position (the *watermark*). On next startup, we load
        this snapshot and replay only the log records written after the
        watermark — dramatically reducing startup time.

        Concurrency: copies ``_primary`` under ``_lock`` to prevent races
        with concurrent ``put()`` calls, then serializes outside the lock.

        Crash safety: writes to a ``.tmp`` file first, fsyncs, then does an
        atomic ``os.rename()``. A crash during the write leaves the old
        snapshot (if any) intact.

        Args:
            log_dir: Path to the log directory (e.g. ``/storage/logs``).
            current_segment_name: Filename of the active segment (e.g.
                ``segment_0003.log``).
            current_segment_offset: Current byte offset in the active segment.
        """
        # 1. Copy _primary under lock — prevents races with put()
        with self._lock:
            primary_copy = dict(self._primary)

        # 2. Build the watermark (captures "snapshot is valid up to here")
        watermark = {
            "segment_id": current_segment_name,
            "offset": current_segment_offset,
        }

        # 3. Serialize all index entries
        snapshot_data = {
            "version": _SNAPSHOT_VERSION,
            "watermark": watermark,
            "records": {
                photo_id: meta.model_dump(mode="json")
                for photo_id, meta in primary_copy.items()
            },
        }

        # 4. Write to temp file first (crash safety)
        tmp_path = os.path.join(log_dir, "index-snapshot.tmp")
        with open(tmp_path, "wb") as f:
            f.write(json.dumps(snapshot_data, ensure_ascii=False).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())

        # 5. Atomic rename — old snapshot replaced in one syscall
        final_path = os.path.join(log_dir, "index-snapshot.bin")
        os.rename(tmp_path, final_path)

        logger.info(
            "Index snapshot saved: %d records, watermark=(%s, %d).",
            len(primary_copy),
            current_segment_name,
            current_segment_offset,
        )

    # ── Snapshot: load ────────────────────────────────────────────────

    def load_from_snapshot_and_log(self, log_dir: str = "/storage/logs") -> int:
        """Fast startup: load snapshot + replay only new records.

        If a valid snapshot exists:
          1. Bulk-load the index from the snapshot (one sequential read).
          2. Replay only the log records written *after* the watermark.

        If no snapshot exists or the version is unsupported, falls back to
        a full log replay (identical to :meth:`load_from_log`).

        Args:
            log_dir: Path to the log directory.

        Returns:
            Number of unique photos in the index after loading.
        """
        snapshot_path = os.path.join(log_dir, "index-snapshot.bin")

        if not os.path.exists(snapshot_path):
            logger.info("No snapshot found. Falling back to full log replay.")
            self.load_from_log(log_dir)
            return self.count

        try:
            with open(snapshot_path, "rb") as f:
                snapshot = json.loads(f.read())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read snapshot (%s). Falling back to full replay.",
                exc,
            )
            self.load_from_log(log_dir)
            return self.count

        # Version gate — reject unknown versions
        version = snapshot.get("version")
        if version != _SNAPSHOT_VERSION:
            logger.warning(
                "Snapshot version %s is unsupported (expected %d). "
                "Falling back to full replay.",
                version,
                _SNAPSHOT_VERSION,
            )
            self.load_from_log(log_dir)
            return self.count

        # Populate the index from snapshot records
        records = snapshot.get("records", {})
        for photo_id, record_dict in records.items():
            try:
                meta = PhotoMeta.model_validate(record_dict)
                self.put(meta)
            except Exception as exc:
                logger.warning(
                    "Skipping invalid snapshot record for photo_id=%s: %s",
                    photo_id,
                    exc,
                )

        logger.info(
            "Loaded %d records from snapshot.", len(self._primary),
        )

        # Replay only records after the watermark
        watermark = snapshot.get("watermark", {})
        replayed = self._replay_after(log_dir, watermark)

        logger.info(
            "Snapshot startup complete: %d from snapshot + %d replayed = %d unique photos.",
            len(records),
            replayed,
            self.count,
        )
        return self.count

    # ── Snapshot: partial replay ──────────────────────────────────────

    def _replay_after(self, log_dir: str, watermark: dict) -> int:
        """Replay log records written after the snapshot watermark.

        Finds all segments at or after the watermark's segment, replays the
        watermark segment from the watermark offset, and replays all
        subsequent segments from the beginning.

        Args:
            log_dir: Path to the log directory.
            watermark: Dict with ``segment_id`` and ``offset`` keys.

        Returns:
            Number of records replayed.
        """
        wm_segment_id = watermark.get("segment_id", "")
        wm_offset = watermark.get("offset", 0)

        # Extract the counter from the watermark segment name
        wm_match = _SEGMENT_PATTERN.match(wm_segment_id)
        if not wm_match:
            logger.warning(
                "Invalid watermark segment_id '%s'. Skipping partial replay.",
                wm_segment_id,
            )
            return 0

        wm_counter = int(wm_match.group(1))

        # Discover and sort all segment files
        if not os.path.isdir(log_dir):
            return 0

        segment_files = []
        for name in sorted(os.listdir(log_dir)):
            match = _SEGMENT_PATTERN.match(name)
            if match:
                counter = int(match.group(1))
                segment_files.append((counter, name))

        replayed = 0
        for counter, name in segment_files:
            segment_path = os.path.join(log_dir, name)

            if counter < wm_counter:
                # Before the watermark — already captured in the snapshot
                continue
            elif counter == wm_counter:
                # The watermark segment — replay from the watermark offset
                for meta in LogReader.replay_segment_from_offset(
                    segment_path, wm_offset
                ):
                    self.put(meta)
                    replayed += 1
            else:
                # After the watermark — replay from the beginning
                for meta in LogReader.replay_segment(segment_path):
                    self.put(meta)
                    replayed += 1

        return replayed

    # ── Full log replay (original startup path) ──────────────────────

    def load_from_log(self, log_dir: str = "/storage/logs") -> int:
        """Rebuild the full index by replaying all log segments.

        Called during server startup. Iterates through all records in
        chronological order — later records for the same photo_id naturally
        supersede earlier ones via `put()`.

        Args:
            log_dir: Path to the log directory.

        Returns:
            Number of records loaded into the index.
        """
        count = 0
        for meta in LogReader.replay_all(log_dir):
            self.put(meta)
            count += 1

        logger.info(
            "Index loaded: %d records replayed, %d unique photos, %d unique hashes.",
            count,
            len(self._primary),
            len(self._hash_index),
        )
        return count

    @property
    def count(self) -> int:
        """Number of unique photos in the index."""
        return len(self._primary)

    @property
    def all_photos(self) -> list[PhotoMeta]:
        """Return all photo metadata records. For debugging/admin use."""
        return list(self._primary.values())
