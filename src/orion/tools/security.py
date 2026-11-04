"""Filesystem security validation for read-only Windows tools."""

from pathlib import Path
from orion.config.settings import get_settings


def validate_safe_path(target_path: str | Path, must_exist: bool = True) -> Path:
    """Validate and resolve canonical path against allowed directories.

    Raises PermissionError or FileNotFoundError if:
    - Filesystem access is not enabled/configured.
    - Path resolves outside configured allowed directories (traversal/symlink escape).
    - Path does not exist when must_exist is True.
    """
    settings = get_settings()
    allowed_dirs = settings.get_allowed_directories()

    if not allowed_dirs:
        raise PermissionError(
            "Filesystem access is disabled. No directories configured in ORION_ALLOWED_DIRECTORIES."
        )

    raw_path = Path(target_path)

    # Resolve to canonical absolute path to block traversal and symlink/junction escapes
    try:
        resolved = raw_path.resolve(strict=must_exist)
    except FileNotFoundError:
        raise FileNotFoundError(f"File or directory does not exist: {target_path}")

    # Check if resolved path is strictly within any allowed directory
    is_allowed = False
    for allowed in allowed_dirs:
        try:
            resolved.relative_to(allowed)
            is_allowed = True
            break
        except ValueError:
            continue

    if not is_allowed:
        raise PermissionError("Access denied: path is outside permitted directories.")

    return resolved
