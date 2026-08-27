"""Compaction & snapshot test suite.

Tests the new compaction and index-snapshot features:
- Round-trip snapshot: save → load → verify identical state
- Snapshot + partial replay: snapshot → more writes → restart → all present
- Compaction basic: duplicate records → compact → only latest survives
- Compaction tombstone: tombstoned records dropped entirely
- Compaction active-segment guard: can't compact the active segment
- Snapshot-after-compaction ordering: compact → snapshot → restart → correct
- Torn-write on stale watermark: compacted offsets → CRC catches mid-record seek
- Concurrent snapshot safety: put() during snapshot doesn't crash

All tests use temporary directories — never touch /storage/.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from app.storage.compactor import Compactor
from app.storage.index import IndexStore
from app.storage.log_reader import LogReader
from app.storage.log_writer import LogWriter
from app.storage.models import PhotoMeta


def _make_meta(
    content_hash: str | None = None,
    photo_id: str | None = None,
    taken_at: datetime | None = None,
    tombstone: bool = False,
) -> PhotoMeta:
    """Helper to create a PhotoMeta with sensible defaults."""
    return PhotoMeta(
        photo_id=photo_id or str(uuid.uuid4()),
        content_hash=content_hash or f"sha256_{uuid.uuid4().hex[:16]}",
        taken_at=taken_at,
        camera="TestCamera",
        gps=(37.7749, -122.4194),
        thumbnail_paths=[],
        tombstone=tombstone,
        uploaded_at=datetime.now(timezone.utc),
    )


# ─── Test 1: Round-trip snapshot ──────────────────────────────────────


class TestSnapshotRoundTrip:
    """Save a snapshot, clear the index, reload from snapshot, verify identical."""

    def test_save_and_load_snapshot_identical(self, tmp_path):
        """Write N records, save snapshot, clear index, load snapshot — identical."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        # Write 20 records
        originals = {}
        for _ in range(20):
            meta = _make_meta()
            written = writer.append(meta)
            originals[written.photo_id] = written

        # Build the index as the server would
        index = IndexStore()
        index.load_from_log(log_dir)
        assert index.count == 20

        # Save snapshot
        index.save_snapshot(
            log_dir,
            writer.active_segment_name,
            writer.active_segment_offset,
        )

        # Verify snapshot file exists
        snapshot_path = os.path.join(log_dir, "index-snapshot.bin")
        assert os.path.exists(snapshot_path)

        # Load snapshot into a fresh index (simulates restart)
        fresh_index = IndexStore()
        count = fresh_index.load_from_snapshot_and_log(log_dir)

        assert count == 20
        assert fresh_index.count == 20

        # Verify every record matches
        for photo_id, orig in originals.items():
            recovered = fresh_index.get(photo_id)
            assert recovered is not None
            assert recovered.photo_id == orig.photo_id
            assert recovered.content_hash == orig.content_hash
            assert recovered.camera == orig.camera

        writer.close()

    def test_snapshot_has_version_field(self, tmp_path):
        """Snapshot JSON must contain a version field."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        meta = _make_meta()
        writer.append(meta)

        index = IndexStore()
        index.load_from_log(log_dir)
        index.save_snapshot(
            log_dir,
            writer.active_segment_name,
            writer.active_segment_offset,
        )

        snapshot_path = os.path.join(log_dir, "index-snapshot.bin")
        with open(snapshot_path, "rb") as f:
            data = json.loads(f.read())

        assert "version" in data
        assert data["version"] == 1
        assert "watermark" in data
        assert "records" in data

        writer.close()

    def test_no_snapshot_falls_back_to_full_replay(self, tmp_path):
        """Without a snapshot file, load_from_snapshot_and_log does full replay."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        for _ in range(10):
            writer.append(_make_meta())
        writer.close()

        # No snapshot saved — load should fall back
        index = IndexStore()
        count = index.load_from_snapshot_and_log(log_dir)

        assert count == 10

    def test_corrupt_snapshot_falls_back_to_full_replay(self, tmp_path):
        """If snapshot is corrupt JSON, falls back to full replay."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        for _ in range(5):
            writer.append(_make_meta())
        writer.close()

        # Write corrupt snapshot
        snapshot_path = os.path.join(log_dir, "index-snapshot.bin")
        with open(snapshot_path, "wb") as f:
            f.write(b"this is not valid json{{{")

        index = IndexStore()
        count = index.load_from_snapshot_and_log(log_dir)

        assert count == 5  # Full replay recovered all records


# ─── Test 2: Snapshot + partial replay ────────────────────────────────


class TestSnapshotPartialReplay:
    """Snapshot → more writes → restart → all records present."""

    def test_snapshot_then_more_writes(self, tmp_path):
        """Records written after snapshot are recovered via partial replay."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        # Phase 1: write 10 records, snapshot
        phase1_ids = []
        for _ in range(10):
            meta = _make_meta()
            written = writer.append(meta)
            phase1_ids.append(written.photo_id)

        index = IndexStore()
        index.load_from_log(log_dir)
        index.save_snapshot(
            log_dir,
            writer.active_segment_name,
            writer.active_segment_offset,
        )

        # Phase 2: write 5 more records AFTER the snapshot
        phase2_ids = []
        for _ in range(5):
            meta = _make_meta()
            written = writer.append(meta)
            phase2_ids.append(written.photo_id)

        writer.close()

        # Simulate restart — load from snapshot + replay
        fresh_index = IndexStore()
        count = fresh_index.load_from_snapshot_and_log(log_dir)

        assert count == 15  # 10 from snapshot + 5 replayed

        # All records from both phases must be present
        for pid in phase1_ids + phase2_ids:
            assert fresh_index.get(pid) is not None, f"Missing photo_id: {pid}"

    def test_snapshot_across_segment_rotation(self, tmp_path):
        """Snapshot in segment 1, new records in segment 2 — all recovered."""
        log_dir = str(tmp_path)
        # Small segments to force rotation
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=512)

        # Write enough to fill segment 1
        phase1_ids = []
        for _ in range(5):
            meta = _make_meta()
            written = writer.append(meta)
            phase1_ids.append(written.photo_id)

        index = IndexStore()
        index.load_from_log(log_dir)
        index.save_snapshot(
            log_dir,
            writer.active_segment_name,
            writer.active_segment_offset,
        )

        # Write more — likely triggers rotation to new segment
        phase2_ids = []
        for _ in range(15):
            meta = _make_meta()
            written = writer.append(meta)
            phase2_ids.append(written.photo_id)

        writer.close()

        # Verify multiple segments exist
        segment_files = [
            f for f in os.listdir(log_dir)
            if f.startswith("segment_") and f.endswith(".log")
        ]
        assert len(segment_files) > 1

        # Restart
        fresh_index = IndexStore()
        count = fresh_index.load_from_snapshot_and_log(log_dir)

        assert count == 20
        for pid in phase1_ids + phase2_ids:
            assert fresh_index.get(pid) is not None


