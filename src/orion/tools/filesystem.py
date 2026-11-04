"""Read-only filesystem inspection tools restricted to allowed directories."""

from datetime import datetime
import os
from pathlib import Path
from typing import Any

from orion.tools.security import validate_safe_path

MAX_LIST_ITEMS = 50
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_DEPTH = 3


def list_files(directory: str) -> dict[str, Any]:
    """List directory entries within an approved folder (non-recursive, max 50 items)."""
    resolved = validate_safe_path(directory)
    if not resolved.is_dir():
        raise NotADirectoryError(f"Specified path is not a directory: {resolved.name}")

    items: list[dict[str, Any]] = []
    total_found = 0

    with os.scandir(resolved) as scanner:
        for entry in scanner:
            total_found += 1
            if len(items) < MAX_LIST_ITEMS:
                try:
                    stat_info = entry.stat()
                    items.append(
                        {
                            "name": entry.name,
                            "is_dir": entry.is_dir(),
                            "size_bytes": stat_info.st_size if entry.is_file() else None,
                        }
                    )
                except OSError:
                    items.append({"name": entry.name, "is_dir": entry.is_dir(), "size_bytes": None})

    return {
        "directory": resolved.name,
        "count": len(items),
        "total_in_folder": total_found,
        "items": items,
        "truncated": total_found > MAX_LIST_ITEMS,
    }


def find_files(query: str, directory: str) -> dict[str, Any]:
    """Search for files by filename matching query inside an approved directory."""
    resolved = validate_safe_path(directory)
    if not resolved.is_dir():
        raise NotADirectoryError(f"Specified path is not a directory: {resolved.name}")

    query_lower = query.strip().lower()
    matches: list[dict[str, Any]] = []
    base_depth = len(resolved.parts)

    for root, dirs, files in os.walk(resolved):
        cur_depth = len(Path(root).parts) - base_depth
        if cur_depth >= MAX_SEARCH_DEPTH:
            dirs.clear()  # Do not recurse deeper

        for filename in files:
            if query_lower in filename.lower():
                full_p = Path(root) / filename
                rel_p = str(full_p.relative_to(resolved))
                matches.append({"name": filename, "relative_path": rel_p})
                if len(matches) >= MAX_SEARCH_RESULTS:
                    return {
                        "directory": resolved.name,
                        "query": query,
                        "count": len(matches),
                        "matches": matches,
                        "truncated": True,
                    }

    return {
        "directory": resolved.name,
        "query": query,
        "count": len(matches),
        "matches": matches,
        "truncated": False,
    }


def get_file_info(path: str) -> dict[str, Any]:
    """Retrieve metadata (size, timestamps, type) for an approved file or directory without reading content."""
    resolved = validate_safe_path(path)
    stat = resolved.stat()

    return {
        "name": resolved.name,
        "is_dir": resolved.is_dir(),
        "size_bytes": stat.st_size if resolved.is_file() else None,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "created_at": datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
    }
