"""Controlled Windows file operations requiring explicit user confirmation.

Strict restrictions:
- All paths must be within approved directories.
- No deletion, overwriting, or replacing of existing files.
- No recursive directory moves or copies.
- Symlinks and junctions are strictly rejected.
- Regular files only.
"""

import os
from pathlib import Path
import shutil
from typing import Any

from orion.tools.security import resolve_folder_alias, validate_safe_path

MAX_TEXT_FILE_BYTES = 1_048_576  # 1 MB maximum for safety


def create_directory(path: str) -> dict[str, Any]:
    """Create a new directory inside an approved directory.

    Rejects creation if the directory already exists (no overwrite).
    """
    dest_path = validate_safe_path(path, must_exist=False)

    if dest_path.exists():
        raise FileExistsError(f"Directory already exists: {dest_path}")

    if not dest_path.parent.exists() or not dest_path.parent.is_dir():
        raise NotADirectoryError(f"Parent directory does not exist: {dest_path.parent}")

    dest_path.mkdir(parents=False, exist_ok=False)

    return {
        "path": str(dest_path),
        "name": dest_path.name,
        "parent": str(dest_path.parent),
    }


def copy_file(source: str, destination: str) -> dict[str, Any]:
    """Copy a regular file inside approved directories.

    Rejects existing destinations, symlinks/junctions, and non-regular files.
    """
    src_path = validate_safe_path(source, must_exist=True)

    if src_path.is_symlink() or os.path.islink(src_path):
        raise PermissionError("Symlinks and junctions are not permitted.")

    if not src_path.is_file():
        raise ValueError(f"Source must be a regular file: {src_path}")

    # If destination refers to an existing directory or folder alias, target file inside it
    dest_alias = resolve_folder_alias(destination)
    candidate_dest = Path(dest_alias)
    if candidate_dest.exists() and candidate_dest.is_dir():
        dest_target = candidate_dest / src_path.name
    else:
        dest_target = candidate_dest

    dest_path = validate_safe_path(dest_target, must_exist=False)

    if dest_path.exists():
        raise FileExistsError(f"Destination already exists: {dest_path}")

    if not dest_path.parent.exists() or not dest_path.parent.is_dir():
        raise NotADirectoryError(f"Destination parent directory does not exist: {dest_path.parent}")

    shutil.copy2(src_path, dest_path)

    return {
        "source": str(src_path),
        "destination": str(dest_path),
        "name": dest_path.name,
        "size_bytes": dest_path.stat().st_size,
    }


def move_file(source: str, destination: str) -> dict[str, Any]:
    """Move a regular file inside approved directories.

    Rejects existing destinations, symlinks/junctions, and directory moves.
    """
    src_path = validate_safe_path(source, must_exist=True)

    if src_path.is_symlink() or os.path.islink(src_path):
        raise PermissionError("Symlinks and junctions are not permitted.")

    if not src_path.is_file():
        raise ValueError(f"Source must be a regular file: {src_path}")

    # If destination refers to an existing directory or folder alias, target file inside it
    dest_alias = resolve_folder_alias(destination)
    candidate_dest = Path(dest_alias)
    if candidate_dest.exists() and candidate_dest.is_dir():
        dest_target = candidate_dest / src_path.name
    else:
        dest_target = candidate_dest

    dest_path = validate_safe_path(dest_target, must_exist=False)

    if dest_path.exists():
        raise FileExistsError(f"Destination already exists: {dest_path}")

    if not dest_path.parent.exists() or not dest_path.parent.is_dir():
        raise NotADirectoryError(f"Destination parent directory does not exist: {dest_path.parent}")

    shutil.move(str(src_path), str(dest_path))

    return {
        "source": str(src_path),
        "destination": str(dest_path),
        "name": dest_path.name,
        "size_bytes": dest_path.stat().st_size,
    }


def rename_file(source: str, new_name: str) -> dict[str, Any]:
    """Rename a regular file within its parent directory.

    Rejects existing destinations, path traversals in new_name, and symlinks.
    """
    src_path = validate_safe_path(source, must_exist=True)

    if src_path.is_symlink() or os.path.islink(src_path):
        raise PermissionError("Symlinks and junctions are not permitted.")

    if not src_path.is_file():
        raise ValueError(f"Source must be a regular file: {src_path}")

    clean_name = new_name.strip().strip("'\"")
    if not clean_name:
        raise ValueError("New filename cannot be empty.")

    if "/" in clean_name or "\\" in clean_name or ".." in clean_name:
        raise ValueError("new_name must be a simple filename without path separators or traversal.")

    dest_path = validate_safe_path(src_path.parent / clean_name, must_exist=False)

    if dest_path.exists():
        raise FileExistsError(f"Destination already exists: {dest_path}")

    src_path.rename(dest_path)

    return {
        "source": str(src_path),
        "destination": str(dest_path),
        "old_name": src_path.name,
        "new_name": dest_path.name,
        "size_bytes": dest_path.stat().st_size,
    }


def create_text_file(path: str, content: str) -> dict[str, Any]:
    """Create a new UTF-8 text file inside an approved directory.

    Rejects existing files (no overwrite) and enforces a 1 MB file size limit.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_TEXT_FILE_BYTES:
        raise ValueError(f"File content exceeds maximum allowed size of {MAX_TEXT_FILE_BYTES} bytes.")

    dest_path = validate_safe_path(path, must_exist=False)

    if dest_path.exists():
        raise FileExistsError(f"File already exists: {dest_path}")

    if not dest_path.parent.exists() or not dest_path.parent.is_dir():
        raise NotADirectoryError(f"Parent directory does not exist: {dest_path.parent}")

    with open(dest_path, "x", encoding="utf-8") as f:
        f.write(content)

    return {
        "path": str(dest_path),
        "name": dest_path.name,
        "size_bytes": len(encoded),
    }
