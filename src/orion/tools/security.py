"""Filesystem security validation and Windows folder alias resolution."""

import os
from pathlib import Path
import re
import sys
from typing import Dict
from orion.config.settings import get_settings


def get_known_windows_folders() -> dict[str, Path]:
    """Resolve actual Windows user directories, accounting for redirected OneDrive folders."""
    home = Path.home().resolve()
    folders: dict[str, Path] = {
        "home": home,
        "downloads": (home / "Downloads").resolve(),
        "documents": (home / "Documents").resolve(),
        "desktop": (home / "Desktop").resolve(),
    }

    # Query Windows Shell Folders via registry when on Windows
    if sys.platform == "win32":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                # Downloads known folder GUID: {374DE290-123F-4565-9164-39C4925E467B}
                for reg_key in ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"):
                    try:
                        val, _ = winreg.QueryValueEx(key, reg_key)
                        expanded = os.path.expandvars(val)
                        folders["downloads"] = Path(expanded).resolve()
                        break
                    except OSError:
                        pass

                # Documents / Personal folder
                try:
                    val, _ = winreg.QueryValueEx(key, "Personal")
                    expanded = os.path.expandvars(val)
                    folders["documents"] = Path(expanded).resolve()
                except OSError:
                    pass

                # Desktop folder
                try:
                    val, _ = winreg.QueryValueEx(key, "Desktop")
                    expanded = os.path.expandvars(val)
                    folders["desktop"] = Path(expanded).resolve()
                except OSError:
                    pass
        except Exception:
            pass

    # OneDrive environment fallback if folder is redirected and exists
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if onedrive:
        od_path = Path(onedrive).resolve()
        if not folders["documents"].exists() and (od_path / "Documents").exists():
            folders["documents"] = (od_path / "Documents").resolve()
        if not folders["desktop"].exists() and (od_path / "Desktop").exists():
            folders["desktop"] = (od_path / "Desktop").resolve()

    return folders


def resolve_folder_alias(raw_path: str) -> str:
    """Deterministically resolve natural-language folder aliases and permitted placeholders.

    Rejects unpermitted environment variables or unresolved placeholders.
    """
    if not raw_path or not raw_path.strip():
        return ""

    text = raw_path.strip().strip("'\"")
    known_folders = get_known_windows_folders()

    # Map normalized alias names to canonical keys
    alias_map = {
        "downloads": "downloads",
        "download": "downloads",
        "documents": "documents",
        "document": "documents",
        "docs": "documents",
        "doc": "documents",
        "desktop": "desktop",
        "desktops": "desktop",
        "home": "home",
        "userprofile": "home",
        "~": "home",
    }

    # 1. Direct natural-language phrasing check
    normalized_phrase = text.lower()
    for prefix in ("my ", "the "):
        if normalized_phrase.startswith(prefix):
            normalized_phrase = normalized_phrase[len(prefix):].strip()
    for suffix in (" folder", " directory", " dir"):
        if normalized_phrase.endswith(suffix):
            normalized_phrase = normalized_phrase[:-len(suffix)].strip()

    if normalized_phrase in alias_map:
        return str(known_folders[alias_map[normalized_phrase]])

    # 2. Check for model placeholder pattern: C:\Users\[username]\Downloads
    placeholder_pattern = (
        r"^[a-zA-Z]:[/\\]Users[/\\](?:\[[^\]]+\]|<[^>]+>|%username%|username)[/\\]"
        r"(?P<tail>[^/\\]+)(?P<subpath>.*)$"
    )
    match_ph = re.match(placeholder_pattern, text, re.IGNORECASE)
    if match_ph:
        tail = match_ph.group("tail").lower()
        subpath = match_ph.group("subpath").lstrip("/\\")
        if tail in alias_map:
            target = known_folders[alias_map[tail]]
            return str(target / subpath) if subpath else str(target)
        raise ValueError(f"Unrecognized placeholder path: {raw_path}")

    # 3. Reject any raw bracket placeholders like [username] or <user>
    if re.search(r"\[[a-zA-Z0-9_\-]+\]|<[a-zA-Z0-9_\-]+>", text):
        raise ValueError(f"Invalid path with unresolved placeholder: {raw_path}")

    # 4. Expand permitted user variables: %USERPROFILE%, %ONEDRIVE%, ~
    permitted_vars = {
        "%USERPROFILE%": str(known_folders["home"]),
        "$USERPROFILE": str(known_folders["home"]),
        "%ONEDRIVE%": str(os.environ.get("OneDrive", known_folders["home"])),
        "$ONEDRIVE": str(os.environ.get("OneDrive", known_folders["home"])),
        "~": str(known_folders["home"]),
    }
    for var, repl in permitted_vars.items():
        if text.upper().startswith(var.upper()):
            text = repl + text[len(var):]
            break

    # 5. Check if starts with a known alias (e.g., "Downloads/project" or "my downloads\file.pdf")
    parts = re.split(r"[/\\]", text, maxsplit=1)
    first_part = parts[0].strip().lower()
    for prefix in ("my ", "the "):
        if first_part.startswith(prefix):
            first_part = first_part[len(prefix):].strip()

    if first_part in alias_map:
        base_folder = known_folders[alias_map[first_part]]
        if len(parts) > 1 and parts[1]:
            return str(base_folder / parts[1])
        return str(base_folder)

    # 6. Reject remaining unpermitted environment variables to prevent variable injection
    if re.search(r"%[a-zA-Z0-9_]+%|\$[a-zA-Z0-9_]+", text):
        raise ValueError(f"Unpermitted environment variable reference in path: {raw_path}")

    return text


