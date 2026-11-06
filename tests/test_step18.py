"""Tests for Step 18: Enhanced System Awareness in ORION."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.integrations.telegram_bot import create_telegram_app
from orion.tools.desktop_inspector import WindowInfo
from orion.tools.router import (
    check_deterministic_system_query,
    check_deterministic_window_query,
    route_and_execute_tool,
)
from orion.tools.system import (
    format_battery_status_natural,
    format_charging_status,
    format_cpu_status,
    format_gpu_status_natural,
    format_ram_status,
    format_storage_status,
    format_system_overview,
    get_battery_status,
    get_gpu_status,
    get_system_overview,
    get_system_resources,
)


def _mock_system_metrics(
    cpu_pct=21.0,
    ram_used_gb=8.4,
    ram_total_gb=16.0,
    ram_pct=53.0,
    ram_free_gb=7.6,
    disk_used_gb=120.0,
    disk_free_gb=120.0,
    disk_total_gb=476.0,
    disk_pct=25.0,
):
    mock_vm = MagicMock()
    mock_vm.used = int(ram_used_gb * (1024**3))
    mock_vm.total = int(ram_total_gb * (1024**3))
    mock_vm.percent = ram_pct
    mock_vm.available = int(ram_free_gb * (1024**3))

    mock_disk = MagicMock()
    mock_disk.used = int(disk_used_gb * (1024**3))
    mock_disk.total = int(disk_total_gb * (1024**3))
    mock_disk.free = int(disk_free_gb * (1024**3))
    mock_disk.percent = disk_pct

    return mock_vm, mock_disk


# ===========================================================================
# 1. SYSTEM OVERVIEW NATURAL RESPONSES
# ===========================================================================

def test_system_overview_natural_response(monkeypatch):
    """'How's my laptop doing?' produces simple, human-friendly summary."""
    mock_vm, mock_disk = _mock_system_metrics()
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=None: 21.0)
    monkeypatch.setattr("psutil.virtual_memory", lambda: mock_vm)
    monkeypatch.setattr("psutil.disk_usage", lambda path: mock_disk)

    mock_battery = MagicMock()
    mock_battery.percent = 82.0
    mock_battery.power_plugged = True
    mock_battery.secsleft = -1
    monkeypatch.setattr("psutil.sensors_battery", lambda: mock_battery)

    mock_gpu = {
        "available": True,
        "gpu_percent": 0.0,
        "temperature_c": 39.0,
        "vram_used_gb": 0.0,
        "vram_total_gb": 6.0,
    }
    monkeypatch.setattr("orion.tools.system.get_gpu_status", lambda: mock_gpu)

    owner_id = 1234
    ctx = {"user_id": owner_id, "owner_id": owner_id}

    for phrase in [
        "How's my laptop doing?",
        "How is my PC doing?",
        "How's my computer?",
        "System status",
        "What's my system status?",
    ]:
        res = check_deterministic_system_query(phrase, context=ctx)
        assert res is not None
        assert res["handled"] is True
        reply = res["reply"]
        assert "Your laptop is doing fine." in reply
        assert "CPU: 21" in reply
        assert "RAM: 8.4/16.0 GB (53%)" in reply
        assert "Battery: 82%, charging" in reply
        assert "Storage: 120.0 GB free" in reply
        assert "GPU: 0%, 39°C" in reply


def test_system_overview_heavy_load_status(monkeypatch):
    """When CPU is elevated, summary accurately notes heavy load."""
    mock_vm, mock_disk = _mock_system_metrics(cpu_pct=92.0)
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=None: 92.0)
    monkeypatch.setattr("psutil.virtual_memory", lambda: mock_vm)
    monkeypatch.setattr("psutil.disk_usage", lambda path: mock_disk)
    monkeypatch.setattr("psutil.sensors_battery", lambda: None)
    monkeypatch.setattr("orion.tools.system.get_gpu_status", lambda: {"available": False})

    res = check_deterministic_system_query("How is my PC doing?")
    assert res is not None
    reply = res["reply"]
    assert "Your laptop is under heavy load." in reply
    assert "CPU: 92" in reply


# ===========================================================================
# 2. SPECIFIC RESOURCE QUERIES (CPU, RAM, STORAGE, BATTERY, GPU)
# ===========================================================================

def test_cpu_usage_query(monkeypatch):
    """'What's my CPU usage?' returns direct CPU percentage."""
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=None: 25.5)

    for phrase in [
        "What's my CPU usage?",
        "What is my CPU load?",
        "CPU usage",
        "How is my CPU doing?",
    ]:
        res = check_deterministic_system_query(phrase)
        assert res is not None
        assert "CPU usage is currently at 25.5%." in res["reply"]


def test_ram_usage_query(monkeypatch):
    """'How much RAM am I using?' returns used/total/percentage."""
    mock_vm, _ = _mock_system_metrics(ram_used_gb=8.4, ram_total_gb=16.0, ram_pct=53.0, ram_free_gb=7.6)
    monkeypatch.setattr("psutil.virtual_memory", lambda: mock_vm)

    for phrase in [
        "How much RAM am I using?",
        "What's my RAM usage?",
        "RAM usage",
        "How much memory is left?",
    ]:
        res = check_deterministic_system_query(phrase)
        assert res is not None
        assert "RAM usage: 8.4/16.0 GB (53%), 7.6 GB free." in res["reply"]


