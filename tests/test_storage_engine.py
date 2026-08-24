"""Storage engine test suite.

Tests the core invariants from the storage-engine skill:
- Round-trip: write N records, replay, verify index matches
- Torn-write: truncate last record, replay recovers all prior records
- Dedup index: same content_hash, different photo_id
- Segment rotation: small max_segment_bytes triggers multiple segments
- CRC integrity: flipped byte detected as corruption

All tests use temporary directories — never touch /storage/.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone

import pytest

from app.storage.index import IndexStore
from app.storage.log_reader import LogReader
from app.storage.log_writer import LogWriter
from app.storage.models import PhotoMeta


def _make_meta(
    content_hash: str | None = None,
    photo_id: str | None = None,
    taken_at: datetime | None = None,
) -> PhotoMeta:
    """Helper to create a PhotoMeta with sensible defaults."""
    return PhotoMeta(
        photo_id=photo_id or str(uuid.uuid4()),
        content_hash=content_hash or f"sha256_{uuid.uuid4().hex[:16]}",
        taken_at=taken_at,
        camera="TestCamera",
        gps=(37.7749, -122.4194),
        thumbnail_paths=[],
        uploaded_at=datetime.now(timezone.utc),
    )


class TestRoundTrip:
    """Write N records, simulate restart, verify index matches."""

    def test_write_and_replay_single_record(self, tmp_path):
        """Single record round-trip."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=64 * 1024 * 1024)

        original = _make_meta()
        written = writer.append(original)
        writer.close()

        assert written.segment_id == "segment_0001.log"
        assert written.offset == 0
        assert written.length > 0

        # Simulate restart: fresh index, replay from log
        index = IndexStore()
        count = index.load_from_log(log_dir=log_dir)

        assert count == 1
        assert index.count == 1

        recovered = index.get(original.photo_id)
        assert recovered is not None
        assert recovered.photo_id == original.photo_id
        assert recovered.content_hash == original.content_hash
        assert recovered.camera == original.camera
        assert recovered.gps == original.gps

    def test_write_and_replay_many_records(self, tmp_path):
        """Multiple records round-trip."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=64 * 1024 * 1024)

        originals = []
        for _ in range(50):
            meta = _make_meta()
            written = writer.append(meta)
            originals.append(written)

        writer.close()

        # Simulate restart
        index = IndexStore()
        count = index.load_from_log(log_dir=log_dir)

        assert count == 50
        assert index.count == 50

        for orig in originals:
            recovered = index.get(orig.photo_id)
            assert recovered is not None
            assert recovered.photo_id == orig.photo_id
            assert recovered.content_hash == orig.content_hash

    def test_update_supersedes_earlier_record(self, tmp_path):
        """A later record for the same photo_id supersedes an earlier one."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=64 * 1024 * 1024)

        photo_id = str(uuid.uuid4())
        v1 = _make_meta(photo_id=photo_id, content_hash="hash_v1")
        v2 = _make_meta(photo_id=photo_id, content_hash="hash_v2")

        writer.append(v1)
        writer.append(v2)
        writer.close()

        index = IndexStore()
        count = index.load_from_log(log_dir=log_dir)

        # Both records replayed, but index holds only the latest
        assert count == 2
        assert index.count == 1

        recovered = index.get(photo_id)
        assert recovered is not None
        assert recovered.content_hash == "hash_v2"


class TestTornWrite:
    """Simulate crash mid-write and verify recovery."""

    def test_truncated_last_record_recovered(self, tmp_path):
        """Truncate the last record mid-byte. Prior records must survive."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=64 * 1024 * 1024)

        # Write 5 valid records
        originals = []
        for _ in range(5):
            meta = _make_meta()
            written = writer.append(meta)
            originals.append(written)

        writer.close()

        # Find the segment file and truncate the last record mid-byte
        segment_path = os.path.join(log_dir, "segment_0001.log")
        file_size = os.path.getsize(segment_path)

        # Truncate to remove the last ~half of the last record
        last_record = originals[-1]
        truncate_at = last_record.offset + (last_record.length // 2)

        with open(segment_path, "r+b") as f:
            f.truncate(truncate_at)

        # Replay — should recover first 4 records, skip the torn 5th
        records = list(LogReader.replay_segment(segment_path))
        assert len(records) == 4

        for i, recovered in enumerate(records):
            assert recovered.photo_id == originals[i].photo_id

    def test_empty_segment_replays_zero_records(self, tmp_path):
        """An empty segment file yields no records."""
        segment_path = os.path.join(str(tmp_path), "segment_0001.log")
        with open(segment_path, "wb") as f:
            pass  # Create empty file

        records = list(LogReader.replay_segment(segment_path))
        assert len(records) == 0


class TestCRCIntegrity:
    """Verify CRC32 detects byte-level corruption."""

    def test_flipped_byte_detected(self, tmp_path):
        """Flip a byte in a valid record. CRC mismatch must be detected."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=64 * 1024 * 1024)

        # Write 3 records
        for _ in range(3):
            writer.append(_make_meta())
        writer.close()

        # Corrupt the second record by flipping a byte
        segment_path = os.path.join(log_dir, "segment_0001.log")
        with open(segment_path, "rb") as f:
            data = f.read()

        lines = data.split(b"\n")
        # lines[1] is the second record (0-indexed)
        corrupted_line = bytearray(lines[1])
        # Flip a byte in the middle of the content (not the crc field)
        mid = len(corrupted_line) // 2
        corrupted_line[mid] ^= 0xFF
        lines[1] = bytes(corrupted_line)

        with open(segment_path, "wb") as f:
            f.write(b"\n".join(lines))

        # Replay — should recover only the first record, then stop at corruption
        records = list(LogReader.replay_segment(segment_path))
        assert len(records) == 1


