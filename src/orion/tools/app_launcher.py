"""Approved Windows application registry and safe launcher for ORION (Step 9).

Strictly enforces:
- Exactly 20 approved applications in allowlist.
- All other applications are blocked.
- No user- or model-provided paths, arguments, or shell commands.
- Safe Win32 process spawning (shell=False, fixed arguments, trusted locations only).
- Safe packaged app activation via IApplicationActivationManager or trusted explorer invocation.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Literal

logger = logging.getLogger("orion.tools.app_launcher")


@dataclass
class AppDefinition:
    """Metadata for an approved Windows application."""

    app_id: str
    display_name: str
    category: str
    launch_type: Literal["win32", "packaged"]
    path_resolver: Callable[[], Path | None] | None = None
    aumid_resolver: Callable[[], str | None] | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)
    fixed_args: tuple[str, ...] = field(default_factory=tuple)


# Trusted system and program directory prefixes for Win32 applications
def _get_trusted_win32_roots() -> list[Path]:
    roots: list[Path] = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "SystemRoot"):
        val = os.environ.get(env_var)
        if val:
            roots.append(Path(val).resolve())

    # Approved per-user program locations
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append((Path(local_app_data) / "Programs").resolve())

    app_data = os.environ.get("APPDATA")
    if app_data:
        roots.append((Path(app_data) / "Zoom" / "bin").resolve())

    return roots


# Resolvers for Win32 applications
def _resolve_word() -> Path | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand = Path(pf) / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE"
    return cand if cand.is_file() else None


def _resolve_excel() -> Path | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand = Path(pf) / "Microsoft Office" / "root" / "Office16" / "EXCEL.EXE"
    return cand if cand.is_file() else None


def _resolve_powerpoint() -> Path | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand = Path(pf) / "Microsoft Office" / "root" / "Office16" / "POWERPNT.EXE"
    return cand if cand.is_file() else None


def _resolve_notepad() -> Path | None:
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    cand = Path(sys_root) / "System32" / "notepad.exe"
    return cand if cand.is_file() else None


def _resolve_file_explorer() -> Path | None:
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    cand = Path(sys_root) / "explorer.exe"
    return cand if cand.is_file() else None


def _resolve_vscode() -> Path | None:
    local_ad = os.environ.get("LOCALAPPDATA")
    if local_ad:
        cand = Path(local_ad) / "Programs" / "Microsoft VS Code" / "Code.exe"
        if cand.is_file():
            return cand
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand2 = Path(pf) / "Microsoft VS Code" / "Code.exe"
    return cand2 if cand2.is_file() else None


def _resolve_antigravity_ide() -> Path | None:
    local_ad = os.environ.get("LOCALAPPDATA")
    if local_ad:
        cand = Path(local_ad) / "Programs" / "Antigravity IDE" / "Antigravity IDE.exe"
        if cand.is_file():
            return cand
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand2 = Path(pf) / "Antigravity IDE" / "Antigravity IDE.exe"
    return cand2 if cand2.is_file() else None


def _resolve_edge() -> Path | None:
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cand = Path(pfx86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    if cand.is_file():
        return cand
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand2 = Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    return cand2 if cand2.is_file() else None


def _resolve_comet() -> Path | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand = Path(pf) / "Perplexity" / "Comet" / "Application" / "comet.exe"
    if cand.is_file():
        return cand
    local_ad = os.environ.get("LOCALAPPDATA")
    if local_ad:
        cand2 = Path(local_ad) / "Programs" / "Perplexity" / "Comet" / "Application" / "comet.exe"
        if cand2.is_file():
            return cand2
    return None


def _resolve_zoom() -> Path | None:
    ad = os.environ.get("APPDATA")
    if ad:
        cand = Path(ad) / "Zoom" / "bin" / "Zoom.exe"
        if cand.is_file():
            return cand
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand2 = Path(pf) / "Zoom" / "bin" / "Zoom.exe"
    return cand2 if cand2.is_file() else None


def _resolve_vlc() -> Path | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand = Path(pf) / "VideoLAN" / "VLC" / "vlc.exe"
    if cand.is_file():
        return cand
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cand2 = Path(pfx86) / "VideoLAN" / "VLC" / "vlc.exe"
    return cand2 if cand2.is_file() else None


def _resolve_winrar() -> Path | None:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand = Path(pf) / "WinRAR" / "WinRAR.exe"
    if cand.is_file():
        return cand
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cand2 = Path(pfx86) / "WinRAR" / "WinRAR.exe"
    return cand2 if cand2.is_file() else None


# Resolvers for Packaged Applications (UWP/MSIX)
def _check_packaged_app_installed(aumid: str) -> bool:
    """Verify if a packaged AppUserModelID is installed on Windows."""
    if sys.platform != "win32":
        # On non-Windows/mock environments, return true if valid format
        return bool(aumid and ("!" in aumid or aumid.startswith("Microsoft.")))

    try:
        import winreg

        # 1. Check AppModel repository packages in HKCU
        repo_path = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
        pkg_prefix = aumid.split("!")[0]
        fam_prefix = pkg_prefix.split("_")[0].lower() if "_" in pkg_prefix else pkg_prefix.lower()

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, repo_path) as k:
                num_subkeys = winreg.QueryInfoKey(k)[0]
                for i in range(num_subkeys):
                    sub = winreg.EnumKey(k, i).lower()
                    if fam_prefix in sub:
                        return True
        except FileNotFoundError:
            pass

        # 2. Check Launch Contract in HKCU
        contract_path = r"Software\Classes\Extensions\ContractId\Windows.Launch\PackageId"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, contract_path) as k:
                num_subkeys = winreg.QueryInfoKey(k)[0]
                for i in range(num_subkeys):
                    sub = winreg.EnumKey(k, i).lower()
                    if fam_prefix in sub:
                        return True
        except FileNotFoundError:
            pass

        # 3. Fallback check for system packaged apps (e.g. Office / system UWP)
        if aumid in ("Microsoft.Office.OneNote.MemoryPreview", "Microsoft.Windows.Explorer"):
            return True

        return False
    except Exception as err:
        logger.debug("Packaged app check exception for %s: %s", aumid, err)
        return False


def _resolve_calculator() -> str | None:
    aumid = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    return aumid if _check_packaged_app_installed(aumid) else None


def _resolve_sticky_notes() -> str | None:
    # Modern Windows 11 uses OneNote MemoryPreview; earlier versions use MicrosoftStickyNotes
    cand1 = "Microsoft.Office.OneNote.MemoryPreview"
    cand2 = "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe!App"
    if _check_packaged_app_installed(cand1):
        return cand1
    if _check_packaged_app_installed(cand2):
        return cand2
    return cand2


def _resolve_whatsapp() -> str | None:
    aumid = "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"
    return aumid if _check_packaged_app_installed(aumid) else None


def _resolve_teams() -> str | None:
    aumid = "MSTeams_8wekyb3d8bbwe!MSTeams"
    return aumid if _check_packaged_app_installed(aumid) else None


def _resolve_chatgpt() -> str | None:
    aumid = "OpenAI.Codex_2p2nqsd0c76g0!App"
    return aumid if _check_packaged_app_installed(aumid) else None


def _resolve_claude() -> str | None:
    aumid = "Claude_pzs8sxrjxfjjc!Claude"
    return aumid if _check_packaged_app_installed(aumid) else None


def _resolve_paint() -> str | None:
    aumid = "Microsoft.Paint_8wekyb3d8bbwe!App"
    return aumid if _check_packaged_app_installed(aumid) else None


def _resolve_snipping_tool() -> str | None:
    aumid = "Microsoft.ScreenSketch_8wekyb3d8bbwe!App"
    return aumid if _check_packaged_app_installed(aumid) else None


# =====================================================================
# EXACT APPLICATION ALLOWLIST — EXACTLY 20 APPLICATIONS
# =====================================================================

APPROVED_APPLICATIONS: dict[str, AppDefinition] = {
    # 1. PRODUCTIVITY (7 applications)
    "word": AppDefinition(
        app_id="word",
        display_name="Microsoft Word",
        category="Productivity",
        launch_type="win32",
        path_resolver=_resolve_word,
        aliases=("word", "microsoft word", "ms word", "winword", "doc", "docs"),
    ),
    "excel": AppDefinition(
        app_id="excel",
        display_name="Microsoft Excel",
        category="Productivity",
        launch_type="win32",
        path_resolver=_resolve_excel,
        aliases=("excel", "microsoft excel", "ms excel", "spreadsheet", "sheets"),
    ),
    "powerpoint": AppDefinition(
        app_id="powerpoint",
        display_name="Microsoft PowerPoint",
        category="Productivity",
        launch_type="win32",
        path_resolver=_resolve_powerpoint,
        aliases=("powerpoint", "power point", "ppt", "microsoft powerpoint", "ms powerpoint", "slides"),
    ),
    "notepad": AppDefinition(
        app_id="notepad",
        display_name="Notepad",
        category="Productivity",
        launch_type="win32",
        path_resolver=_resolve_notepad,
        aliases=("notepad", "note pad", "node pad", "not pad", "windows notepad", "text editor"),
    ),
    "calculator": AppDefinition(
        app_id="calculator",
        display_name="Calculator",
        category="Productivity",
        launch_type="packaged",
        aumid_resolver=_resolve_calculator,
        aliases=("calculator", "calc", "cal c", "call c", "cal see", "windows calculator"),
    ),
    "sticky_notes": AppDefinition(
        app_id="sticky_notes",
        display_name="Sticky Notes",
        category="Productivity",
        launch_type="packaged",
        aumid_resolver=_resolve_sticky_notes,
        aliases=("sticky notes", "sticky note", "stickynotes", "notes"),
    ),
    "file_explorer": AppDefinition(
        app_id="file_explorer",
        display_name="File Explorer",
        category="Productivity",
        launch_type="win32",
        path_resolver=_resolve_file_explorer,
        aliases=("file explorer", "explorer", "files explorer", "windows explorer", "my computer", "files"),
    ),
    # 2. DEVELOPMENT & AI (4 applications)
    "vscode": AppDefinition(
        app_id="vscode",
        display_name="Visual Studio Code",
        category="Development & AI",
        launch_type="win32",
        path_resolver=_resolve_vscode,
        aliases=("vs code", "vscode", "v s code", "we s code", "wee s code", "code", "visual studio code"),
    ),
    "antigravity_ide": AppDefinition(
        app_id="antigravity_ide",
        display_name="Antigravity IDE",
        category="Development & AI",
        launch_type="win32",
        path_resolver=_resolve_antigravity_ide,
        aliases=("antigravity", "antigravity ide", "agy"),
    ),
    "chatgpt": AppDefinition(
        app_id="chatgpt",
        display_name="ChatGPT Desktop",
        category="Development & AI",
        launch_type="packaged",
        aumid_resolver=_resolve_chatgpt,
        aliases=("chatgpt", "chatgpt desktop", "chat gpt", "openai"),
    ),
    "claude": AppDefinition(
        app_id="claude",
        display_name="Claude Desktop",
        category="Development & AI",
        launch_type="packaged",
        aumid_resolver=_resolve_claude,
        aliases=("claude", "claude desktop", "anthropic"),
    ),
    # 3. BROWSERS & COMMUNICATION (5 applications)
    "edge": AppDefinition(
        app_id="edge",
        display_name="Microsoft Edge",
        category="Browsers & Communication",
        launch_type="win32",
        path_resolver=_resolve_edge,
        aliases=("edge", "microsoft edge", "ms edge", "browser"),
    ),
    "comet": AppDefinition(
        app_id="comet",
        display_name="Comet",
        category="Browsers & Communication",
        launch_type="win32",
        path_resolver=_resolve_comet,
        aliases=("comet", "perplexity comet", "perplexity"),
    ),
    "whatsapp": AppDefinition(
        app_id="whatsapp",
        display_name="WhatsApp",
        category="Browsers & Communication",
        launch_type="packaged",
        aumid_resolver=_resolve_whatsapp,
        aliases=("whatsapp", "whats app", "wa"),
    ),
    "teams": AppDefinition(
        app_id="teams",
        display_name="Microsoft Teams",
        category="Browsers & Communication",
        launch_type="packaged",
        aumid_resolver=_resolve_teams,
        aliases=("teams", "microsoft teams", "ms teams"),
    ),
    "zoom": AppDefinition(
        app_id="zoom",
        display_name="Zoom Workplace",
        category="Browsers & Communication",
        launch_type="win32",
        path_resolver=_resolve_zoom,
        aliases=("zoom", "zoom workplace", "zoom meeting", "zoom meetings"),
    ),
    # 4. MEDIA & UTILITIES (4 applications)
    "vlc": AppDefinition(
        app_id="vlc",
        display_name="VLC Media Player",
        category="Media & Utilities",
        launch_type="win32",
        path_resolver=_resolve_vlc,
        aliases=("vlc", "vlc media player", "vlc player", "video player"),
    ),
    "paint": AppDefinition(
        app_id="paint",
        display_name="Paint",
        category="Media & Utilities",
        launch_type="packaged",
        aumid_resolver=_resolve_paint,
        aliases=("paint", "ms paint", "mspaint", "microsoft paint"),
    ),
    "snipping_tool": AppDefinition(
        app_id="snipping_tool",
        display_name="Snipping Tool",
        category="Media & Utilities",
        launch_type="packaged",
        aumid_resolver=_resolve_snipping_tool,
        aliases=("snipping tool", "snippingtool", "snip", "screenshot"),
    ),
    "winrar": AppDefinition(
        app_id="winrar",
        display_name="WinRAR",
        category="Media & Utilities",
        launch_type="win32",
        path_resolver=_resolve_winrar,
        aliases=("winrar", "win rar", "rar"),
    ),
}

# Blocked applications and unapproved system components mapped to explanatory messages
BLOCKED_APPLICATIONS: dict[str, str] = {
    "chrome": "Google Chrome is not installed on this system and is not in the approved allowlist.",
    "google chrome": "Google Chrome is not installed on this system and is not in the approved allowlist.",
    "steam": "Steam is not in the approved application allowlist.",
    "epic games": "Epic Games Launcher is not in the approved application allowlist.",
    "epic games launcher": "Epic Games Launcher is not in the approved application allowlist.",
    "discord": "Discord is not currently installed or in the approved application allowlist.",
    "powershell": "PowerShell is a command shell and is strictly prohibited for security.",
    "windows powershell": "PowerShell is a command shell and is strictly prohibited for security.",
    "cmd": "Command Prompt is a command shell and is strictly prohibited for security.",
    "command prompt": "Command Prompt is a command shell and is strictly prohibited for security.",
    "terminal": "Windows Terminal is a command shell and is strictly prohibited for security.",
    "windows terminal": "Windows Terminal is a command shell and is strictly prohibited for security.",
    "git bash": "Git Bash is a command shell and is strictly prohibited for security.",
    "regedit": "Registry Editor is an administrative tool and is strictly prohibited for security.",
    "registry editor": "Registry Editor is an administrative tool and is strictly prohibited for security.",
    "task manager": "Task Manager is an administrative tool and is not in the approved allowlist.",
    "control panel": "Control Panel is an administrative utility and is not in the approved allowlist.",
}


def get_approved_apps() -> dict[str, AppDefinition]:
    """Return the immutable approved applications registry."""
    return dict(APPROVED_APPLICATIONS)


def find_matching_apps(query: str) -> list[str]:
    """Find all approved app_ids that match the query in an app-command context.

    Returns a list of matching approved app_ids (0, 1, or multiple).
    Strictly matches against the 20 approved applications only.
    Never launches an unknown app through unrestricted fuzzy matching.
    """
    clean = re.sub(r"[^\w\s]", "", query).strip().lower()
    if not clean:
        return []

    # Reject if suspicious shell/URI characters were present
    if any(ch in query for ch in (";", "&", "|", "`", "$", "<", ">", "\\", "/", ":", "\n", "\r")):
        return []

    # Strip conversational prefixes: "the ", "my "
    clean = re.sub(r"^(?:the\s+|my\s+)", "", clean).strip()

    # Strip common suffixes: " app", " application", " program", " window", " windows"
    clean = re.sub(r"\s+(?:app|application|program|window|windows)$", "", clean).strip()

    # 1. Direct match on app_id
    if clean in APPROVED_APPLICATIONS:
        return [clean]

    # 2. Match aliases and display names (exact match)
    exact_matches = []
    for app_id, app in APPROVED_APPLICATIONS.items():
        if clean == app.display_name.lower():
            exact_matches.append(app_id)
        elif any(clean == alias.lower() for alias in app.aliases):
            exact_matches.append(app_id)

    if exact_matches:
        return list(dict.fromkeys(exact_matches))

    # 3. Normalized space match (e.g. "vs code" vs "vscode", "note pad" vs "notepad")
    clean_nospace = clean.replace(" ", "")
    nospace_matches = []
    for app_id, app in APPROVED_APPLICATIONS.items():
        if clean_nospace == app.app_id.replace("_", ""):
            nospace_matches.append(app_id)
        elif clean_nospace == app.display_name.lower().replace(" ", ""):
            nospace_matches.append(app_id)
        elif any(clean_nospace == alias.lower().replace(" ", "") for alias in app.aliases):
            nospace_matches.append(app_id)

    if nospace_matches:
        return list(dict.fromkeys(nospace_matches))

    # 4. Substring match against approved allowlist only (query must be at least 3 chars)
    if len(clean) >= 3:
        sub_matches = set()
        for app_id, app in APPROVED_APPLICATIONS.items():
            if clean in app.display_name.lower() or any(clean in alias.lower() for alias in app.aliases):
                sub_matches.add(app_id)
        if sub_matches:
            return sorted(list(sub_matches))

    return []


def resolve_app_alias(query: str) -> str | None:
    """Resolve a user or model string to an approved app_id if unambiguous.

    Returns the approved app_id if exactly one match is found, or None.
    """
    matches = find_matching_apps(query)
    if len(matches) == 1:
        return matches[0]
    return None


def is_app_blocked(query: str) -> tuple[bool, str]:
    """Check if a given application query refers to a known blocked application."""
    clean = re.sub(r"[^\w\s]", "", query).strip().lower()
    if clean in BLOCKED_APPLICATIONS:
        return True, BLOCKED_APPLICATIONS[clean]
    for blocked_name, reason in BLOCKED_APPLICATIONS.items():
        if blocked_name in clean:
            return True, reason
    return False, ""


def is_app_available(app_id: str) -> tuple[bool, str, Any]:
    """Check if an approved application is currently available and installed.

    Returns (is_available, message, target_resolved).
    """
    if app_id not in APPROVED_APPLICATIONS:
        return False, f"Application '{app_id}' is not in the approved application allowlist.", None

    app = APPROVED_APPLICATIONS[app_id]

    if app.launch_type == "win32":
        if not app.path_resolver:
            return False, f"No path resolver configured for '{app.display_name}'.", None
        resolved_path = app.path_resolver()
        if not resolved_path:
            return False, f"Application '{app.display_name}' executable was not found on this system.", None

        # Verify executable existence
        if not resolved_path.is_file():
            return False, f"Application '{app.display_name}' executable does not exist.", None

        # Canonicalize path and verify against trusted locations
        try:
            canonical = resolved_path.resolve(strict=True)
        except Exception as err:
            return False, f"Failed to resolve path for '{app.display_name}': {err}", None

        # Validate canonical path is within trusted roots
        trusted_roots = _get_trusted_win32_roots()
        is_trusted_root = any(
            str(canonical).lower().startswith(str(root).lower()) for root in trusted_roots
        )
        if not is_trusted_root:
            logger.warning("Application path %s outside trusted roots: %s", canonical, trusted_roots)
            return False, f"Executable path for '{app.display_name}' is outside approved system directories.", None

        # Reject paths in untrusted locations (Downloads, Temp subfolders, etc.)
        for part in canonical.parts:
            if part.lower() in ("downloads",):
                return False, f"Executable path for '{app.display_name}' is in an untrusted directory.", None

        return True, "Available", canonical

    elif app.launch_type == "packaged":
        if not app.aumid_resolver:
            return False, f"No package resolver configured for '{app.display_name}'.", None
        aumid = app.aumid_resolver()
        if not aumid:
            return False, f"Packaged application '{app.display_name}' is not installed or available on this system.", None

        return True, "Available", aumid

    return False, "Unknown launch type.", None


def _activate_packaged_app(aumid: str) -> bool:
    """Activate a packaged Windows application using COM or trusted shell activation."""
    # 1. Prefer IApplicationActivationManager via COM on Windows
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", wintypes.BYTE * 8),
                ]

            clsid_act = GUID(
                0x45BA127D, 0x10A8, 0x46EA, (wintypes.BYTE * 8)(0x8A, 0xB7, 0x56, 0xEA, 0x90, 0x78, 0x94, 0x3C)
            )
            iid_act = GUID(
                0x2E941141, 0x7F97, 0x4756, (wintypes.BYTE * 8)(0xBA, 0x1D, 0x9D, 0xEC, 0xDE, 0x89, 0x4A, 0x3D)
            )

            ole32 = ctypes.oledll.ole32
            ole32.CoInitialize(None)

            mgr = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid_act),
                None,
                1 | 4,  # CLSCTX_LOCAL_SERVER | CLSCTX_INPROC_SERVER
                ctypes.byref(iid_act),
                ctypes.byref(mgr),
            )
            if hr == 0 and mgr.value:
                # vtable slot 3: ActivateApplication(this, appUserModelId, arguments, options, &pid)
                vtable = ctypes.cast(mgr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                activate_func_type = ctypes.WINFUNCTYPE(
                    ctypes.HRESULT,
                    ctypes.c_void_p,
                    ctypes.c_wchar_p,
                    ctypes.c_wchar_p,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                )
                activate_func = activate_func_type(vtable[3])
                pid = wintypes.DWORD(0)
                hr_act = activate_func(mgr, aumid, None, 0, ctypes.byref(pid))

                # Release COM object
                release_func_type = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
                release_func = release_func_type(vtable[2])
                release_func(mgr)

                if hr_act == 0:
                    logger.info("Successfully activated packaged app %s with PID %d via COM", aumid, pid.value)
                    return True
                else:
                    logger.warning("COM ActivateApplication failed for %s with hr=%s", aumid, hex(hr_act & 0xFFFFFFFF))
        except Exception as err:
            logger.warning("COM activation exception for %s: %s", aumid, err)

    # 2. Fallback: activate via trusted explorer.exe shell:AppsFolder with shell=False
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    explorer_path = Path(sys_root) / "explorer.exe"
    if not explorer_path.is_file():
        logger.error("explorer.exe not found at %s", explorer_path)
        return False

    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
        )

    target_uri = f"shell:AppsFolder\\{aumid}"
    logger.info("Invoking packaged app %s via explorer.exe %s", aumid, target_uri)
    subprocess.Popen(
        [str(explorer_path), target_uri],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return True


def launch_application(app_id: str) -> dict[str, Any]:
    """Execute the launch of an approved application after user confirmation.

    Never accepts paths or arguments from the user or model.
    """
    if app_id not in APPROVED_APPLICATIONS:
        return {
            "success": False,
            "app_id": app_id,
            "error": f"Application '{app_id}' is not in the approved application allowlist.",
        }

    app = APPROVED_APPLICATIONS[app_id]

    # Revalidate availability immediately prior to launching
    is_avail, avail_msg, target = is_app_available(app_id)
    if not is_avail:
        return {
            "success": False,
            "app_id": app_id,
            "display_name": app.display_name,
            "error": f"Application unavailable: {avail_msg}",
        }

    try:
        if app.launch_type == "win32":
            canonical_path = target
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
                )

            # Fixed argument vector only. No model-generated flags.
            cmd = [str(canonical_path)] + list(app.fixed_args)
            logger.info("Launching Win32 application %s via %s", app.display_name, cmd)
            subprocess.Popen(
                cmd,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
            return {
                "success": True,
                "app_id": app_id,
                "display_name": app.display_name,
                "message": f"Launch requested for {app.display_name}.",
            }

        elif app.launch_type == "packaged":
            aumid = target
            success = _activate_packaged_app(aumid)
            if not success:
                return {
                    "success": False,
                    "app_id": app_id,
                    "display_name": app.display_name,
                    "error": f"Application unavailable: Failed to activate {app.display_name}.",
                }
            return {
                "success": True,
                "app_id": app_id,
                "display_name": app.display_name,
                "message": f"Launch requested for {app.display_name}.",
            }

        return {
            "success": False,
            "app_id": app_id,
            "display_name": app.display_name,
            "error": "Unsupported application launch type.",
        }

    except Exception as err:
        logger.error("Error launching application %s: %s", app.display_name, err)
        return {
            "success": False,
            "app_id": app_id,
            "display_name": app.display_name,
            "error": f"Failed to launch {app.display_name}: {type(err).__name__}",
        }


def _get_short_display_name(app_id: str) -> str:
    """Return natural short display name for notifications (e.g. 'VS Code')."""
    if app_id == "vscode":
        return "VS Code"
    if app_id == "file_explorer":
        return "File Explorer"
    approved = get_approved_apps()
    if app_id in approved:
        return approved[app_id].display_name
    return app_id


def _join_names(names: list[str], conj: str = "and") -> str:
    """Join names with commas and conjunction."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} {conj} {names[1]}"
    return f"{', '.join(names[:-1])} {conj} {names[-1]}"