def test_storage_free_query(monkeypatch):
    """'How much storage is left?' returns free and total storage."""
    _, mock_disk = _mock_system_metrics(disk_free_gb=165.2, disk_total_gb=476.0, disk_pct=65.3)
    monkeypatch.setattr("psutil.disk_usage", lambda path: mock_disk)

    for phrase in [
        "How much storage is left?",
        "How much disk space is left?",
        "Storage free",
        "What's my storage status?",
    ]:
        res = check_deterministic_system_query(phrase)
        assert res is not None
        assert "Storage: 165.2 GB free out of 476.0 GB (65.3% used)." in res["reply"]


def test_battery_charging_query(monkeypatch):
    """'Is my laptop charging?' confirms plugged-in state."""
    mock_battery = MagicMock()
    mock_battery.percent = 85.0
    mock_battery.power_plugged = True
    monkeypatch.setattr("psutil.sensors_battery", lambda: mock_battery)

    for phrase in [
        "Is my laptop charging?",
        "Is my PC plugged in?",
        "Is it charging?",
    ]:
        res = check_deterministic_system_query(phrase)
        assert res is not None
        assert "Yes, your laptop is plugged in and charging (85.0%)." in res["reply"]


def test_battery_discharging_query(monkeypatch):
    """Battery percentage and discharging state reported accurately."""
    mock_battery = MagicMock()
    mock_battery.percent = 62.0
    mock_battery.power_plugged = False
    mock_battery.secsleft = 5400  # 1.5 hours
    monkeypatch.setattr("psutil.sensors_battery", lambda: mock_battery)

    # 1. Charging question
    res_chg = check_deterministic_system_query("Is my laptop charging?")
    assert res_chg is not None
    assert "No, your laptop is running on battery (62.0%)." in res_chg["reply"]

    # 2. Percentage question
    res_pct = check_deterministic_system_query("What's my battery percentage?")
    assert res_pct is not None
    assert "Battery: 62.0%, on battery (~1h 30m remaining)." in res_pct["reply"]


def test_battery_sensor_unavailable(monkeypatch):
    """When battery sensor returns None (e.g. desktop PC), reports honest N/A."""
    monkeypatch.setattr("psutil.sensors_battery", lambda: None)

    res_chg = check_deterministic_system_query("Is my laptop charging?")
    assert "Battery sensor is not available on this system." in res_chg["reply"]

    res_pct = check_deterministic_system_query("What's my battery percentage?")
    assert "Battery sensor is not available on this system." in res_pct["reply"]


def test_gpu_metrics_available(monkeypatch):
    """'What's my GPU usage?' parses nvidia-smi output safely without shell=True."""
    monkeypatch.setattr("shutil.which", lambda cmd: "C:\\WINDOWS\\system32\\nvidia-smi.exe")
    monkeypatch.setattr(
        "subprocess.check_output",
        lambda *args, **kwargs: b"12, 45, 1536, 6144\n",
    )

    for phrase in [
        "What's my GPU usage?",
        "GPU status",
        "What's my GPU temperature?",
    ]:
        res = check_deterministic_system_query(phrase)
        assert res is not None
        assert "GPU: 12.0% utilization, 45.0°C, 1.5/6.0 GB VRAM used." in res["reply"]


def test_gpu_metrics_unavailable(monkeypatch):
    """When nvidia-smi is not found, reports clean unsupported notice."""
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    res = check_deterministic_system_query("What's my GPU usage?")
    assert res is not None
    assert "GPU metrics are not available on this system." in res["reply"]


# ===========================================================================
# 3. RUNNING APPLICATION AWARENESS
# ===========================================================================

@pytest.mark.asyncio
async def test_running_apps_query(monkeypatch):
    """'What apps are running?' returns list of open approved applications."""
    mock_windows = [
        WindowInfo(hwnd=0x1001, app_id="vscode", display_name="Visual Studio Code", state="Normal", is_active=True),
        WindowInfo(hwnd=0x1002, app_id="notepad", display_name="Notepad", state="Minimized", is_active=False),
    ]
    monkeypatch.setattr("orion.tools.desktop_inspector._enumerate_windows_native", lambda: mock_windows)

    res = await route_and_execute_tool("What apps are running?")
    assert res is not None
    assert res["handled"] is True
    reply = res["reply"]
    assert "Visual Studio Code" in reply
    assert "Notepad" in reply
    assert "Open Application Windows" in reply


@pytest.mark.asyncio
async def test_is_specific_app_running_positive(monkeypatch):
    """'Is VS Code running?' confirms application is running."""
    mock_windows = [
        WindowInfo(hwnd=0x1001, app_id="vscode", display_name="Visual Studio Code", state="Normal", is_active=True),
    ]
    monkeypatch.setattr("orion.tools.desktop_inspector._enumerate_windows_native", lambda: mock_windows)

    for phrase in [
        "Is VS Code running?",
        "Is Visual Studio Code open?",
    ]:
        res = await route_and_execute_tool(phrase)
        assert res is not None
        assert res["handled"] is True
        assert "Yes, **Visual Studio Code** is currently running" in res["reply"]
        assert "1 Normal" in res["reply"]


