"""Read-only system resources, GPU, and battery inspection tools (Step 18)."""

import logging
import os
import shutil
import subprocess
from typing import Any
import psutil

logger = logging.getLogger("orion.tools.system")


def get_battery_status() -> dict[str, Any]:
    """Retrieve current battery percentage, charging state, and estimated time remaining."""
    battery = psutil.sensors_battery()
    if battery is None:
        return {
            "available": False,
            "message": "Battery sensor not available on this system.",
        }

    secs = None
    secsleft = getattr(battery, "secsleft", None)
    if isinstance(secsleft, (int, float)) and secsleft > 0:
        secs = int(secsleft)

    return {
        "available": True,
        "percent": round(battery.percent, 1),
        "plugged_in": bool(battery.power_plugged),
        "secs_left": secs,
    }


def get_system_resources() -> dict[str, Any]:
    """Retrieve current CPU utilization, RAM usage, and primary disk usage."""
    cpu_percent = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    root_drive = "C:\\" if os.name == "nt" else "/"
    disk = psutil.disk_usage(root_drive)

    return {
        "cpu_percent": round(cpu_percent, 1),
        "ram_used_gb": round(vm.used / (1024**3), 2),
        "ram_total_gb": round(vm.total / (1024**3), 2),
        "ram_free_gb": round(vm.available / (1024**3), 2),
        "ram_percent": round(vm.percent, 1),
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_free_gb": round(disk.free / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "disk_percent": round(disk.percent, 1),
    }