def _format_composite_outcome(
    succeeded_actions: list[dict[str, Any]],
    failed_actions: list[dict[str, Any]],
    skipped_actions: list[dict[str, Any]],
) -> str:
    """Format outcomes into concise, natural combined updates without raw JSON or stack traces."""
    all_acts = succeeded_actions + failed_actions
    if not all_acts:
        return "No operations were performed."

    # 1. If only launches:
    if all(a.get("action") == "launch" for a in all_acts + skipped_actions):
        s_names = [a["display_name"] for a in succeeded_actions]
        f_names = [a["display_name"] for a in failed_actions]
        if s_names and not f_names:
            return f"Opened {_join_names(s_names)}."
        elif s_names and f_names:
            return f"Opened {_join_names(s_names)}, but couldn't launch {_join_names(f_names, 'or')}."
        else:
            return f"Couldn't launch {_join_names(f_names, 'or')}."

    # 2. If launch failed and dependent actions were skipped:
    if not succeeded_actions and failed_actions:
        first_fail = failed_actions[0]
        if first_fail.get("action") == "launch":
            return f"Couldn't launch {first_fail['display_name']}."
        return f"Couldn't {first_fail['action']} {first_fail['display_name']}."

    launches_succ = [a for a in succeeded_actions if a.get("action") == "launch"]
    launches_fail = [a for a in failed_actions if a.get("action") == "launch"]
    wins_succ = [a for a in succeeded_actions if a.get("action") != "launch"]
    wins_fail = [a for a in failed_actions if a.get("action") != "launch"]

    # Pattern A: Window action first, then launch (e.g. "Minimize calculator then open explorer")
    if all_acts and all_acts[0].get("action") != "launch":
        first_w = all_acts[0]
        w_disp = first_w["display_name"]
        w_act = first_w["action"]
        if first_w in wins_succ:
            past = {"minimize": "minimized", "maximize": "maximized", "restore": "restored", "focus": "focused"}.get(w_act, w_act)
            win_part = f"{w_disp} {past}."
        else:
            win_part = f"Couldn't {w_act} {w_disp}."

        launch_part = ""
        if launches_succ:
            l_names = _join_names([a["display_name"] for a in launches_succ])
            launch_part = f" {l_names} is now open."
        elif launches_fail:
            l_names = _join_names([a["display_name"] for a in launches_fail], "or")
            launch_part = f" Couldn't open {l_names}."

        return (win_part + launch_part).strip()

    # Pattern B: Launch(es) first, then window action(s)
    if launches_succ:
        l_names = _join_names([a["display_name"] for a in launches_succ])

        # Single app: launched and window-managed
        if len(launches_succ) == 1 and wins_succ and len(wins_succ) == 1 and wins_succ[0]["app_id"] == launches_succ[0]["app_id"]:
            w = wins_succ[0]
            if w["action"] == "focus":
                return f"Opened {l_names} and brought it to the front."
            elif w["action"] in ("maximize", "minimize", "restore"):
                past = {"maximize": "maximized", "minimize": "minimized", "restore": "restored"}.get(w["action"])
                return f"Opened and {past} {l_names}."

        # Single app: launch succeeded, but window action failed
        if len(launches_succ) == 1 and wins_fail and len(wins_fail) == 1 and wins_fail[0]["app_id"] == launches_succ[0]["app_id"]:
            w = wins_fail[0]
            err = (w.get("error") or "").lower()
            if w["action"] == "focus" and ("behind" in err or "blocked" in err or "front" in err or "foreground" in err):
                return f"Opened {l_names}, but Windows wouldn't bring it to the front."
            verb = {"maximize": "maximize it", "minimize": "minimize it", "restore": "restore it", "focus": "bring it to the front"}.get(w["action"], f"{w['action']} it")
            return f"Opened {l_names}, but couldn't {verb}."

        # Multiple launches with window action:
        launch_sent = f"Opened {l_names}."
        win_sents = []
        for w in wins_succ:
            w_disp = w["display_name"]
            if w["action"] == "focus":
                win_sents.append(f"{w_disp} is now in the foreground.")
            elif w["action"] == "maximize":
                win_sents.append(f"{w_disp} is now maximized.")
            elif w["action"] == "minimize":
                win_sents.append(f"{w_disp} is now minimized.")
            elif w["action"] == "restore":
                win_sents.append(f"{w_disp} is now restored to Normal.")

        for w in wins_fail:
            w_disp = w["display_name"]
            err = (w.get("error") or "").lower()
            if w["action"] == "focus" and ("behind" in err or "blocked" in err or "front" in err or "foreground" in err):
                win_sents.append(f"Windows wouldn't bring {w_disp} to the front.")
            else:
                win_sents.append(f"Couldn't {w['action']} {w_disp}.")

        if win_sents:
            return f"{launch_sent} {' '.join(win_sents)}"
        return launch_sent

    return "Operations completed."