# ─── Test 3: Compaction basic ─────────────────────────────────────────


class TestCompactionBasic:
    """Duplicate records → compact → only latest survives."""

    def test_compact_removes_superseded_records(self, tmp_path):
        """Compaction keeps only the latest record per photo_id."""
        log_dir = str(tmp_path)
        # Small segments to control rotation
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=4096)

        # Write two versions of the same photo
        photo_id = str(uuid.uuid4())
        v1 = _make_meta(photo_id=photo_id, content_hash="hash_v1")
        v2 = _make_meta(photo_id=photo_id, content_hash="hash_v2")

        writer.append(v1)
        writer.append(v2)

        # Also write a unique photo
        unique = _make_meta()
        writer.append(unique)

        # Force rotation so segment_0001 is closed
        writer._rotate_segment()

        # Build index
        index = IndexStore()
        index.load_from_log(log_dir)

        segment_path = os.path.join(log_dir, "segment_0001.log")
        bytes_before = os.path.getsize(segment_path)

        # Compact the closed segment
        compactor = Compactor(log_dir=log_dir, index=index, log_writer=writer)
        result = compactor.compact_segment(segment_path)

        assert result.records_before == 3
        assert result.records_after == 2  # v2 + unique
        assert result.bytes_after < bytes_before

        # Verify the compacted segment replays correctly
        records = list(LogReader.replay_segment(segment_path))
        assert len(records) == 2

        ids = {r.photo_id for r in records}
        assert photo_id in ids
        assert unique.photo_id in ids

        # Verify the surviving record for photo_id has v2's hash
        for r in records:
            if r.photo_id == photo_id:
                assert r.content_hash == "hash_v2"

        writer.close()

    def test_compact_drops_records_superseded_by_later_segment(self, tmp_path):
        """Records superseded by a later segment are dropped entirely."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=4096)

        # Write to segment 1
        photo_id = str(uuid.uuid4())
        v1 = _make_meta(photo_id=photo_id, content_hash="hash_v1")
        writer.append(v1)

        # Force rotation
        writer._rotate_segment()

        # Write newer version to segment 2
        v2 = _make_meta(photo_id=photo_id, content_hash="hash_v2")
        writer.append(v2)

        # Build index (will have v2 pointing to segment_0002)
        index = IndexStore()
        index.load_from_log(log_dir)

        # Compact segment 1 — v1 should be dropped (superseded by v2 in segment 2)
        segment_path = os.path.join(log_dir, "segment_0001.log")
        compactor = Compactor(log_dir=log_dir, index=index, log_writer=writer)
        result = compactor.compact_segment(segment_path)

        assert result.records_before == 1
        assert result.records_after == 0  # All records superseded

        writer.close()


# ─── Test 4: Compaction tombstone ─────────────────────────────────────


class TestCompactionTombstone:
    """Tombstoned records are fully removed by compaction."""

    def test_tombstone_record_dropped_on_compaction(self, tmp_path):
        """A record marked as tombstone is removed during compaction."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=4096)

        # Write original record
        photo_id = str(uuid.uuid4())
        original = _make_meta(photo_id=photo_id, content_hash="hash_orig")
        writer.append(original)

        # Write tombstone for the same photo
        tombstone = _make_meta(
            photo_id=photo_id,
            content_hash="hash_orig",
            tombstone=True,
        )
        writer.append(tombstone)

        # Force rotation
        writer._rotate_segment()

        # Build index — tombstone is the latest record
        index = IndexStore()
        index.load_from_log(log_dir)

        segment_path = os.path.join(log_dir, "segment_0001.log")
        compactor = Compactor(log_dir=log_dir, index=index, log_writer=writer)
        result = compactor.compact_segment(segment_path)

        # Both the original and the tombstone should be gone
        assert result.records_after == 0

        # Verify the compacted file has no records
        records = list(LogReader.replay_segment(segment_path))
        assert len(records) == 0

        writer.close()


