"""Secure remote file bridge protocol (Fetch & Drop) for ORION (Step 22)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("orion.tools.file_bridge")

# Sensitive extensions disallowed from remote transfer
SENSITIVE_EXTENSIONS = {".env", ".pem", ".key", ".db", ".sqlite", ".sqlite3"}


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
) -> Path | None:
    """Get the most recently modified file in the directory matching the extension."""
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
                candidates.append((f.stat().st_mtime, f))
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
) -> Path | None:
    """Find a specific file in the given alias or across approved directories."""
    safe_name = os.path.basename(filename).strip()
    if not safe_name:
        return None

    raw_path = Path(filename)
    if ".." in raw_path.parts:
        return None

    if is_sensitive_file(safe_name):
        return None

    if directory_alias:
        target_dir = resolve_directory_alias(directory_alias)
        candidate = (target_dir / safe_name).resolve()
        if candidate.is_file() and is_path_allowed(candidate, extra_allowed=[target_dir]):
            return candidate
        return None

    for d in get_approved_directories():
        candidate = (d / safe_name).resolve()
        if candidate.is_file() and is_path_allowed(candidate):
            return candidate

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
