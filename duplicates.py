"""
Duplicate file finder - groups by size, verifies with hash.
Uses the existing QuickFind index for fast initial grouping.
"""
import hashlib
import os
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, List, Optional

from indexer import DEFAULT_DB, get_db


@dataclass
class DuplicateGroup:
    size: int
    hash: str
    files: List[str]

    @property
    def wasted_bytes(self) -> int:
        return self.size * (len(self.files) - 1)


def file_hash(filepath: str) -> Optional[str]:
    """MD5 hash of a file. Returns None if file can't be read."""
    try:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while chunk := f.read(1024 * 1024):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def find_duplicates(
    db_path: str = DEFAULT_DB,
    min_size: int = 1,
    scan_paths: Optional[List[str]] = None,
    callback: Optional[Callable] = None,
    num_workers: int = 8,
) -> List[DuplicateGroup]:
    """
    Find duplicate files using the index.

    Steps:
    1. Group files by size (from index - instant)
    2. For groups with 2+ files, compute hash (parallel)
    3. Group by hash - identical hash = duplicate

    Args:
        db_path: Path to QuickFind database
        min_size: Minimum file size to consider (skip 0-byte files)
        scan_paths: Only consider files under these paths (None = all)
        callback: Called with (stage, progress, total) for updates
        num_workers: Parallel hash workers
    """
    conn = get_db(db_path)

    # Step 1: Group by size from index
    if callback:
        callback("Grouping by size...", 0, 0)

    query = "SELECT path, size FROM files WHERE is_dir=0 AND size >= ?"
    params = [min_size]

    rows = conn.execute(query, params).fetchall()

    size_groups = defaultdict(list)
    for path, size in rows:
        if scan_paths:
            if not any(path.startswith(p) for p in scan_paths):
                continue
        size_groups[size].append(path)

    # Filter to only groups with 2+ files (potential duplicates)
    candidates = {size: paths for size, paths in size_groups.items() if len(paths) >= 2}

    if callback:
        total_to_hash = sum(len(paths) for paths in candidates.values())
        callback("Hashing candidates...", 0, total_to_hash)

    # Step 2: Hash files in candidate groups
    hash_groups = defaultdict(list)
    hashed = 0

    def hash_file(filepath):
        return filepath, file_hash(filepath)

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        all_files = [(path, size) for size, paths in candidates.items() for path in paths]
        futures = [pool.submit(hash_file, path) for path, _ in all_files]

        for future in futures:
            filepath, h = future.result()
            if h:
                # Find size for this file
                size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                hash_groups[(h, size)].append(filepath)
            hashed += 1
            if callback and hashed % 100 == 0:
                callback("Hashing...", hashed, len(all_files))

    # Step 3: Filter to actual duplicates (2+ files with same hash)
    duplicates = []
    for (h, size), paths in hash_groups.items():
        if len(paths) >= 2:
            # Verify files still exist
            existing = [p for p in paths if os.path.exists(p)]
            if len(existing) >= 2:
                duplicates.append(DuplicateGroup(size=size, hash=h, files=existing))

    # Sort by wasted space (biggest waste first)
    duplicates.sort(key=lambda d: d.wasted_bytes, reverse=True)

    if callback:
        callback("Done", len(duplicates), len(duplicates))

    return duplicates


def delete_files(paths: List[str]) -> tuple:
    """Delete files. Returns (deleted_count, failed_count)."""
    deleted = 0
    failed = 0
    for path in paths:
        try:
            os.remove(path)
            deleted += 1
        except (OSError, PermissionError):
            failed += 1
    return deleted, failed