# ─── Test 5: Compaction active-segment guard ──────────────────────────


class TestCompactionActiveGuard:
    """Cannot compact the currently active segment."""

    def test_compact_active_segment_raises(self, tmp_path):
        """Attempting to compact the active segment raises RuntimeError."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)
        writer.append(_make_meta())

        index = IndexStore()
        index.load_from_log(log_dir)

        # Try to compact the active segment
        active_path = os.path.join(log_dir, writer.active_segment_name)
        compactor = Compactor(log_dir=log_dir, index=index, log_writer=writer)

        with pytest.raises(RuntimeError, match="Cannot compact the active segment"):
            compactor.compact_segment(active_path)

        writer.close()


# ─── Test 6: Snapshot after compaction ordering ───────────────────────


class TestSnapshotAfterCompaction:
    """Compact → snapshot → restart → correct index."""

    def test_compact_all_then_restart(self, tmp_path):
        """compact_all_closed takes a post-compaction snapshot. Restart is correct."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=512)

        # Write enough records to fill multiple segments
        all_ids = {}
        for _ in range(20):
            meta = _make_meta()
            written = writer.append(meta)
            all_ids[written.photo_id] = written.content_hash

        # Build index
        index = IndexStore()
        index.load_from_log(log_dir)

        # compact_all_closed does compaction + snapshot
        compactor = Compactor(log_dir=log_dir, index=index, log_writer=writer)
        results = compactor.compact_all_closed()

        assert len(results) > 0  # At least some closed segments were compacted

        # Verify snapshot was saved (by compact_all_closed)
        snapshot_path = os.path.join(log_dir, "index-snapshot.bin")
        assert os.path.exists(snapshot_path)

        writer.close()

        # Simulate full restart
        fresh_index = IndexStore()
        count = fresh_index.load_from_snapshot_and_log(log_dir)

        assert count == 20
        for pid, chash in all_ids.items():
            recovered = fresh_index.get(pid)
            assert recovered is not None, f"Missing photo_id: {pid}"
            assert recovered.content_hash == chash


# ─── Test 7: Stale watermark after compaction ─────────────────────────