@pytest.mark.asyncio
async def test_is_specific_app_running_negative(monkeypatch):
    """'Is Calculator running?' indicates application is not running."""
    mock_windows = [
        WindowInfo(hwnd=0x1001, app_id="vscode", display_name="Visual Studio Code", state="Normal", is_active=True),
    ]
    monkeypatch.setattr("orion.tools.desktop_inspector._enumerate_windows_native", lambda: mock_windows)

    res = await route_and_execute_tool("Is Calculator running?")
    assert res is not None
    assert res["handled"] is True
    assert "No, **Calculator** is not currently running." in res["reply"]


@pytest.mark.asyncio
async def test_is_unapproved_app_rejected(monkeypatch):
    """'Is Steam running?' rejects unapproved application attempt."""
    res = await route_and_execute_tool("Is Steam running?")
    assert res is not None
    assert res["handled"] is True
    assert "not in the approved application allowlist" in res["reply"]


@pytest.mark.asyncio
async def test_active_window_query(monkeypatch):
    """'What's my active window?' reports current foreground approved window."""
    mock_win = WindowInfo(hwnd=0x1001, app_id="vscode", display_name="Visual Studio Code", state="Normal", is_active=True)
    monkeypatch.setattr("orion.tools.desktop_inspector._get_foreground_hwnd_native", lambda: 0x1001)
    monkeypatch.setattr(
        "orion.tools.desktop_inspector._get_real_window_process_info",
        lambda hwnd: (1234, "C:\\Program Files\\Microsoft VS Code\\Code.exe"),
    )
    monkeypatch.setattr("orion.tools.desktop_inspector._inspect_window_state_native", lambda hwnd: "Normal")

    for phrase in [
        "What's my active window?",
        "What is the active window?",
        "Which app is active?",
    ]:
        res = await route_and_execute_tool(phrase)
        assert res is not None
        assert res["handled"] is True
        assert "Active Application" in res["reply"]
        assert "Visual Studio Code" in res["reply"]


# ===========================================================================
# 4. CONVERSATIONAL FALLBACK FOR CONCEPTUAL QUESTIONS
# ===========================================================================

def test_conceptual_questions_fallback_to_conversation():
    """General questions about CPU, RAM, GPU concepts do not trigger system monitoring."""
    conceptual_queries = [
        "What is a CPU?",
        "What is RAM?",
        "What is a GPU?",
        "How does a GPU work?",
        "Why do computers need RAM?",
        "What does CPU stand for?",
        "Explain GPU architectures",
        "Define RAM memory",
        "What are the differences between CPU and GPU?",
    ]
    for q in conceptual_queries:
        res = check_deterministic_system_query(q)
        assert res is None, f"Expected None for conceptual question '{q}', got {res}"


# ===========================================================================
# 5. OWNER AUTHORIZATION GUARDS
# ===========================================================================

def test_unauthorized_user_system_query_rejected():
    """Non-owner users cannot inspect laptop resources."""
    owner_id = 1111
    intruder_id = 9999
    ctx = {"user_id": intruder_id, "owner_id": owner_id}

    for phrase in [
        "What's my CPU usage?",
        "How's my laptop doing?",
        "Is my laptop charging?",
        "How much storage is left?",
    ]:
        res = check_deterministic_system_query(phrase, context=ctx)
        assert res is not None
        assert res["handled"] is True
        assert "Access restricted" in res["reply"]


# ===========================================================================
# 6. INTEGRATION WITH TELEGRAM BOT TEXT AND VOICE PIPELINE
# ===========================================================================

@pytest.mark.asyncio
async def test_telegram_system_awareness_message_pipeline(monkeypatch):
    """Telegram message pipeline processes system awareness questions cleanly."""
    owner_id = 777
    token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

    mock_vm, mock_disk = _mock_system_metrics()
    monkeypatch.setattr("psutil.cpu_percent", lambda interval=None: 15.0)
    monkeypatch.setattr("psutil.virtual_memory", lambda: mock_vm)
    monkeypatch.setattr("psutil.disk_usage", lambda path: mock_disk)
    monkeypatch.setattr("psutil.sensors_battery", lambda: None)
    monkeypatch.setattr("orion.tools.system.get_gpu_status", lambda: {"available": False})

    app = create_telegram_app(token=token, owner_id=owner_id)
    message_handler_func = app.handlers[0][-1].callback

    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()

    update = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = owner_id
    update.effective_user.id = owner_id
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.text = "How's my laptop doing?"

    await message_handler_func(update, context)

    update.effective_message.reply_text.assert_called_once()
    reply = update.effective_message.reply_text.call_args[0][0]
    assert "Your laptop is doing fine." in reply
    assert "CPU: 15" in reply