def execute_composite_app_action(
    command_type: str,
    app_ids: list[str] | None = None,
    target_app_id: str | None = None,
    window_action: str | None = None,
    summary: str = "Application action",
    display_names: list[str] | None = None,
    display_name: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute multiple application launches or sequential launch-and-window actions.

    Executes each action sequentially, verifies individual outcomes, stops dependent
    actions if a prerequisite fails, and returns a concise, natural combined message.
    """
    import time
    approved = get_approved_apps()

    # Normalize actions list from legacy command_type if not provided
    action_list = list(actions or [])
    if not action_list:
        if command_type == "multi_launch":
            for aid in (app_ids or []):
                action_list.append({"action": "launch", "app_id": aid})
        elif command_type == "launch_and_window":
            target = target_app_id or (app_ids[0] if app_ids else None)
            if target:
                action_list.append({"action": "launch", "app_id": target})
                action_list.append({"action": window_action or "maximize", "app_id": target})

    if not action_list:
        return {"success": False, "error": f"No actions specified for {command_type}."}

    succeeded_actions: list[dict[str, Any]] = []
    failed_actions: list[dict[str, Any]] = []
    skipped_actions: list[dict[str, Any]] = []
    failed_apps: set[str] = set()
    launched_apps: set[str] = set()

    from orion.tools.desktop_inspector import (
        _enumerate_windows_native,
        focus_application_window,
        maximize_application_window,
        minimize_application_window,
        restore_application_window,
    )

    act_map = {
        "focus": focus_application_window,
        "minimize": minimize_application_window,
        "maximize": maximize_application_window,
        "restore": restore_application_window,
    }

    for item in action_list:
        act = item.get("action", "launch")
        aid = item.get("app_id", "")
        disp = item.get("display_name") or _get_short_display_name(aid)

        if not aid or aid not in approved:
            failed_actions.append({"action": act, "app_id": aid, "display_name": disp, "error": "not approved"})
            failed_apps.add(aid)
            continue

        # Prerequisite failure check: stop dependent actions on this app
        if aid in failed_apps:
            skipped_actions.append({"action": act, "app_id": aid, "display_name": disp, "error": "prerequisite failed"})
            continue

        if act == "launch":
            is_avail, _, _ = is_app_available(aid)
            if not is_avail:
                failed_actions.append({"action": act, "app_id": aid, "display_name": disp, "error": "not available"})
                failed_apps.add(aid)
                continue

            res = launch_application(aid)
            if res.get("success"):
                launched_apps.add(aid)
                succeeded_actions.append({"action": act, "app_id": aid, "display_name": disp})
            else:
                failed_actions.append({"action": act, "app_id": aid, "display_name": disp, "error": res.get("error")})
                failed_apps.add(aid)

        elif act in act_map:
            # If app was just launched earlier in this command, wait briefly for window to appear
            if aid in launched_apps:
                for _ in range(5):
                    try:
                        wins = _enumerate_windows_native()
                        if any(w.app_id == aid for w in wins):
                            break
                    except Exception:
                        pass
                    time.sleep(0.25)

            tok = item.get("window_token")
            idx = item.get("window_index")
            fn = act_map[act]
            win_res = fn(aid, window_token=tok, window_index=idx)
            if win_res.get("success"):
                succeeded_actions.append({"action": act, "app_id": aid, "display_name": disp, "message": win_res.get("message")})
            else:
                failed_actions.append({"action": act, "app_id": aid, "display_name": disp, "error": win_res.get("error", "")})
        else:
            failed_actions.append({"action": act, "app_id": aid, "display_name": disp, "error": f"unknown action {act}"})

    msg = _format_composite_outcome(succeeded_actions, failed_actions, skipped_actions)
    overall_success = len(succeeded_actions) > 0

    return {
        "success": overall_success,
        "partial": len(failed_actions) > 0 and len(succeeded_actions) > 0,
        "message": msg,
        "succeeded": [a["display_name"] for a in succeeded_actions],
        "failed": [a["display_name"] for a in failed_actions],
        "skipped": [a["display_name"] for a in skipped_actions],
    }