class TestStaleWatermark:
    """Compacted offsets → stale watermark → CRC catches it safely."""

    def test_stale_watermark_graceful_degradation(self, tmp_path):
        """If snapshot watermark references pre-compaction offsets,
        the CRC32 check catches the mid-record seek and partial replay
        fails safely — the snapshot data is still valid."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir, max_segment_bytes=4096)

        # Write records with duplicates
        photo_id_a = str(uuid.uuid4())
        writer.append(_make_meta(photo_id=photo_id_a, content_hash="ha_v1"))
        writer.append(_make_meta(photo_id=photo_id_a, content_hash="ha_v2"))

        unique_b = _make_meta()
        writer.append(unique_b)

        # Build index and take snapshot BEFORE compaction
        index = IndexStore()
        index.load_from_log(log_dir)

        index.save_snapshot(
            log_dir,
            writer.active_segment_name,
            writer.active_segment_offset,
        )

        # Force rotation so segment_0001 is closed
        writer._rotate_segment()

        # Compact segment_0001 — offsets change, but we DON'T re-snapshot
        segment_path = os.path.join(log_dir, "segment_0001.log")
        compactor = Compactor(log_dir=log_dir, index=index, log_writer=writer)
        # Compact manually (not compact_all_closed which would re-snapshot)
        compactor.compact_segment(segment_path)

        writer.close()

        # Restart with the stale snapshot — watermark offset is pre-compaction
        fresh_index = IndexStore()
        count = fresh_index.load_from_snapshot_and_log(log_dir)

        # The snapshot itself has all 2 unique photos.
        # Partial replay from stale watermark may fail (CRC mismatch) but
        # the snapshot data is still valid. We should have at least the
        # snapshot's records.
        assert count >= 2
        assert fresh_index.get(photo_id_a) is not None
        assert fresh_index.get(unique_b.photo_id) is not None


# ─── Test 8: Concurrent snapshot safety ───────────────────────────────


class TestConcurrentSnapshot:
    """put() during save_snapshot() doesn't crash or corrupt."""

    def test_put_during_snapshot_no_crash(self, tmp_path):
        """Concurrent put() calls while save_snapshot() runs — no crash."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        # Pre-populate
        for _ in range(100):
            meta = _make_meta()
            written = writer.append(meta)

        index = IndexStore()
        index.load_from_log(log_dir)
        assert index.count == 100

        errors = []

        def snapshot_worker():
            try:
                index.save_snapshot(
                    log_dir,
                    writer.active_segment_name,
                    writer.active_segment_offset,
                )
            except Exception as exc:
                errors.append(exc)

        def put_worker():
            try:
                for _ in range(50):
                    meta = _make_meta()
                    written = writer.append(meta)
                    index.put(written)
            except Exception as exc:
                errors.append(exc)

        # Run both concurrently
        t_snap = threading.Thread(target=snapshot_worker)
        t_put = threading.Thread(target=put_worker)

        t_snap.start()
        t_put.start()

        t_snap.join(timeout=10)
        t_put.join(timeout=10)

        assert not errors, f"Concurrent operations raised errors: {errors}"

        # The snapshot file should exist and be valid JSON
        snapshot_path = os.path.join(log_dir, "index-snapshot.bin")
        assert os.path.exists(snapshot_path)

        with open(snapshot_path, "rb") as f:
            data = json.loads(f.read())

        # The snapshot should have at least 100 records (pre-populate)
        # and possibly up to 150 (if put_worker records were captured)
        assert len(data["records"]) >= 100

        writer.close()


# ─── Test: LogWriter snapshot trigger counter ─────────────────────────


class TestSnapshotTrigger:
    """LogWriter tracks append count and signals when snapshot is due."""

    def test_snapshot_due_after_threshold(self, tmp_path):
        """snapshot_due becomes True after 1000 appends."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        assert writer.snapshot_due is False

        # Write 999 records — not yet due
        for _ in range(999):
            writer.append(_make_meta())
        assert writer.snapshot_due is False

        # Write the 1000th — now due
        writer.append(_make_meta())
        assert writer.snapshot_due is True

        # Reset
        writer.reset_snapshot_counter()
        assert writer.snapshot_due is False

        writer.close()

    def test_active_segment_properties(self, tmp_path):
        """active_segment_name and active_segment_offset are accurate."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        assert writer.active_segment_name == "segment_0001.log"
        assert writer.active_segment_offset == 0

        writer.append(_make_meta())
        assert writer.active_segment_offset > 0

        writer.close()


# ─── Test: replay_segment_from_offset ─────────────────────────────────


class TestReplayFromOffset:
    """LogReader.replay_segment_from_offset skips records before the offset."""

    def test_replay_from_midpoint(self, tmp_path):
        """Replay from midpoint skips earlier records."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        # Write 5 records, track offsets
        written = []
        for _ in range(5):
            meta = _make_meta()
            w = writer.append(meta)
            written.append(w)

        writer.close()

        segment_path = os.path.join(log_dir, "segment_0001.log")

        # Replay from the 3rd record's offset — should yield records 3, 4, 5
        start_offset = written[2].offset
        records = list(
            LogReader.replay_segment_from_offset(segment_path, start_offset)
        )

        assert len(records) == 3
        assert records[0].photo_id == written[2].photo_id
        assert records[1].photo_id == written[3].photo_id
        assert records[2].photo_id == written[4].photo_id

    def test_replay_from_end_yields_nothing(self, tmp_path):
        """Replay from end of file yields zero records."""
        log_dir = str(tmp_path)
        writer = LogWriter(log_dir=log_dir)

        writer.append(_make_meta())
        end_offset = writer.active_segment_offset
        writer.close()

        segment_path = os.path.join(log_dir, "segment_0001.log")
        records = list(
            LogReader.replay_segment_from_offset(segment_path, end_offset)
        )
        assert len(records) == 0
