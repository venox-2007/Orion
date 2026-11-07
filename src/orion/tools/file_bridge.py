"""Secure remote file bridge protocol (Fetch & Drop) for ORION (Step 22)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("orion.tools.file_bridge")

# Sensitive extensions disallowed from remote transfer
SENSITIVE_EXTENSIONS = {".env", ".pem", ".key", ".db", ".sqlite", ".sqlite3"}

# Telegram file upload guard (bot limit is 50MB, guard threshold 48MB)
MAX_FILE_BYTES = 48 * 1024 * 1024


class FileTooLargeError(Exception):
    """Raised when an approved file exceeds Telegram's upload limit."""

    def __init__(self, path: Path, size_bytes: int):
        self.path = path
        self.size_bytes = size_bytes
        super().__init__(
            f"File '{path.name}' exceeds maximum upload size ({size_bytes / (1024 * 1024):.1f} MB, limit is 50 MB)."
        )


def get_approved_directories() -> list[Path]:
    """Return allowlisted base directories for file access in priority order."""
    home = Path.home().resolve()
    dirs = [
        (home / "Downloads").resolve(),
        (home / "Documents").resolve(),
        (home / "Desktop").resolve(),
        Path.cwd().resolve(),
    ]
    unique_dirs: list[Path] = []
    seen = set()
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique_dirs.append(d)
    return unique_dirs


def resolve_directory_alias(directory_alias: str | Path = "downloads") -> Path:
    """Resolve a directory alias or custom path to a resolved Path object."""
    if isinstance(directory_alias, Path):
        return directory_alias.resolve()

    alias = str(directory_alias).strip().lower()
    home = Path.home().resolve()
    alias_map = {
        "downloads": (home / "Downloads").resolve(),
        "download": (home / "Downloads").resolve(),
        "documents": (home / "Documents").resolve(),
        "document": (home / "Documents").resolve(),
        "docs": (home / "Documents").resolve(),
        "desktop": (home / "Desktop").resolve(),
        "workspace": Path.cwd().resolve(),
        "project": Path.cwd().resolve(),
    }
    if alias in alias_map:
        return alias_map[alias]

    return Path(directory_alias).resolve()


def is_sensitive_file(path: Path | str) -> bool:
    """Check if a file is hidden, system, or has a sensitive extension."""
    p = Path(path)
    name = p.name.lower()
    if name.startswith("."):
        return True
    for ext in SENSITIVE_EXTENSIONS:
        if name.endswith(ext):
            return True
    return False


def is_path_allowed(path: Path | str, extra_allowed: list[Path] | None = None) -> bool:
    """Verify that a path is strictly inside approved directories and is not sensitive."""
    try:
        resolved = Path(path).resolve()
    except Exception:
        return False

    raw_parts = Path(path).parts
    if ".." in raw_parts:
        return False

    if is_sensitive_file(resolved):
        return False

    allowed_dirs = get_approved_directories()
    if extra_allowed:
        allowed_dirs = list(allowed_dirs) + list(extra_allowed)

    for allowed in allowed_dirs:
        try:
            if resolved.is_relative_to(allowed.resolve()):
                return True
        except (ValueError, AttributeError):
            continue

    return False


def validate_approved_path(
    path: Path | str,
    extra_allowed: list[Path] | None = None,
    raise_error: bool = False,
) -> Path | None:
    """Validate that path is authorized. Returns resolved Path or None / raises PermissionError."""
    p = Path(path)
    if not is_path_allowed(p, extra_allowed=extra_allowed):
        if raise_error:
            raise PermissionError(f"Access to file '{path}' is denied or outside approved directories.")
        return None
    return p.resolve()


def get_latest_file(
    directory_alias: str | Path = "downloads",
    extension: str | None = None,
    max_bytes: int = MAX_FILE_BYTES,
) -> Path | None:
    """Get the most recently modified file in the directory matching the extension and size limit."""
    target_dir = resolve_directory_alias(directory_alias)
    if not target_dir.exists() or not target_dir.is_dir():
        return None

    norm_ext = None
    if extension:
        ext = extension.strip().lower()
        if ext.startswith("*"):
            ext = ext[1:]
        if not ext.startswith("."):
            ext = f".{ext}"
        norm_ext = ext

    candidates: list[tuple[float, Path]] = []
    try:
        for f in target_dir.iterdir():
            try:
                if not f.is_file():
                    continue
                if is_sensitive_file(f):
                    continue
                if norm_ext and f.suffix.lower() != norm_ext:
                    continue
                st = f.stat()
                if max_bytes is not None and st.st_size > max_bytes:
                    continue
                candidates.append((st.st_mtime, f))
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1].resolve()


