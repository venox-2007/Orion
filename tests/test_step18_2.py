"""Unit tests for Step 18.2: GPU Metrics Timeout & Parsing Resilience."""

from unittest.mock import MagicMock
import pytest

from orion.tools.system import format_gpu_status_natural, format_system_overview, get_gpu_status


def test_normal_gpu_metrics_parsing_and_timeout(monkeypatch):
    """Normal nvidia-smi CSV output parses correctly and verifies timeout=5.0."""
    monkeypatch.setattr("shutil.which", lambda cmd: r"C:\Windows\System32\nvidia-smi.exe")

    mock_check_output = MagicMock(return_value=b"15.0, 48.0, 1024, 6144\n")
    monkeypatch.setattr("subprocess.check_output", mock_check_output)

    status = get_gpu_status()
    assert status["available"] is True
    assert status["gpu_percent"] == 15.0
    assert status["temperature_c"] == 48.0
    assert status["vram_used_mb"] == 1024.0
    assert status["vram_total_mb"] == 6144.0
    assert status["vram_used_gb"] == 1.0
    assert status["vram_total_gb"] == 6.0

    # Verify timeout=5.0 was passed
    mock_check_output.assert_called_once()
    _, kwargs = mock_check_output.call_args
    assert kwargs.get("timeout") == 5.0

    # Verify natural formatting
    formatted = format_gpu_status_natural(status)
    assert formatted == "GPU: 15.0% utilization, 48.0°C, 1.0/6.0 GB VRAM used."


def test_partial_gpu_metrics_with_unsupported_utilization(monkeypatch):
    """Partial nvidia-smi output containing [Not Supported] or N/A still parses available: True with remaining valid metrics."""
    monkeypatch.setattr("shutil.which", lambda cmd: r"C:\Windows\System32\nvidia-smi.exe")

    mock_check_output = MagicMock(return_value=b"[Not Supported], 52.0, N/A, 6144\n")
    monkeypatch.setattr("subprocess.check_output", mock_check_output)

    status = get_gpu_status()
    assert status["available"] is True
    assert status["gpu_percent"] is None
    assert status["temperature_c"] == 52.0
    assert status["vram_used_mb"] is None
    assert status["vram_total_mb"] == 6144.0
    assert status["vram_total_gb"] == 6.0

    # Verify natural formatting handles missing fields gracefully without crashing
    formatted = format_gpu_status_natural(status)
    assert "52.0°C" in formatted
    assert "utilization" not in formatted

    # Verify format_system_overview handles missing utilization cleanly
    overview_data = {
        "cpu_percent": 10.0,
        "ram_used_gb": 8.0,
        "ram_total_gb": 16.0,
        "ram_percent": 50.0,
        "disk_free_gb": 100.0,
        "gpu": status,
    }
    overview = format_system_overview(overview_data)
    assert "GPU: 52°C" in overview
