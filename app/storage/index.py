"""In-memory index for the Bitcask-style storage engine.

Holds full metadata records in RAM — reads are pure hashmap lookups with zero
disk I/O. The index is rebuilt from log segments on startup and updated
incrementally on each write.

Thread safety: `put()` is guarded by a threading.Lock. Reads (`get`,
`contains_hash`, `get_by_hash`) are lock-free — Python's GIL guarantees
dict reads are atomic for our single-writer, multi-reader pattern.
"""

from __future__ import annotations

import logging
import threading

from app.storage.log_reader import LogReader
from app.storage.models import PhotoMeta

logger = logging.getLogger("pixel_vault.storage.index")


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