def find_approved_file(
    filename: str,
    directory_alias: str | Path | None = None,
    max_bytes: int | None = None,
) -> Path | None:
    """Find a specific file in the given alias or across approved directories.

    Supports:
    1. Exact filename matches.
    2. Case-insensitive stem matching (e.g. "EAadhar" -> "EAadhar.pdf").
    3. Case-insensitive prefix matching (e.g. "EAadhar" -> "EAadhar_00000.pdf").
    4. Case-insensitive substring matching.
    """
    safe_name = os.path.basename(filename).strip()
    if not safe_name:
        return None

    raw_path = Path(filename)
    if ".." in raw_path.parts:
        return None

    if is_sensitive_file(safe_name):
        return None

    dirs_to_search: list[Path] = []
    if directory_alias:
        target_dir = resolve_directory_alias(directory_alias)
        dirs_to_search = [target_dir]
    else:
        dirs_to_search = get_approved_directories()

    query_lower = safe_name.lower()
    query_stem_lower = Path(safe_name).stem.lower()

    # Pass 1: Exact filename match (case-insensitive)
    for d in dirs_to_search:
        if not d.exists() or not d.is_dir():
            continue
        try:
            for f in d.iterdir():
                try:
                    if not f.is_file():
                        continue
                    if is_sensitive_file(f):
                        continue
                    if f.name.lower() == query_lower:
                        cand = f.resolve()
                        if is_path_allowed(cand, extra_allowed=dirs_to_search):
                            if max_bytes is not None and cand.stat().st_size > max_bytes:
                                raise FileTooLargeError(cand, cand.stat().st_size)
                            return cand
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue

    # Pass 2: Stem match (f.stem.lower() == query_stem_lower or f.stem.lower() == query_lower)
    for d in dirs_to_search:
        if not d.exists() or not d.is_dir():
            continue
        try:
            for f in d.iterdir():
                try:
                    if not f.is_file():
                        continue
                    if is_sensitive_file(f):
                        continue
                    f_stem_lower = f.stem.lower()
                    if f_stem_lower == query_stem_lower or f_stem_lower == query_lower:
                        cand = f.resolve()
                        if is_path_allowed(cand, extra_allowed=dirs_to_search):
                            if max_bytes is not None and cand.stat().st_size > max_bytes:
                                raise FileTooLargeError(cand, cand.stat().st_size)
                            return cand
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue

    # Pass 3: Prefix match (f.name starts with query or f.stem starts with query_stem)
    for d in dirs_to_search:
        if not d.exists() or not d.is_dir():
            continue
        try:
            for f in d.iterdir():
                try:
                    if not f.is_file():
                        continue
                    if is_sensitive_file(f):
                        continue
                    f_stem_lower = f.stem.lower()
                    f_name_lower = f.name.lower()
                    if f_stem_lower.startswith(query_stem_lower) or f_name_lower.startswith(query_lower):
                        cand = f.resolve()
                        if is_path_allowed(cand, extra_allowed=dirs_to_search):
                            if max_bytes is not None and cand.stat().st_size > max_bytes:
                                raise FileTooLargeError(cand, cand.stat().st_size)
                            return cand
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue

    # Pass 4: Substring match
    for d in dirs_to_search:
        if not d.exists() or not d.is_dir():
            continue
        try:
            for f in d.iterdir():
                try:
                    if not f.is_file():
                        continue
                    if is_sensitive_file(f):
                        continue
                    if query_stem_lower in f.stem.lower() or query_lower in f.name.lower():
                        cand = f.resolve()
                        if is_path_allowed(cand, extra_allowed=dirs_to_search):
                            if max_bytes is not None and cand.stat().st_size > max_bytes:
                                raise FileTooLargeError(cand, cand.stat().st_size)
                            return cand
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue

    return None


def save_incoming_file(
    file_bytes: bytes,
    filename: str,
    target_dir: str | Path = "downloads",
) -> Path:
    """Save an incoming file from Telegram to the laptop, avoiding collisions."""
    safe_name = os.path.basename(filename).strip()
    if not safe_name:
        safe_name = "received_file"

    dest_dir = resolve_directory_alias(target_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    target_path = dest_dir / safe_name
    if target_path.exists():
        stem = target_path.stem
        suffix = target_path.suffix
        counter = 1
        while True:
            candidate = dest_dir / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                target_path = candidate
                break
            counter += 1

    target_path.write_bytes(file_bytes)
    return target_path.resolve()
