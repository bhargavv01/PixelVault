"""Log segment compactor for the Bitcask-style storage engine.

Compaction merges old segment files by keeping only the latest record per
``photo_id`` and discarding superseded (and tombstoned) records. It reclaims
disk space without changing the logical state of the system.

Invariants respected:
- The log is append-only: compaction writes a NEW file and does an atomic swap.
  It never edits bytes in the existing segment.
- Never compact the active segment: only closed/full segments are eligible.
- CRC32 per record: every record in the compacted file gets a fresh CRC32.
- Crash safety: if we crash during compaction, the ``.compact.tmp`` file is
  incomplete — the old segment is still intact.
- Index is NOT updated during compaction (Decision #2 from grill-me review).
  Reads serve from ``_primary`` which holds full metadata. Stale bookkeeping
  fields (``segment_id``/``offset``/``length``) are harmless and corrected
  on the next snapshot+restart.
- A snapshot is ALWAYS taken immediately after compaction (Decision #9).
"""

from __future__ import annotations

import json
import logging
import os
import re
import zlib
from dataclasses import dataclass, asdict

from app.storage.index import IndexStore
from app.storage.log_reader import LogReader
from app.storage.models import PhotoMeta

logger = logging.getLogger("pixel_vault.storage.compactor")

# Must match LogWriter / LogReader
_SEGMENT_PATTERN = re.compile(r"^segment_(\d{4})\.log$")


