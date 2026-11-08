"""Unit tests for Step 23 — Process Management, System Power States & Web Navigation."""

from collections import namedtuple
from unittest.mock import MagicMock, patch
import pytest

from orion.tools.process_manager import (
    SYSTEM_PROCESS_BLOCKLIST,
    format_top_processes,
    get_top_resource_processes,
    terminate_application,
)
from orion.tools.router import (
    check_deterministic_process_and_power_query,
    route_and_execute_tool,
)
from orion.tools.workstation import (
    HWND_BROADCAST,
    MONITOR_OFF,
    SC_MONITORPOWER,
    WM_SYSCOMMAND,
    open_browser_url,
    open_web_search,
    sleep_system,
    turn_off_screen,
)

MemInfo = namedtuple("MemInfo", ["rss", "vms"])


class MockProcess:
    """Mock psutil.Process instance for testing."""

    def __init__(
        self,
        pid: int,
        name: str,
        cpu_percent: float = 0.0,
        memory_percent: float = 0.0,
        rss_bytes: int = 100 * 1024 * 1024,
    ):
        self._pid = pid
        self._name = name
        self._cpu_percent = cpu_percent
        self._memory_percent = memory_percent
        self._rss_bytes = rss_bytes
        self.terminated = False
        self.killed = False

    @property
    def info(self):
        return {
            "pid": self._pid,
            "name": self._name,
            "cpu_percent": self._cpu_percent,
            "memory_percent": self._memory_percent,
            "memory_info": MemInfo(rss=self._rss_bytes, vms=self._rss_bytes * 2),
        }

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_system_blocklist_guard():
    """Verify that critical system processes in the blocklist are strictly rejected."""
    # Test with both .exe and without .exe
    blocked_targets = ["csrss.exe", "csrss", "explorer.exe", "explorer", "system", "dwm.exe", "services.exe"]
    for target in blocked_targets:
        success, message = terminate_application(target)
        assert success is False
        assert "Cannot terminate critical Windows system process" in message


def test_terminate_application_mocked():
    """Verify terminate_application finds matching processes and terminates or kills them."""
    notepad_proc = MockProcess(pid=1234, name="notepad.exe")
    mock_procs = [
        MockProcess(pid=100, name="svchost.exe"),
        notepad_proc,
        MockProcess(pid=2000, name="code.exe"),
    ]

    # 1. Graceful termination with alias "notepad"
    with patch("psutil.process_iter", return_value=mock_procs), patch("psutil.wait_procs", return_value=([notepad_proc], [])):
        success, msg = terminate_application("notepad", force=False)
        assert success is True
        assert notepad_proc.terminated is True
        assert notepad_proc.killed is False
        assert "Successfully closed 1 instance(s) of *Notepad*" in msg

    # 2. Force kill with "notepad.exe"
    notepad_proc2 = MockProcess(pid=5678, name="notepad.exe")
    mock_procs2 = [notepad_proc2]
    with patch("psutil.process_iter", return_value=mock_procs2), patch("psutil.wait_procs", return_value=([notepad_proc2], [])):
        success, msg = terminate_application("notepad.exe", force=True)
        assert success is True
        assert notepad_proc2.killed is True
        assert "Successfully closed 1 instance(s) of *Notepad.Exe*" in msg

    # 3. Process not found
    with patch("psutil.process_iter", return_value=[]):
        success, msg = terminate_application("nonexistent_app")
        assert success is False
        assert "No running instances of 'nonexistent_app' were found." in msg


def test_get_top_processes_ordering():
    """Verify get_top_resource_processes correctly orders by CPU vs RAM and formats properly."""
    p_high_cpu = MockProcess(pid=101, name="python.exe", cpu_percent=75.5, rss_bytes=200 * 1024 * 1024)
    p_high_ram = MockProcess(pid=102, name="chrome.exe", cpu_percent=5.0, rss_bytes=2 * 1024 * 1024 * 1024)
    p_mid = MockProcess(pid=103, name="code.exe", cpu_percent=25.0, rss_bytes=500 * 1024 * 1024)

    mock_procs = [p_high_cpu, p_high_ram, p_mid]

    with patch("psutil.process_iter", return_value=mock_procs):
        # Sort by CPU: high_cpu (75.5%) -> mid (25.0%) -> high_ram (5.0%)
        top_cpu = get_top_resource_processes(metric="cpu", limit=3)
        assert len(top_cpu) == 3
        assert top_cpu[0]["pid"] == 101
        assert top_cpu[1]["pid"] == 103
        assert top_cpu[2]["pid"] == 102

        cpu_text = format_top_processes(top_cpu, metric="cpu")
        assert "Top Processes by CPU:" in cpu_text
        assert "Python" in cpu_text
        assert "75.5% CPU" in cpu_text

    with patch("psutil.process_iter", return_value=mock_procs):
        # Sort by RAM: high_ram (2GB) -> mid (500MB) -> high_cpu (200MB)
        top_ram = get_top_resource_processes(metric="ram", limit=3)
        assert len(top_ram) == 3
        assert top_ram[0]["pid"] == 102
        assert top_ram[1]["pid"] == 103
        assert top_ram[2]["pid"] == 101

        ram_text = format_top_processes(top_ram, metric="ram")
        assert "Top Processes by RAM:" in ram_text
        assert "Chrome" in ram_text
        assert "2.0 GB RAM" in ram_text


