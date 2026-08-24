#!/usr/bin/env python3
"""Initialize and verify storage directories for Pixel Vault."""

import os
import sys
import tempfile

REQUIRED_DIRS = [
    "/storage/blobs",
    "/storage/logs",
    "/storage/thumbs",
    "/storage/tmp",
]


def init_storage() -> bool:
    all_ok = True
    print("Checking / initializing storage directories...")

    for path in REQUIRED_DIRS:
        try:
            os.makedirs(path, exist_ok=True)
            # Verify write access by creating and removing a test file
            test_file_path = os.path.join(path, f".write_test_{os.getpid()}")
            with open(test_file_path, "w") as f:
                f.write("ok")
            os.remove(test_file_path)
            print(f"  [OK] {path} (exists and writable)")
        except Exception as exc:
            print(f"  [FAIL] {path}: {exc}", file=sys.stderr)
            all_ok = False

    return all_ok


if __name__ == "__main__":
    success = init_storage()
    if not success:
        sys.exit(1)
    print("Storage directory initialization complete.")