def _compute_crc32(record_dict: dict) -> int:
    """Compute CRC32 over the canonical JSON bytes (excluding crc32 field)."""
    canonical_bytes = json.dumps(
        record_dict, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return zlib.crc32(canonical_bytes) & 0xFFFFFFFF


@dataclass
class CompactionResult:
    """Summary of a single segment compaction."""

    segment_name: str
    records_before: int
    records_after: int
    bytes_before: int
    bytes_after: int

    def to_dict(self) -> dict:
        return asdict(self)


class Compactor:
    """Compacts closed log segments by removing superseded and tombstoned records.

    This class does NOT update the in-memory index during compaction. The index
    is only corrected on the next snapshot+restart cycle. This avoids the TOCTOU
    race identified in the design review.

    Args:
        log_dir: Path to the log directory (default: ``/storage/logs``).
        index: The in-memory index, used for cross-checking which records
            are still current.
        log_writer: The active log writer, used to identify the active
            segment (which must never be compacted) and to take a snapshot
            after compaction.
    """

    def __init__(
        self,
        log_dir: str,
        index: IndexStore,
        log_writer: "LogWriter",
    ) -> None:
        self._log_dir = log_dir
        self._index = index
        self._log_writer = log_writer

    def compact_segment(self, segment_path: str) -> CompactionResult:
        """Compact a single closed segment file.

        Steps:
          1. Guard: refuse to compact the active segment.
          2. Replay the segment and deduplicate (keep latest per ``photo_id``).
          3. Cross-check against the index: drop records where the index
             shows the photo now lives in a different segment.
          4. Drop tombstoned records entirely.
          5. Write surviving records to ``.compact.tmp`` with fresh CRC32.
          6. fsync + atomic rename to replace the original segment.
          7. Do NOT update the in-memory index.

        Args:
            segment_path: Absolute path to the segment file to compact.

        Returns:
            A :class:`CompactionResult` summarizing the compaction.

        Raises:
            RuntimeError: If ``segment_path`` is the active segment.
            FileNotFoundError: If the segment file does not exist.
        """
        segment_name = os.path.basename(segment_path)

        # ── INVARIANT #7: Never compact the active segment ──
        if segment_name == self._log_writer.active_segment_name:
            raise RuntimeError(
                f"Cannot compact the active segment: {segment_name}"
            )

        if not os.path.exists(segment_path):
            raise FileNotFoundError(f"Segment not found: {segment_path}")

        bytes_before = os.path.getsize(segment_path)

        # 1. Replay the segment — collect all records
        records = list(LogReader.replay_segment(segment_path))
        records_before = len(records)

        # 2. Deduplicate: keep only the LATEST record per photo_id
        #    (Later records supersede earlier ones — Bitcask semantics)
        latest: dict[str, PhotoMeta] = {}
        for meta in records:
            latest[meta.photo_id] = meta

        # 3. Filter: keep only records that are still current in the index
        #    AND are not tombstoned.
        live_records: list[PhotoMeta] = []
        for photo_id, meta in latest.items():
            # Drop tombstoned records — they mark deleted photos
            if meta.tombstone:
                logger.debug(
                    "Dropping tombstoned record: photo_id=%s in %s",
                    photo_id,
                    segment_name,
                )
                continue

            # Cross-check: is this record still the current version?
            current = self._index.get(photo_id)
            if current is not None and current.segment_id == segment_name:
                live_records.append(meta)
            else:
                logger.debug(
                    "Dropping superseded record: photo_id=%s (index points to %s, not %s)",
                    photo_id,
                    current.segment_id if current else "<deleted>",
                    segment_name,
                )

        # 4. Write compacted segment to a temp file
        compact_tmp = segment_path + ".compact.tmp"
        with open(compact_tmp, "wb") as f:
            for meta in live_records:
                # Build record dict without bookkeeping fields
                record = meta.model_dump(mode="json")
                record.pop("segment_id", None)
                record.pop("offset", None)
                record.pop("length", None)

                # Compute fresh CRC32
                crc = _compute_crc32(record)
                record["crc32"] = crc

                # Serialize as a single JSON line
                line_bytes = (
                    json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    + b"\n"
                )
                f.write(line_bytes)

            f.flush()
            os.fsync(f.fileno())

        # 5. Atomic swap — replace the old segment
        os.rename(compact_tmp, segment_path)

        bytes_after = os.path.getsize(segment_path)
        records_after = len(live_records)

        # 6. Do NOT update the in-memory index (Decision #2).
        #    The stale segment_id/offset/length in the index are harmless
        #    because reads serve from _primary (full metadata), not from
        #    segment seeks. The next snapshot+restart corrects bookkeeping.

        result = CompactionResult(
            segment_name=segment_name,
            records_before=records_before,
            records_after=records_after,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
        )

        logger.info(
            "Compacted %s: %d -> %d records, %d -> %d bytes (%.0f%% saved).",
            segment_name,
            records_before,
            records_after,
            bytes_before,
            bytes_after,
            (1 - bytes_after / bytes_before) * 100 if bytes_before > 0 else 0,
        )

        return result

    def compact_all_closed(self) -> list[CompactionResult]:
        """Compact all closed (non-active) segments.

        Iterates all ``segment_NNNN.log`` files in the log directory,
        skipping the active segment. Each eligible segment is compacted
        individually.

        After all segments are compacted, a snapshot is taken immediately
        (Decision #9: compaction without a follow-up snapshot is a bug).

        Returns:
            A list of :class:`CompactionResult` for each compacted segment.
        """
        if not os.path.isdir(self._log_dir):
            logger.warning("Log directory does not exist: %s", self._log_dir)
            return []

        active = self._log_writer.active_segment_name

        # Discover segment files, excluding the active one
        segment_files = []
        for name in sorted(os.listdir(self._log_dir)):
            if _SEGMENT_PATTERN.match(name) and name != active:
                segment_files.append(os.path.join(self._log_dir, name))

        if not segment_files:
            logger.info("No closed segments to compact.")
            return []

        logger.info(
            "Compacting %d closed segment(s) (active=%s)...",
            len(segment_files),
            active,
        )

        results = []
        for segment_path in segment_files:
            try:
                result = self.compact_segment(segment_path)
                results.append(result)
            except Exception as exc:
                logger.error(
                    "Failed to compact %s: %s",
                    os.path.basename(segment_path),
                    exc,
                )

        # ── Decision #9: ALWAYS snapshot after compaction ──
        # This ensures the watermark references post-compaction offsets.
        # Without this, a crash before the next natural snapshot would
        # leave the watermark pointing to pre-compaction byte offsets.
        self._index.save_snapshot(
            self._log_dir,
            self._log_writer.active_segment_name,
            self._log_writer.active_segment_offset,
        )
        self._log_writer.reset_snapshot_counter()

        logger.info(
            "Compaction complete: %d segment(s) processed, post-compaction snapshot saved.",
            len(results),
        )

        return results

    def run_background(self) -> list[CompactionResult]:
        """Entry point for background thread execution.

        Sets the process to lowest I/O and CPU priority to avoid contending
        with upload traffic on the mechanical HDD, then runs compaction.

        Returns:
            Results from :meth:`compact_all_closed`.
        """
        try:
            os.nice(19)
        except OSError:
            # nice(19) may fail if already at max niceness — harmless
            pass

        logger.info("Background compaction starting (nice=19).")
        return self.compact_all_closed()
