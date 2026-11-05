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
        aliases=("powerpoint", "ppt", "microsoft powerpoint", "ms powerpoint", "slides"),
    ),
    "notepad": AppDefinition(
        app_id="notepad",
        display_name="Notepad",
        category="Productivity",
        launch_type="win32",
        path_resolver=_resolve_notepad,
        aliases=("notepad", "windows notepad", "text editor"),
    ),
    "calculator": AppDefinition(
        app_id="calculator",
        display_name="Calculator",
        category="Productivity",
        launch_type="packaged",
        aumid_resolver=_resolve_calculator,
        aliases=("calculator", "calc", "windows calculator"),
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
        aliases=("file explorer", "explorer", "windows explorer", "my computer", "files"),
    ),
    # 2. DEVELOPMENT & AI (4 applications)
    "vscode": AppDefinition(
        app_id="vscode",
        display_name="Visual Studio Code",
        category="Development & AI",
        launch_type="win32",
        path_resolver=_resolve_vscode,
        aliases=("vs code", "vscode", "code", "visual studio code"),
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


def resolve_app_alias(query: str) -> str | None:
    """Resolve a user or model string to an approved app_id.

    Returns the approved app_id if found, or None.
    """
    clean = re.sub(r"[^\w\s]", "", query).strip().lower()
    if not clean:
        return None

    # Reject if suspicious shell/URI characters were present
    if any(ch in query for ch in (";", "&", "|", "`", "$", "<", ">", "\\", "/", ":", "\n", "\r")):
        return None

    # Strip conversational prefixes: "the ", "my "
    clean = re.sub(r"^(?:the\s+|my\s+)", "", clean).strip()

    # Strip common suffixes: " app", " application", " program", " window", " windows"
    clean = re.sub(r"\s+(?:app|application|program|window|windows)$", "", clean).strip()

    # Direct match on app_id
    if clean in APPROVED_APPLICATIONS:
        return clean

    # Match aliases and display names (exact)
    for app_id, app in APPROVED_APPLICATIONS.items():
        if clean == app.display_name.lower():
            return app_id
        for alias in app.aliases:
            if clean == alias.lower():
                return app_id

    # Normalized space match (e.g. "vs code" vs "vscode")
    clean_nospace = clean.replace(" ", "")
    for app_id, app in APPROVED_APPLICATIONS.items():
        if clean_nospace == app.app_id.replace("_", ""):
            return app_id
        if clean_nospace == app.display_name.lower().replace(" ", ""):
            return app_id
        for alias in app.aliases:
            if clean_nospace == alias.lower().replace(" ", ""):
                return app_id

    # Substring match only if clean is a substring of display_name or an alias
    matches = set()
    for app_id, app in APPROVED_APPLICATIONS.items():
        if clean in app.display_name.lower() or any(clean in alias.lower() for alias in app.aliases):
            matches.add(app_id)

    if len(matches) == 1:
        return next(iter(matches))

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

    Executes each action sequentially, verifies individual outcomes, and returns
    a concise, natural combined message.
    """
    import time
    approved = get_approved_apps()

    if command_type == "multi_launch":
        app_ids = app_ids or []
        succeeded: list[str] = []
        failed: list[str] = []

        for aid in app_ids:
            if aid not in approved:
                failed.append(aid)
                continue

            app = approved[aid]
            disp = _get_short_display_name(aid)
            is_avail, _, _ = is_app_available(aid)
            if not is_avail:
                failed.append(disp)
                continue

            res = launch_application(aid)
            if res.get("success"):
                succeeded.append(disp)
            else:
                failed.append(disp)

        def _join_names(names: list[str], conj: str = "and") -> str:
            if not names:
                return ""
            if len(names) == 1:
                return names[0]
            if len(names) == 2:
                return f"{names[0]} {conj} {names[1]}"
            return f"{', '.join(names[:-1])} {conj} {names[-1]}"

        if succeeded and not failed:
            msg = f"Opened {_join_names(succeeded)}."
        elif succeeded and failed:
            msg = f"Opened {_join_names(succeeded)}, but couldn't launch {_join_names(failed, 'or')}."
        else:
            msg = f"Couldn't launch {_join_names(failed, 'or')}."

        return {
            "success": len(succeeded) > 0,
            "message": msg,
            "succeeded": succeeded,
            "failed": failed,
        }

    elif command_type == "launch_and_window":
        target = target_app_id or (app_ids[0] if app_ids else None)
        if not target or target not in approved:
            return {"success": False, "error": f"Application '{target}' is not approved."}

        disp = _get_short_display_name(target)

        # 1. Launch the application
        is_avail, _, _ = is_app_available(target)
        if not is_avail:
            return {"success": False, "message": f"Couldn't launch {disp}."}

        launch_res = launch_application(target)
        if not launch_res.get("success"):
            return {"success": False, "message": f"Couldn't launch {disp}."}

        # 2. Wait briefly for window to appear on desktop (up to ~1.5s)
        from orion.tools.desktop_inspector import (
            _enumerate_windows_native,
            focus_application_window,
            maximize_application_window,
            minimize_application_window,
            restore_application_window,
        )

        win_found = False
        for _ in range(5):
            try:
                wins = _enumerate_windows_native()
                if any(w.app_id == target for w in wins):
                    win_found = True
                    break
            except Exception:
                pass
            time.sleep(0.3)

        # 3. Perform the requested window action
        win_res: dict[str, Any] = {"success": False}
        act_map = {
            "maximize": maximize_application_window,
            "minimize": minimize_application_window,
            "focus": focus_application_window,
            "restore": restore_application_window,
        }
        fn = act_map.get(window_action or "maximize")
        if fn:
            win_res = fn(target)

        # 4. Natural response formatting
        verb_success = {
            "maximize": "maximized",
            "minimize": "minimized",
            "focus": "brought it to the foreground",
            "restore": "restored",
        }.get(window_action, "managed")

        verb_fail = {
            "maximize": "maximize it",
            "minimize": "minimize it",
            "focus": "bring it to the foreground",
            "restore": "restore it",
        }.get(window_action, "manage it")

        if win_res.get("success"):
            if window_action in ("maximize", "minimize", "restore"):
                msg = f"Opened and {verb_success} {disp}."
            else:
                msg = f"Opened {disp} and {verb_success}."
            return {"success": True, "message": msg}
        else:
            msg = f"Opened {disp}, but couldn't {verb_fail}."
            return {"success": True, "partial": True, "message": msg}

    return {"success": False, "error": f"Unknown command type: {command_type}"}
