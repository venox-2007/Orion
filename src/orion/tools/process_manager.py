"""Process monitoring and safe application termination for ORION (Step 23)."""

from __future__ import annotations

import logging
from typing import Any
import psutil

logger = logging.getLogger("orion.tools.process_manager")

# Critical Windows processes that must NEVER be terminated
SYSTEM_PROCESS_BLOCKLIST = {
    "system",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "explorer.exe",
    "winlogon.exe",
    "dwm.exe",
    "fontdrvhost.exe",
}

# Known process name aliases for user conveniences
KNOWN_APP_ALIASES: dict[str, str] = {
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "notepad": "notepad.exe",
    "vscode": "code.exe",
    "vs code": "code.exe",
    "code": "code.exe",
    "word": "winword.exe",
    "ms word": "winword.exe",
    "excel": "excel.exe",
    "ms excel": "excel.exe",
    "powerpoint": "powerpnt.exe",
    "spotify": "spotify.exe",
    "calculator": "calculatorapp.exe",
    "calc": "calculatorapp.exe",
    "telegram": "telegram.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "terminal": "windowsterminal.exe",
    "powershell": "powershell.exe",
    "cmd": "cmd.exe",
}


def get_top_resource_processes(metric: str = "cpu", limit: int = 5) -> list[dict[str, Any]]:
    """Retrieve top running processes sorted by CPU or RAM utilization."""
    clean_metric = metric.strip().lower()
    use_mem = clean_metric in ("ram", "memory", "mem")

    processes: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or "Unknown"
            pid = info.get("pid")
            cpu = info.get("cpu_percent") or 0.0
            mem_pct = info.get("memory_percent") or 0.0
            mem_info = info.get("memory_info")
            rss = mem_info.rss if mem_info else 0
            rss_mb = rss / (1024 * 1024)
            rss_gb = rss / (1024 * 1024 * 1024)

            # Clean name without .exe
            clean_name = name
            if clean_name.lower().endswith(".exe"):
                clean_name = clean_name[:-4]

            processes.append({
                "pid": pid,
                "name": clean_name.title() if clean_name.islower() else clean_name,
                "raw_name": name,
                "cpu_percent": round(cpu, 1),
                "memory_percent": round(mem_pct, 1),
                "memory_bytes": rss,
                "memory_mb": round(rss_mb, 1),
                "memory_gb": round(rss_gb, 2),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if use_mem:
        processes.sort(key=lambda x: (x["memory_bytes"], x["cpu_percent"]), reverse=True)
    else:
        processes.sort(key=lambda x: (x["cpu_percent"], x["memory_bytes"]), reverse=True)

    return processes[:limit]


def format_top_processes(processes: list[dict[str, Any]], metric: str = "cpu") -> str:
    """Format process inspection results into clean markdown."""
    clean_metric = metric.strip().lower()
    metric_label = "RAM" if clean_metric in ("ram", "memory", "mem") else "CPU"

    if not processes:
        return f"📊 *Top Processes by {metric_label}:*\nNo active processes found."

    lines = [f"📊 *Top Processes by {metric_label}:*"]
    for p in processes:
        if p["memory_gb"] >= 1.0:
            ram_str = f"{p['memory_gb']:.1f} GB RAM"
        else:
            ram_str = f"{p['memory_mb']:.0f} MB RAM"
        lines.append(f"• *{p['name']}* (PID: {p['pid']}) — {p['cpu_percent']:.1f}% CPU | {ram_str}")

    return "\n".join(lines)


def terminate_application(app_name: str, force: bool = False) -> tuple[bool, str]:
    """Gracefully terminate or force-kill specified user applications by name or alias."""
    clean_target = app_name.strip().lower()
    target_exe = KNOWN_APP_ALIASES.get(clean_target, clean_target)
    if not target_exe.endswith(".exe"):
        target_candidates = {target_exe, f"{target_exe}.exe"}
    else:
        target_candidates = {target_exe, target_exe[:-4]}

    # Check against System Process Blocklist
    for cand in target_candidates:
        if cand in SYSTEM_PROCESS_BLOCKLIST or f"{cand}.exe" in SYSTEM_PROCESS_BLOCKLIST:
            return (False, "⚠️ Cannot terminate critical Windows system process.")

    matching_procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            p_name = (proc.info.get("name") or "").lower()
            if p_name in target_candidates or (p_name.endswith(".exe") and p_name[:-4] in target_candidates):
                matching_procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not matching_procs:
        return (False, f"No running instances of '{app_name}' were found.")

    count = len(matching_procs)
    for p in matching_procs:
        try:
            if force:
                p.kill()
            else:
                p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    try:
        gone, alive = psutil.wait_procs(matching_procs, timeout=1.5)
        if alive and not force:
            for p in alive:
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
    except Exception:
        pass

    display_name = app_name.strip().title()
    return (True, f"🛑 Successfully closed {count} instance(s) of *{display_name}*.")
