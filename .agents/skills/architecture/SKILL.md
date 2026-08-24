---
name: architecture
description: >-
  Comprehensive guide to the Private Photo Cloud system architecture, Bitcask-style storage engine, in-memory indexing structures, and read/write workflows. Use when implementing, refactoring, or querying storage mechanisms, log serialization, deduplication, indexing, or photo upload and retrieval pipelines.
---

# System Architecture: Private Photo Cloud

This skill defines the core architecture, storage engine specifications, indexing structures, and data flows for the Private Photo Cloud server.

---

## 1. Directory Structure (The Database)

The system uses the physical hard drive as a Bitcask-style storage engine:
* `/storage/blobs/`: Stores immutable images. Filename is **EXACTLY** the SHA-256 hash of the file (no file extensions).
* `/storage/logs/`: Contains `segment.log` files. This is an append-only JSONL (JSON Lines) ledger of all metadata.
* `/storage/thumbs/`: Stores resized web-friendly thumbnails, named by the original file's SHA-256 hash.

---

## 2. In-Memory Index (State)

On server startup, the API reads all `/storage/logs/segment_NNNN.log` files sequentially and reconstructs the in-memory index:
1. **`HashMap`**: `photo_id` -> `PhotoMeta(content_hash, taken_at, camera, gps, thumbnail_paths, uploaded_at, segment_id, offset, length)` — Full metadata records in RAM. Reads are pure hashmap lookups with zero disk I/O. The `segment_id`/`offset`/`length` fields exist for crash-recovery bookkeeping.
2. **`HashIndex`**: `content_hash` -> `photo_id` — Secondary index for O(1) deduplication checks during upload.
3. **`DateTree`** *(future)*: `taken_at` -> `[photo_id, photo_id, ...]` — Deferred to a later phase.


---

## 3. Write Flow (Upload & Deduplication)

When handling incoming `POST /photos` requests:

1. **Stream & Hash**: Read the incoming request in chunked streams (e.g., 1MB chunks to protect RAM). Write chunks to a temporary file (`tmp/<uuid>.tmp`) while simultaneously updating a `hashlib.sha256()` hash digest.
2. **Dedup Check**: When streaming completes, check if the calculated SHA-256 hash exists in the RAM index:
   * If yes: Delete the temporary file and return `200 OK` (idempotency).
3. **Commit**: If new, atomically move/rename the temp file to `/storage/blobs/<hash>` via `os.rename()`.
4. **EXIF Extraction**: Read the committed blob's header synchronously to extract `taken_at`, `camera`, and related metadata.
5. **Log Append**: Serialize the photo metadata to JSON. Append it to the active `segment.log` file. Record the exact byte offset and length where this entry was written.
6. **Index Update**: Insert the new record into both the in-memory `HashMap` and `DateTree`.
7. **Background Task**: Enqueue background worker task for thumbnail generation.

---

## 4. Read Flow (Retrieval)

When handling `GET /photos/{id}` requests:

1. **Index Lookup**: Query `photo_id` in the RAM `HashMap` to retrieve `(segment_id, offset, length)`.
2. **Segment Seek**: Open the designated segment log file, perform `file.seek(offset)`, and read the slice (`file.read(length)`).
3. **Metadata Deserialization**: Deserialize the raw JSON record into the response model.
4. **Blob Delivery**: When serving the actual image binary, stream the file directly from `/storage/blobs/<hash>` using FastAPI's `FileResponse`.