def _safe_float(val: str | None) -> float | None:
    """Safely convert a string to float, returning None for N/A, [Not Supported], or errors."""
    if not val:
        return None
    val_clean = val.strip().lower()
    if val_clean in ("n/a", "na", "[not supported]", "not supported", "unknown", "none", ""):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def get_gpu_status() -> dict[str, Any]:
    """Retrieve GPU utilization, temperature, and VRAM if supported local tools are available."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {
            "available": False,
            "message": "GPU metrics are not available on this system.",
        }

    try:
        out = subprocess.check_output(
            [
                smi,
                "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            shell=False,
            timeout=5.0,
        ).decode().strip()
        if not out:
            return {
                "available": False,
                "message": "GPU metrics are not available on this system.",
            }

        line = out.splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            gpu_pct = _safe_float(parts[0])
            temp_c = _safe_float(parts[1])
            used_mb = _safe_float(parts[2])
            total_mb = _safe_float(parts[3])

            # Return available: True if at least one metric parsed successfully
            if any(m is not None for m in (gpu_pct, temp_c, used_mb, total_mb)):
                used_gb = round(used_mb / 1024, 1) if used_mb is not None else None
                total_gb = round(total_mb / 1024, 1) if total_mb is not None else None
                return {
                    "available": True,
                    "gpu_percent": gpu_pct,
                    "temperature_c": temp_c,
                    "vram_used_mb": used_mb,
                    "vram_total_mb": total_mb,
                    "vram_used_gb": used_gb,
                    "vram_total_gb": total_gb,
                }
    except Exception as err:
        logger.debug("Failed querying GPU via nvidia-smi: %s", err)

    return {
        "available": False,
        "message": "GPU metrics are not available on this system.",
    }


def get_system_overview() -> dict[str, Any]:
    """Retrieve comprehensive system metrics: CPU, RAM, Disk, Battery, and GPU."""
    res = get_system_resources()
    battery = get_battery_status()
    gpu = get_gpu_status()
    return {
        **res,
        "battery": battery,
        "gpu": gpu,
    }


# =============================================================================
# Natural language formatters for system status queries (Step 18)
# =============================================================================

def _fmt_pct(val: Any) -> str:
    """Format numeric percentage cleanly without trailing decimals if whole."""
    if isinstance(val, (int, float)):
        return f"{int(round(val))}%"
    return f"{val}%"


def format_system_overview(data: dict[str, Any]) -> str:
    """Format full system status into a clean, human-friendly summary."""
    cpu = data.get("cpu_percent", 0.0)
    ram_pct = data.get("ram_percent", 0.0)

    if (isinstance(cpu, (int, float)) and cpu >= 85.0) or (isinstance(ram_pct, (int, float)) and ram_pct >= 90.0):
        headline = "Your laptop is under heavy load."
    else:
        headline = "Your laptop is doing fine."

    ram_used = data.get("ram_used_gb")
    ram_total = data.get("ram_total_gb")
    lines = [
        headline,
        f"CPU: {_fmt_pct(cpu)}",
        f"RAM: {ram_used}/{ram_total} GB ({_fmt_pct(ram_pct)})",
    ]

    battery = data.get("battery", {})
    if battery.get("available"):
        plugged = "charging" if battery.get("plugged_in") else "on battery"
        lines.append(f"Battery: {_fmt_pct(battery.get('percent'))}, {plugged}")

    disk_free = data.get("disk_free_gb")
    if disk_free is not None:
        lines.append(f"Storage: {disk_free} GB free")

    gpu = data.get("gpu", {})
    if gpu.get("available"):
        gpu_pct = gpu.get("gpu_percent")
        temp = gpu.get("temperature_c")
        gpu_parts: list[str] = []
        if gpu_pct is not None:
            gpu_parts.append(_fmt_pct(gpu_pct))
        if temp is not None:
            temp_str = f"{int(round(temp)) if isinstance(temp, (int, float)) else temp}°C"
            gpu_parts.append(temp_str)
        if not gpu_parts:
            used_gb = gpu.get("vram_used_gb")
            total_gb = gpu.get("vram_total_gb")
            if used_gb is not None and total_gb is not None:
                gpu_parts.append(f"{used_gb}/{total_gb} GB VRAM")
            else:
                gpu_parts.append("Available")
        lines.append(f"GPU: {', '.join(gpu_parts)}")

    return "\n".join(lines)


def format_cpu_status(data: dict[str, Any]) -> str:
    """Format CPU usage status."""
    return f"CPU usage is currently at {data.get('cpu_percent', 0.0)}%."


def format_ram_status(data: dict[str, Any]) -> str:
    """Format RAM usage status."""
    used = data.get("ram_used_gb")
    total = data.get("ram_total_gb")
    pct = data.get("ram_percent")
    pct_str = _fmt_pct(pct)
    free = data.get("ram_free_gb")
    free_str = f", {free} GB free" if free is not None else ""
    return f"RAM usage: {used}/{total} GB ({pct_str}){free_str}."


def format_storage_status(data: dict[str, Any]) -> str:
    """Format storage / disk usage status."""
    free = data.get("disk_free_gb")
    total = data.get("disk_total_gb")
    pct = data.get("disk_percent")
    if free is not None and total is not None:
        return f"Storage: {free} GB free out of {total} GB ({pct}% used)."
    return f"Storage: {data.get('disk_used_gb')}/{total} GB ({pct}% used)."


def format_battery_status_natural(battery: dict[str, Any]) -> str:
    """Format battery percentage and state naturally."""
    if not battery.get("available"):
        return "Battery sensor is not available on this system."
    pct = battery.get("percent")
    plugged = battery.get("plugged_in")
    if plugged:
        return f"Battery: {pct}%, plugged in and charging."
    secs = battery.get("secs_left")
    time_str = f" (~{secs // 3600}h {(secs % 3600) // 60}m remaining)" if secs and secs > 0 else ""
    return f"Battery: {pct}%, on battery{time_str}."


def format_charging_status(battery: dict[str, Any]) -> str:
    """Answer whether laptop is charging."""
    if not battery.get("available"):
        return "Battery sensor is not available on this system."
    pct = battery.get("percent")
    plugged = battery.get("plugged_in")
    if plugged:
        return f"Yes, your laptop is plugged in and charging ({pct}%)."
    return f"No, your laptop is running on battery ({pct}%)."


def format_gpu_status_natural(gpu: dict[str, Any]) -> str:
    """Format GPU utilization, temperature, and VRAM."""
    if not gpu.get("available"):
        return "GPU metrics are not available on this system."
    pct = gpu.get("gpu_percent")
    temp = gpu.get("temperature_c")
    used = gpu.get("vram_used_gb")
    total = gpu.get("vram_total_gb")

    parts: list[str] = []
    if pct is not None:
        parts.append(f"{pct}% utilization")
    if temp is not None:
        parts.append(f"{temp}°C")
    if used is not None and total is not None:
        parts.append(f"{used}/{total} GB VRAM used")
    elif used is not None:
        parts.append(f"{used} GB VRAM used")

    if not parts:
        return "GPU is available, but specific metrics are not currently reporting."
    return f"GPU: {', '.join(parts)}."
