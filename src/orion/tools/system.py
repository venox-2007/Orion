"""Read-only system resources and battery inspection tools."""

import os
from typing import Any
import psutil


def get_battery_status() -> dict[str, Any]:
    """Retrieve current battery percentage, charging state, and estimated time remaining."""
    battery = psutil.sensors_battery()
    if battery is None:
        return {
            "available": False,
            "message": "Battery sensor not available on this system.",
        }

    return {
        "available": True,
        "percent": round(battery.percent, 1),
        "plugged_in": battery.power_plugged,
        "secs_left": battery.secsleft if battery.secsleft > 0 else None,
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
        "ram_percent": round(vm.percent, 1),
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "disk_percent": round(disk.percent, 1),
    }