def test_display_and_power_mocked():
    """Verify turn_off_screen, sleep_system, and browser launchers invoke respective APIs."""
    # 1. Screen off
    with patch("ctypes.windll.user32.SendMessageW") as mock_send_msg:
        assert turn_off_screen() is True
        mock_send_msg.assert_called_once_with(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF)

    # 2. System sleep
    with patch("ctypes.windll.PowrProf.SetSuspendState") as mock_suspend:
        assert sleep_system() is True
        mock_suspend.assert_called_once_with(0, 1, 0)

    # 3. Direct URL
    with patch("webbrowser.open") as mock_wb_open:
        assert open_browser_url("github.com") is True
        mock_wb_open.assert_called_once_with("https://github.com")

        mock_wb_open.reset_mock()
        assert open_browser_url("https://python.org") is True
        mock_wb_open.assert_called_once_with("https://python.org")

    # 4. Web search
    with patch("webbrowser.open") as mock_wb_open:
        assert open_web_search("python asyncio") is True
        mock_wb_open.assert_called_once_with("https://www.google.com/search?q=python+asyncio")

        mock_wb_open.reset_mock()
        assert open_web_search("lofi beats", engine="youtube") is True
        mock_wb_open.assert_called_once_with("https://www.youtube.com/results?search_query=lofi+beats")


@pytest.mark.asyncio
async def test_router_queries():
    """Verify router handles process management, display, sleep, and web queries without invoking Ollama."""
    # 1. Close application
    notepad_proc = MockProcess(pid=777, name="chrome.exe")
    with patch("psutil.process_iter", return_value=[notepad_proc]), patch("psutil.wait_procs", return_value=([notepad_proc], [])):
        res = await route_and_execute_tool("close chrome")
        assert res is not None
        assert res.get("handled") is True
        assert "Successfully closed 1 instance(s) of *Chrome*" in res.get("reply", "")

    # 2. Kill application
    np = MockProcess(pid=888, name="notepad.exe")
    with patch("psutil.process_iter", return_value=[np]), patch("psutil.wait_procs", return_value=([np], [])):
        res = await route_and_execute_tool("kill notepad")
        assert res is not None
        assert res.get("handled") is True
        assert np.killed is True

    # 3. Resource hogs / top processes
    proc_sample = [MockProcess(pid=999, name="python.exe", cpu_percent=88.0, rss_bytes=100 * 1024 * 1024)]
    with patch("psutil.process_iter", return_value=proc_sample):
        res = await route_and_execute_tool("what's eating my ram")
        assert res is not None
        assert res.get("handled") is True
        assert "Top Processes by RAM:" in res.get("reply", "")

        res_cpu = await route_and_execute_tool("top cpu processes")
        assert res_cpu is not None
        assert res_cpu.get("handled") is True
        assert "Top Processes by CPU:" in res_cpu.get("reply", "")

    # 4. Screen off
    with patch("ctypes.windll.user32.SendMessageW"):
        res = await route_and_execute_tool("turn off screen")
        assert res is not None
        assert res.get("handled") is True
        assert "Display turned off." in res.get("reply", "")

    # 5. Sleep system
    with patch("ctypes.windll.PowrProf.SetSuspendState"):
        res = await route_and_execute_tool("put computer to sleep")
        assert res is not None
        assert res.get("handled") is True
        assert "System putting itself to sleep." in res.get("reply", "")

    # 6. Direct URL navigation
    with patch("webbrowser.open") as mock_wb:
        res = await route_and_execute_tool("open github.com")
        assert res is not None
        assert res.get("handled") is True
        assert "Opening *https://github.com*" in res.get("reply", "")
        mock_wb.assert_called_once_with("https://github.com")

    # 7. Web search navigation
    with patch("webbrowser.open") as mock_wb:
        res = await route_and_execute_tool("search for python tutorials")
        assert res is not None
        assert res.get("handled") is True
        assert "Searching Google for: _python tutorials_" in res.get("reply", "")
        mock_wb.assert_called_once_with("https://www.google.com/search?q=python+tutorials")

    # 8. Non-app exclusions (e.g. close window, stop music) should NOT be hijacked by process termination
    res_window = check_deterministic_process_and_power_query("close window")
    assert res_window is None

    res_playback = check_deterministic_process_and_power_query("stop music")
    assert res_playback is None


@pytest.mark.asyncio
async def test_no_raw_html_in_router_responses(tmp_path):
    """Regression test: verify router responses do not emit raw HTML tags."""
    import re
    from pathlib import Path
    from orion.tools.router import route_and_execute_tool

    dummy_file = tmp_path / "sample.pdf"
    dummy_file.write_bytes(b"%PDF dummy")

    with patch("webbrowser.open"), \
         patch("orion.tools.file_bridge.get_latest_file", return_value=dummy_file), \
         patch("orion.tools.file_bridge.is_path_allowed", return_value=True):
        queries = [
            "open github.com",
            "search for test",
            "show tasks",
            "send me the latest file",
        ]
        for query in queries:
            resp = await route_and_execute_tool(query)
            assert resp is not None, f"Query '{query}' returned None"
            assert resp.get("handled") is True
            # For file fetch, reply may be in reply or caption
            reply = resp.get("reply") or resp.get("caption") or ""
            match = re.search(r"</?(?:b|i|s|code|pre)>", reply)
            assert match is None, f"Found raw HTML tag {match.group(0)} in response for query '{query}': {resp}"