class TestDedupIndex:
    """Verify the content_hash -> photo_id secondary index."""

    def test_contains_hash_after_insert(self, tmp_path):
        """After inserting a record, its content_hash is findable."""
        index = IndexStore()
        meta = _make_meta(content_hash="abc123")
        index.put(meta)

        assert index.contains_hash("abc123") is True
        assert index.contains_hash("nonexistent") is False

    def test_get_by_hash(self, tmp_path):
        """get_by_hash returns the correct PhotoMeta."""
        index = IndexStore()
        meta = _make_meta(content_hash="abc123")
        index.put(meta)

        recovered = index.get_by_hash("abc123")
        assert recovered is not None
        assert recovered.photo_id == meta.photo_id

    def test_same_hash_different_ids_latest_wins(self, tmp_path):
        """Two records with same content_hash — dedup index points to latest."""
        index = IndexStore()
        meta1 = _make_meta(content_hash="same_hash", photo_id="id_1")
        meta2 = _make_meta(content_hash="same_hash", photo_id="id_2")

        index.put(meta1)
        index.put(meta2)

        assert index.contains_hash("same_hash") is True
        recovered = index.get_by_hash("same_hash")
        assert recovered is not None
        assert recovered.photo_id == "id_2"  # Latest wins

        # Both photo_ids should still be individually retrievable
        assert index.get("id_1") is not None
        assert index.get("id_2") is not None


class TestSegmentRotation:
    """Verify segment rotation when max_segment_bytes is exceeded."""

    def test_rotation_creates_multiple_segments(self, tmp_path):
        """Small max_segment_bytes triggers rotation across multiple files."""
        log_dir = str(tmp_path)
        # Use very small threshold to force rotation
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=512)

        originals = []
        for _ in range(20):
            meta = _make_meta()
            written = writer.append(meta)
            originals.append(written)

        writer.close()

        # Verify multiple segment files were created
        segment_files = sorted(
            f for f in os.listdir(log_dir)
            if f.startswith("segment_") and f.endswith(".log")
        )
        assert len(segment_files) > 1, f"Expected multiple segments, got: {segment_files}"

        # Verify replay recovers all records
        index = IndexStore()
        count = index.load_from_log(log_dir=log_dir)

        assert count == 20
        assert index.count == 20

        for orig in originals:
            recovered = index.get(orig.photo_id)
            assert recovered is not None
            assert recovered.content_hash == orig.content_hash

    def test_rotation_preserves_segment_naming(self, tmp_path):
        """Segment filenames follow segment_NNNN.log convention."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=256)

        for _ in range(30):
            writer.append(_make_meta())
        writer.close()

        segment_files = sorted(
            f for f in os.listdir(log_dir)
            if f.startswith("segment_") and f.endswith(".log")
        )

        for i, name in enumerate(segment_files, start=1):
            assert name == f"segment_{i:04d}.log", f"Unexpected segment name: {name}"


class TestSingleWriter:
    """Verify single-writer enforcement via flock."""

    def test_second_writer_fails(self, tmp_path):
        """A second LogWriter on the same directory must raise RuntimeError."""
        log_dir = str(tmp_path)
        writer1 = LogWriter(log_dir=log_dir)

        with pytest.raises(RuntimeError, match="Cannot acquire writer lock"):
            writer2 = LogWriter(log_dir=log_dir)

        writer1.close()

    def test_writer_reacquires_after_close(self, tmp_path):
        """After close(), a new writer can acquire the lock."""
        log_dir = str(tmp_path)
        writer1 = LogWriter(log_dir=log_dir)
        writer1.close()

        # This should succeed — lock is released
        writer2 = LogWriter(log_dir=log_dir)
        writer2.close()
