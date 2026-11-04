"""Focused tests for Step 10: Windows Desktop Awareness."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.config.settings import Settings
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import get_approved_apps
from orion.tools.desktop_inspector import (
    WindowInfo,
    _get_process_path_native,
    _get_real_window_process_info,
    _match_process_to_approved_app,
    get_active_window,
    get_application_window_status,
    list_open_windows,
)
from orion.tools.registry import ToolRegistry
from orion.tools.router import (
    check_deterministic_window_query,
    format_tool_result,
    route_and_execute_tool,
)


@pytest.fixture
def step10_env(tmp_path: Path):
    """Fixture providing isolated database and mock settings."""
    db_file = tmp_path / "test_step10.db"
    settings = Settings(
        telegram_bot_token="test_token",
        telegram_owner_id=12345,
        database_path=str(db_file),
    )
    with patch("orion.config.settings.get_settings", return_value=settings), \
         patch("orion.memory.database.get_settings", return_value=settings):
        memory = ConversationMemory(db_path=str(db_file))
        yield {
            "db_path": str(db_file),
            "settings": settings,
            "memory": memory,
            "tmp_path": tmp_path,
        }


# 1. List approved open windows
def test_list_approved_open_windows():
    mock_windows = [
        WindowInfo(hwnd=1001, app_id="vscode", display_name="Visual Studio Code", state="Normal", is_active=True),
        WindowInfo(hwnd=1002, app_id="chatgpt", display_name="ChatGPT Desktop", state="Maximized", is_active=False),
    ]
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=mock_windows):
        res = list_open_windows()
        assert res["success"] is True
        assert res["total_windows"] == 2
        apps = res["open_windows"]
        assert len(apps) == 2
        vscode_info = next(a for a in apps if a["app_id"] == "vscode")
        assert vscode_info["window_count"] == 1
        assert vscode_info["states"] == ["Normal"]
        assert vscode_info["is_active"] is True


# 2. Exclude unapproved applications
def test_exclude_unapproved_applications():
    # Test that unapproved applications are rejected by process matching
    unapproved_cases = [
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "Chrome_WidgetWin_1"),
        (r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "ConsoleWindowClass"),
        (r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_1.0\wt.exe", "CASCADIA_HOSTING_WINDOW_CLASS"),
        (r"C:\Windows\System32\cmd.exe", "ConsoleWindowClass"),
        (r"C:\Program Files (x86)\Steam\steam.exe", "vguiPopupWindow"),
        (r"C:\Program Files\Discord\Discord.exe", "Chrome_WidgetWin_1"),
        (r"C:\Program Files\Spotify\Spotify.exe", "Chrome_WidgetWin_1"),
        # Shell windows under explorer.exe must be excluded
        (r"C:\Windows\explorer.exe", "Shell_TrayWnd"),
        (r"C:\Windows\explorer.exe", "Progman"),
        (r"C:\Windows\explorer.exe", "WorkerW"),
        (r"C:\Windows\explorer.exe", "Xaml_WindowedPopupClass"),
    ]
    for path, win_cls in unapproved_cases:
        matched = _match_process_to_approved_app(path, win_cls)
        assert matched is None

    # Real File Explorer window is accepted
    assert _match_process_to_approved_app(r"C:\Windows\explorer.exe", "CabinetWClass") == "file_explorer"
    assert _match_process_to_approved_app(r"C:\Windows\explorer.exe", "ExploreWClass") == "file_explorer"


# 3. Detect normal, minimized, and maximized states
def test_detect_normal_minimized_maximized_states():
    from orion.tools.desktop_inspector import _inspect_window_state_native

    # Test via mocked user32 functions
    with patch("ctypes.windll.user32.IsIconic", return_value=True), \
         patch("ctypes.windll.user32.IsZoomed", return_value=False):
        assert _inspect_window_state_native(1234) == "Minimized"

    with patch("ctypes.windll.user32.IsIconic", return_value=False), \
         patch("ctypes.windll.user32.IsZoomed", return_value=True):
        assert _inspect_window_state_native(1234) == "Maximized"

    with patch("ctypes.windll.user32.IsIconic", return_value=False), \
         patch("ctypes.windll.user32.IsZoomed", return_value=False), \
         patch("ctypes.windll.user32.GetWindowPlacement", return_value=False):
        assert _inspect_window_state_native(1234) == "Normal"


# 4. Multiple windows for the same application
def test_multiple_windows_for_same_application():
    mock_windows = [
        WindowInfo(hwnd=2001, app_id="excel", display_name="Microsoft Excel", state="Normal", is_active=False),
        WindowInfo(hwnd=2002, app_id="excel", display_name="Microsoft Excel", state="Minimized", is_active=False),
        WindowInfo(hwnd=2003, app_id="word", display_name="Microsoft Word", state="Maximized", is_active=True),
    ]
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=mock_windows):
        res = list_open_windows()
        assert res["success"] is True
        assert res["total_windows"] == 3
        apps = res["open_windows"]
        assert len(apps) == 2

        excel_info = next(a for a in apps if a["app_id"] == "excel")
        assert excel_info["window_count"] == 2
        assert set(excel_info["states"]) == {"Normal", "Minimized"}
        assert excel_info["is_active"] is False

        word_info = next(a for a in apps if a["app_id"] == "word")
        assert word_info["window_count"] == 1
        assert word_info["states"] == ["Maximized"]
        assert word_info["is_active"] is True


# 5. Correct foreground application
def test_correct_foreground_application():
    with patch("orion.tools.desktop_inspector._get_foreground_hwnd_native", return_value=3001), \
         patch("orion.tools.desktop_inspector._get_real_window_process_info", return_value=(999, r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE")), \
         patch("orion.tools.desktop_inspector._inspect_window_state_native", return_value="Maximized"):
        res = get_active_window()
        assert res["success"] is True
        assert res["has_active_approved_window"] is True
        assert res["app_id"] == "word"
        assert res["display_name"] == "Microsoft Word"
        assert res["state"] == "Maximized"
        assert "Microsoft Word" in res["message"]


# 6. Unknown foreground app hides its identity
def test_unknown_foreground_app_hides_identity():
    # If foreground window is an unapproved process (e.g. Chrome, PowerShell, game)
    with patch("orion.tools.desktop_inspector._get_foreground_hwnd_native", return_value=4001), \
         patch("orion.tools.desktop_inspector._get_real_window_process_info", return_value=(888, r"C:\Program Files\Google\Chrome\Application\chrome.exe")):
        res = get_active_window()
        assert res["success"] is True
        assert res["has_active_approved_window"] is False
        assert res["message"] == "The active application is not available to ORION."
        # Ensure no unapproved process path or application name is revealed
        assert "chrome" not in res["message"].lower()
        assert "Google" not in res["message"]


# 7. Reject unknown app_id in get_application_window_status
def test_reject_unknown_app_id():
    res1 = get_application_window_status("unapproved_app")
    assert res1["success"] is False
    assert "not in the approved" in res1["error"]

    res2 = get_application_window_status("chrome")
    assert res2["success"] is False
    assert "not in the approved" in res2["error"]


# 8. Distinguish running app from open window
def test_distinguish_running_app_from_open_window():
    # No open windows for word
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[]):
        res = get_application_window_status("word")
        assert res["success"] is True
        assert res["has_open_window"] is False
        assert res["window_count"] == 0
        assert res["states"] == []
        assert "does not currently have any open windows" in res["message"]


# 9. Safely handle disappearing windows
def test_safely_handle_disappearing_windows():
    # If a window closes or fails during enumeration
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", side_effect=OSError("Invalid window handle")):
        res = list_open_windows()
        assert res["success"] is False
        assert "unavailable" in res["error"].lower()

        res_active = get_active_window()
        assert res_active["has_active_approved_window"] is False
        assert res_active["message"] == "The active application is not available to ORION."


# 10. Safely handle process access denied
def test_safely_handle_process_access_denied():
    with patch("ctypes.windll.kernel32.OpenProcess", return_value=0):
        # Access denied returns empty path
        path = _get_process_path_native(1234)
        assert path == ""


# 11. Packaged app mapping, including unknown case
def test_packaged_app_mapping_including_unknown_case():
    # 1. ChatGPT in WindowsApps
    chatgpt_path = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1029.7803.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
    assert _match_process_to_approved_app(chatgpt_path, "Chrome_WidgetWin_1") == "chatgpt"

    # 2. Calculator in WindowsApps
    calc_path = r"C:\Program Files\WindowsApps\Microsoft.WindowsCalculator_11.2401.0.0_x64__8wekyb3d8bbwe\CalculatorApp.exe"
    assert _match_process_to_approved_app(calc_path, "ApplicationFrameWindow") == "calculator"

    # 3. Unknown packaged application in WindowsApps
    unknown_pkg = r"C:\Program Files\WindowsApps\Vendor.UnknownApp_1.0.0.0_x64__8wekyb3d8bbwe\UnknownApp.exe"
    assert _match_process_to_approved_app(unknown_pkg, "ApplicationFrameWindow") is None

    # 4. ApplicationFrameHost unwrapping
    def fake_get_pid(hwnd, byref_pid):
        byref_pid._obj.value = 500 if hwnd == 100 else 600
        return 1

    def fake_enum_child(hwnd, cb, lparam):
        cb(200, 0)
        return True

    with patch("ctypes.windll.user32.GetWindowThreadProcessId", side_effect=fake_get_pid), \
         patch("orion.tools.desktop_inspector._get_process_path_native") as mock_path, \
         patch("ctypes.windll.user32.EnumChildWindows", side_effect=fake_enum_child):
        mock_path.side_effect = lambda p: (
            r"C:\Windows\System32\ApplicationFrameHost.exe" if p == 500
            else r"C:\Program Files\WindowsApps\Microsoft.WindowsCalculator_11.2401.0.0_x64__8wekyb3d8bbwe\CalculatorApp.exe"
        )
        pid, path = _get_real_window_process_info(100)
        assert "calculator" in path.lower()


# 12. Owner-only /windows and /active commands
@pytest.mark.asyncio
async def test_owner_only_windows_and_active_commands(step10_env):
    from orion.integrations.telegram_bot import UNAUTHORIZED_MESSAGE, create_telegram_app

    memory = step10_env["memory"]
    owner_id = 12345
    token = "12345:mock_token"
    app = create_telegram_app(token=token, owner_id=owner_id, memory=memory)

    # Unauthorized user
    unauth_update = MagicMock()
    unauth_update.effective_chat.type = "private"
    unauth_update.effective_user.id = 99999
    unauth_msg = MagicMock()
    unauth_msg.reply_text = AsyncMock()
    unauth_update.effective_message = unauth_msg

    for handler in app.handlers.get(0, []):
        cmds = getattr(handler, "commands", None)
        if cmds and "windows" in cmds:
            await handler.callback(unauth_update, MagicMock())
            unauth_msg.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)
        if cmds and "active" in cmds:
            await handler.callback(unauth_update, MagicMock())
            unauth_msg.reply_text.assert_called_with(UNAUTHORIZED_MESSAGE)

    # Authorized owner
    auth_update = MagicMock()
    auth_update.effective_chat.type = "private"
    auth_update.effective_user.id = owner_id
    auth_msg = MagicMock()
    auth_msg.reply_text = AsyncMock()
    auth_update.effective_message = auth_msg

    mock_wins = [WindowInfo(hwnd=1, app_id="vscode", display_name="Visual Studio Code", state="Normal", is_active=True)]
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=mock_wins), \
         patch("orion.tools.desktop_inspector._get_foreground_hwnd_native", return_value=1), \
         patch("orion.tools.desktop_inspector._get_real_window_process_info", return_value=(1, r"C:\Users\user\AppData\Local\Programs\Microsoft VS Code\Code.exe")):
        for handler in app.handlers.get(0, []):
            cmds = getattr(handler, "commands", None)
            if cmds and "windows" in cmds:
                await handler.callback(auth_update, MagicMock())
            if cmds and "active" in cmds:
                await handler.callback(auth_update, MagicMock())

        assert auth_msg.reply_text.call_count >= 2


# 13. Natural-language window queries
@pytest.mark.asyncio
async def test_natural_language_window_queries():
    # 1. Open windows query
    q1 = check_deterministic_window_query("Orion, what apps are open?")
    assert q1 is not None
    assert q1["tool"] == "list_open_windows"

    # 2. Active application query
    q2 = check_deterministic_window_query("Which application is active?")
    assert q2 is not None
    assert q2["tool"] == "get_active_window"

    # 3. Is VS Code open?
    q3 = check_deterministic_window_query("Is VS Code open?")
    assert q3 is not None
    assert q3["tool"] == "get_application_window_status"
    assert q3["arguments"] == {"app_id": "vscode"}

    # 4. Is WhatsApp minimized?
    q4 = check_deterministic_window_query("Is WhatsApp minimized?")
    assert q4 is not None
    assert q4["tool"] == "get_application_window_status"
    assert q4["arguments"] == {"app_id": "whatsapp"}

    # 5. How many Excel windows are open?
    q5 = check_deterministic_window_query("How many Excel windows are open?")
    assert q5 is not None
    assert q5["tool"] == "get_application_window_status"
    assert q5["arguments"] == {"app_id": "excel"}

    # 6. Unapproved app rejected
    q6 = check_deterministic_window_query("Is Chrome open?")
    assert q6 is not None
    assert q6["handled"] is True
    assert "not in the approved application allowlist" in q6["reply"]


# 14. Existing Step 9 launcher compatibility
def test_existing_step9_launcher_compatibility():
    registry = ToolRegistry()

    # Desktop awareness tools registered
    assert registry.is_registered("list_open_windows")
    assert registry.is_registered("get_active_window")
    assert registry.is_registered("get_application_window_status")

    # Step 9 app launcher still registered and requires confirmation
    assert registry.is_registered("launch_application")
    assert registry.is_write_action("launch_application") is True

    # Desktop awareness tools are read-only (no confirmation required)
    assert registry.is_write_action("list_open_windows") is False
    assert registry.is_write_action("get_active_window") is False
    assert registry.is_write_action("get_application_window_status") is False

    # Exact 20 applications preserved
    apps = get_approved_apps()
    assert len(apps) == 20
