"""Log segment reader for startup replay and crash recovery.

Reads segment files sequentially, validates CRC32 checksums on each record,
and yields valid PhotoMeta instances. On a CRC mismatch (torn write), iteration
stops for that segment — all prior records are guaranteed valid.

All I/O is synchronous and read-only. This module never opens files for writing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import zlib
from collections.abc import Iterator

from app.storage.models import PhotoMeta

logger = logging.getLogger("pixel_vault.storage.log_reader")

# Must match the pattern used by LogWriter
_SEGMENT_PATTERN = re.compile(r"^segment_(\d{4})\.log$")


def _verify_crc32(record_dict: dict, expected_crc: int) -> bool:
    """Verify the CRC32 checksum of a record.

    Strips the `crc32` field, re-serializes with sort_keys=True, recomputes
    the checksum, and compares against the expected value.
    """
    # Build the dict without crc32 for checksum computation
    check_dict = {k: v for k, v in record_dict.items() if k != "crc32"}
    canonical_bytes = json.dumps(check_dict, sort_keys=True, ensure_ascii=False).encode("utf-8")
    computed = zlib.crc32(canonical_bytes) & 0xFFFFFFFF
    return computed == expected_crc


class LogReader:
    """Read-only log segment reader for index reconstruction and recovery."""

    @staticmethod
    def replay_segment(segment_path: str) -> Iterator[PhotoMeta]:
        """Replay a single segment file, yielding valid PhotoMeta records.

        Reads line by line, validates the CRC32 checksum for each record.
        On the first CRC mismatch (torn write), logs a warning and stops
        iteration — all previously yielded records are valid.

        Args:
            segment_path: Absolute path to the segment file.

        Yields:
            PhotoMeta instances for each valid record in the segment.
        """
        segment_name = os.path.basename(segment_path)
        records_read = 0

        try:
            with open(segment_path, "rb") as f:
                while True:
                    line_offset = f.tell()
                    line = f.readline()

                    # EOF
                    if not line:
                        break

                    # Skip empty lines (shouldn't exist, but defensive)
                    stripped = line.strip()
                    if not stripped:
                        continue

                    try:
                        record = json.loads(stripped)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        logger.warning(
                            "JSON decode error in %s at offset %d: %s. "
                            "Stopping replay for this segment.",
                            segment_name,
                            line_offset,
                            exc,
                        )
                        break

                    # Extract and verify CRC32
                    expected_crc = record.get("crc32")
                    if expected_crc is None:
                        logger.warning(
                            "Record missing crc32 field in %s at offset %d. "
                            "Stopping replay for this segment.",
                            segment_name,
                            line_offset,
                        )
                        break

                    if not _verify_crc32(record, expected_crc):
                        logger.warning(
                            "CRC32 mismatch in %s at offset %d (expected %d). "
                            "Possible torn write. Stopping replay for this segment. "
                            "%d records recovered before this point.",
                            segment_name,
                            line_offset,
                            expected_crc,
                            records_read,
                        )
                        break

                    # Strip crc32 before building PhotoMeta
                    record.pop("crc32", None)

                    # Populate bookkeeping fields from the file position
                    record["segment_id"] = segment_name
                    record["offset"] = line_offset
                    record["length"] = len(line)

                    try:
                        meta = PhotoMeta.model_validate(record)
                    except Exception as exc:
                        logger.warning(
                            "Failed to parse PhotoMeta in %s at offset %d: %s. "
                            "Stopping replay for this segment.",
                            segment_name,
                            line_offset,
                            exc,
                        )
                        break

                    records_read += 1
                    yield meta

        except FileNotFoundError:
            logger.error("Segment file not found: %s", segment_path)
            return

        logger.info(
            "Replayed %s: %d records recovered.",
            segment_name,
            records_read,
        )

    @staticmethod
    def replay_segment_from_offset(
        segment_path: str, start_offset: int
    ) -> Iterator[PhotoMeta]:
        """Replay a segment starting from a specific byte offset.

        Used by snapshot-based startup to skip records before the watermark.
        Seeks to ``start_offset``, then reads line-by-line with CRC32
        validation as usual.

        If the offset lands in the middle of a record (e.g., post-compaction
        offset mismatch), the first readline() will return a partial JSON
        fragment, the CRC32 check will fail, and replay stops safely — no
        data corruption.

        Args:
            segment_path: Absolute path to the segment file.
            start_offset: Byte offset to seek to before reading.

        Yields:
            PhotoMeta instances for each valid record after the offset.
        """
        segment_name = os.path.basename(segment_path)
        records_read = 0

        try:
            with open(segment_path, "rb") as f:
                f.seek(start_offset)

                while True:
                    line_offset = f.tell()
                    line = f.readline()

                    # EOF
                    if not line:
                        break

                    # Skip empty lines
                    stripped = line.strip()
                    if not stripped:
                        continue

                    try:
                        record = json.loads(stripped)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        logger.warning(
                            "JSON decode error in %s at offset %d: %s. "
                            "Stopping replay for this segment (may be "
                            "stale watermark after compaction).",
                            segment_name,
                            line_offset,
                            exc,
                        )
                        break

                    # Extract and verify CRC32
                    expected_crc = record.get("crc32")
                    if expected_crc is None:
                        logger.warning(
                            "Record missing crc32 field in %s at offset %d. "
                            "Stopping replay for this segment.",
                            segment_name,
                            line_offset,
                        )
                        break

                    if not _verify_crc32(record, expected_crc):
                        logger.warning(
                            "CRC32 mismatch in %s at offset %d (expected %d). "
                            "Possible torn write or stale watermark. "
                            "Stopping replay. %d records recovered.",
                            segment_name,
                            line_offset,
                            expected_crc,
                            records_read,
                        )
                        break

                    # Strip crc32 before building PhotoMeta
                    record.pop("crc32", None)

                    # Populate bookkeeping fields
                    record["segment_id"] = segment_name
                    record["offset"] = line_offset
                    record["length"] = len(line)

                    try:
                        meta = PhotoMeta.model_validate(record)
                    except Exception as exc:
                        logger.warning(
                            "Failed to parse PhotoMeta in %s at offset %d: %s. "
                            "Stopping replay for this segment.",
                            segment_name,
                            line_offset,
                            exc,
                        )
                        break

                    records_read += 1
                    yield meta

        except FileNotFoundError:
            logger.error("Segment file not found: %s", segment_path)
            return

        logger.info(
            "Replayed %s from offset %d: %d records recovered.",
            segment_name,
            start_offset,
            records_read,
        )

    @staticmethod
    def replay_all(log_dir: str = "/storage/logs") -> Iterator[PhotoMeta]:
        """Replay all segment files in chronological order.

        Discovers `segment_NNNN.log` files in the log directory, sorts them
        lexicographically (which equals chronologically due to zero-padded
        naming), and chains their replay iterators.

        Args:
            log_dir: Path to the log directory.

        Yields:
            PhotoMeta instances from all segments in order.
        """
        if not os.path.isdir(log_dir):
            logger.warning("Log directory does not exist: %s", log_dir)
            return

        # Discover and sort segment files
        segment_files = []
        for name in sorted(os.listdir(log_dir)):
            if _SEGMENT_PATTERN.match(name):
                segment_files.append(os.path.join(log_dir, name))

        if not segment_files:
            logger.info("No segment files found in %s. Starting with empty index.", log_dir)
            return

        logger.info(
            "Found %d segment file(s) in %s. Starting replay...",
            len(segment_files),
            log_dir,
        )

        total_records = 0
        for segment_path in segment_files:
            for meta in LogReader.replay_segment(segment_path):
                total_records += 1
                yield meta

        logger.info("Full replay complete: %d total records recovered.", total_records)
