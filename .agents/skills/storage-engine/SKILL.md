---
name: storage-engine
description: >-
  Implements and modifies the custom photo metadata storage engine — the append-only log, in-memory index, and content-addressed blob store — for the private photo server. Use when writing or editing code in the storage module, the log writer or reader, the in-memory index, compaction, crash recovery, or blob storage logic.
---

# Storage Engine Skill

This project intentionally hand-builds a Bitcask-style storage engine instead of using a database, for learning and resume value. Correctness here matters more than in any other part of the codebase — treat every change in this module as touching durability-critical logic.

## Non-negotiable Invariants

Never violate these, even to fix a bug faster or simplify a diff:

1. **The log is append-only.** Never open a segment file in a mode that edits or overwrites existing bytes. An update to a photo's metadata is always a brand-new, complete record appended to the end — never a patch to an old record.
2. **Single writer.** Only the API server's write path appends to the log. Background workers (thumbnail generation, EXIF backfill) must call back into that same write path rather than opening the log file directly. If you find yourself writing code that opens a log segment for append outside the designated writer function, stop — that's the bug class to avoid.
3. **Write ordering is fixed:** write blob to temp file → hash → check for duplicate → atomic rename into the blob store → append metadata record to log → update in-memory index. Never update the index before the log append has succeeded. Never reverse blob-write and log-append.
4. **Every log record carries a checksum of its own bytes.** This checksum exists solely to detect a torn write during crash-recovery replay — it is unrelated to the content hash used for photo identity/dedup. Don't conflate the two.
5. **The in-memory index holds full metadata records**, not just pointers — `photo_id -> {content_hash, taken_at, camera, gps, thumbnail_paths, segment_id, offset, length, ...}`. The `segment_id`/`offset`/`length` fields exist for crash-recovery bookkeeping, not to serve reads. A read is a pure hashmap lookup; it should never need to seek into a log file during normal operation.
6. **Never rewrite the entire index to disk on every write.** Every individual write is a single small append to the log. The full in-memory index is dumped to `index-snapshot.bin` only periodically (size- or time-triggered), via temp-file-then-atomic-rename — never overwritten in place.
7. **Compaction only touches closed segments**, never the currently-active one being appended to. The compacted replacement segment must be written in full, then swapped in via atomic rename, with index pointers updated only after the rename succeeds — never partway through.

## Decision Tree

- **Changing the log record format** (adding a field, changing encoding) → you must update the recovery/replay logic and the compaction logic in the same change. They all read the same on-disk format and must agree.
- **Changing the index structure** → confirm it's still fully rebuildable by replaying the log from a snapshot. If a new index field can't be derived purely from log records, that's a design bug, not an implementation detail to fix later.
- **Adding a new query pattern** (e.g. search by tag) → add a new in-memory secondary index, mirroring how the date-sorted index works. Don't repurpose the primary `photo_id` index or change the blob store's on-disk layout to support a query need — those two things stay query-agnostic.

## Testing Requirements Before This Module Is Considered "Done" for a Given Change

- **Round-trip test:** write N records, simulate a restart (load snapshot + replay log), verify the resulting index is identical to pre-restart state.
- **Torn-write test:** truncate the last record of a segment mid-byte, run replay, verify it stops cleanly at the checksum mismatch and doesn't corrupt or lose any earlier, valid records.
- **Compaction test:** compact a segment containing superseded records, verify query results are byte-identical before and after compaction, and verify dead space was actually reclaimed on disk.
- **Single-writer check:** confirm no code path outside the designated write function ever opens a log segment in write or append mode.

Run this test suite after any change touching this module — not just once at the end of a build session.

## Reference

Full narrative design doc with rationale, rejected alternatives, and how this maps to real systems (Bitcask, LSM-trees, Kafka) lives at `docs/photo_storage_system_design.md` — read it for the "why" behind any of the above if a design question comes up that isn't covered here.