def validate_safe_path(target_path: str | Path, must_exist: bool = True) -> Path:
    """Resolve folder aliases and validate canonical path against allowed directories.

    Raises:
    - ValueError if path contains unpermitted placeholders.
    - PermissionError if filesystem access is disabled or path resolves outside allowed dirs.
    - FileNotFoundError if path does not exist and must_exist is True.
    """
    settings = get_settings()
    allowed_dirs = settings.get_allowed_directories()

    if not allowed_dirs:
        raise PermissionError(
            "Filesystem access is disabled. No directories configured in ORION_ALLOWED_DIRECTORIES."
        )

    # First resolve any natural-language aliases or permitted user variables
    resolved_alias_str = resolve_folder_alias(str(target_path))
    raw_path = Path(resolved_alias_str)

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
        raise PermissionError(f"Access denied: '{target_path}' is outside permitted directories.")

    return resolved


def prettify_path(raw_path: str | Path) -> str:
    """Format an absolute or raw filesystem path using human-readable folder aliases.

    Avoids leaking private Windows usernames and host path trees.
    Example:
    C:\\Users\\username\\Downloads\\notes.pdf -> Downloads/notes.pdf
    C:\\Users\\username\\OneDrive\\Documents\\Notes -> Documents/Notes
    """
    if not raw_path:
        return ""

    # Resolve any natural folder aliases (e.g. 'Downloads/...' -> actual user downloads folder)
    try:
        resolved_alias = resolve_folder_alias(str(raw_path))
        p = Path(resolved_alias)
    except Exception:
        p = Path(raw_path)

    try:
        resolved = p.resolve(strict=False)
    except Exception:
        resolved = p

    known = get_known_windows_folders()
    alias_order = [
        ("downloads", "Downloads"),
        ("documents", "Documents"),
        ("desktop", "Desktop"),
        ("home", "~"),
    ]

    for key, display_name in alias_order:
        base = known.get(key)
        if base:
            try:
                rel = resolved.relative_to(base)
                rel_str = str(rel).replace("\\", "/")
                return f"{display_name}/{rel_str}" if rel_str != "." else display_name
            except ValueError:
                continue

    clean_str = str(raw_path).replace("\\", "/")
    home_str = str(known.get("home", "")).replace("\\", "/")
    if home_str and clean_str.startswith(home_str):
        return "~" + clean_str[len(home_str):]

    return clean_str

