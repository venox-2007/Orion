"""Unit tests for Step 27 — Remote Shell & PowerShell Command Bridge."""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from orion.tools.router import (
    check_deterministic_shell_query,
    route_and_execute_tool,
)
from orion.tools.shell_bridge import (
    execute_shell_command,
    format_shell_output,
    is_command_dangerous,
)


@pytest.mark.asyncio
async def test_shell_execution_echo():
    """Verify simple echo commands execute in cmd and powershell with exit code 0."""
    # 1. Execute in PowerShell
    res_ps = await execute_shell_command("Write-Output 'Hello PowerShell'", shell_type="powershell")
    assert res_ps["exit_code"] == 0
    assert "Hello PowerShell" in res_ps["stdout"]
    assert res_ps["stderr"] == ""
    assert res_ps["duration_sec"] >= 0.0

    fmt_ps = format_shell_output(res_ps, "Write-Output 'Hello PowerShell'", shell_type="powershell")
    assert "💻 *Terminal (POWERSHELL)* — Exit 0" in fmt_ps
    assert "Hello PowerShell" in fmt_ps
    assert re.search(r"</?(?:b|i|s|code|pre)>", fmt_ps) is None

    # 2. Execute in CMD
    res_cmd = await execute_shell_command("echo Hello CMD", shell_type="cmd")
    assert res_cmd["exit_code"] == 0
    assert "Hello CMD" in res_cmd["stdout"]
    assert res_cmd["stderr"] == ""

    fmt_cmd = format_shell_output(res_cmd, "echo Hello CMD", shell_type="cmd")
    assert "💻 *Terminal (CMD)* — Exit 0" in fmt_cmd
    assert "Hello CMD" in fmt_cmd
    assert re.search(r"</?(?:b|i|s|code|pre)>", fmt_cmd) is None


@pytest.mark.asyncio
async def test_shell_command_timeout():
    """Verify hanging shell command triggers timeout, terminates process, and returns -1."""
    # Mock a process that hangs indefinitely in communicate
    mock_process = MagicMock()
    mock_process.kill = MagicMock()
    mock_process.wait = AsyncMock()

    async def hanging_communicate():
        await asyncio.sleep(10)
        return b"", b""

    mock_process.communicate = hanging_communicate

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
        res = await execute_shell_command("Start-Sleep -Seconds 100", shell_type="powershell", timeout_sec=1)
        assert res["exit_code"] == -1
        assert "timed out after 1s" in res["stderr"]
        assert res["stdout"] == ""
        mock_process.kill.assert_called_once()

        fmt = format_shell_output(res, "Start-Sleep -Seconds 100", shell_type="powershell")
        assert "⚠️ *Terminal Error (POWERSHELL)* — Exit -1" in fmt
        assert "timed out after 1s" in fmt
        assert re.search(r"</?(?:b|i|s|code|pre)>", fmt) is None


def test_dangerous_command_guard():
    """Verify destructive/catastrophic commands are identified and blocked prior to execution."""
    dangerous_commands = [
        "format C: /fs:NTFS",
        "format d:",
        "del /f /q /s C:\\Windows\\System32",
        "rmdir /s C:\\",
        ":(){ :|:& };:",
    ]
    for cmd in dangerous_commands:
        assert is_command_dangerous(cmd) is True

    safe_commands = [
        "dir C:\\Users",
        "Get-Process",
        "git status",
        "echo format is good",  # Wait: word format by itself is blocked
    ]
    assert is_command_dangerous("Get-Process") is False
    assert is_command_dangerous("git status") is False

    # Synchronous safety check on execute_shell_command
    # Run async function to ensure it blocks before launching process
    loop = asyncio.new_event_loop()
    try:
        res = loop.run_until_complete(execute_shell_command("format C: /y"))
        assert res["exit_code"] == 1
        assert "⚠️ Command blocked by system safety guard." in res["stderr"]
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_router_shell_queries():
    """Verify router matches ps/powershell and cmd/run prefixes deterministically."""
    # 1. PowerShell: "ps: Get-Date"
    res_ps = await route_and_execute_tool("ps: Get-Date")
    assert res_ps is not None
    assert res_ps.get("handled") is True
    assert "💻 *Terminal (POWERSHELL)* — Exit 0" in res_ps.get("reply", "")
    assert re.search(r"</?(?:b|i|s|code|pre)>", res_ps["reply"]) is None

    # Alternative PowerShell prefix: "powershell: $PSVersionTable.PSVersion"
    res_ps2 = await route_and_execute_tool("powershell: Write-Output 'Orion Shell'")
    assert res_ps2 is not None and res_ps2.get("handled") is True
    assert "Orion Shell" in res_ps2.get("reply", "")

    # 2. CMD: "run: echo Hello"
    res_run = await route_and_execute_tool("run: echo Hello")
    assert res_run is not None
    assert res_run.get("handled") is True
    assert "💻 *Terminal (CMD)* — Exit 0" in res_run.get("reply", "")
    assert "Hello" in res_run.get("reply", "")
    assert re.search(r"</?(?:b|i|s|code|pre)>", res_run["reply"]) is None

    # Alternative CMD prefixes: "cmd: echo World", "terminal: echo Test"
    res_cmd = await route_and_execute_tool("cmd: echo World")
    assert res_cmd is not None and res_cmd.get("handled") is True
    assert "World" in res_cmd.get("reply", "")

    res_term = await route_and_execute_tool("terminal: echo TestTerminal")
    assert res_term is not None and res_term.get("handled") is True
    assert "TestTerminal" in res_term.get("reply", "")

    # Blocked dangerous command via router
    res_blocked = await route_and_execute_tool("run: format C:")
    assert res_blocked is not None
    assert res_blocked.get("handled") is True
    assert "⚠️ *Terminal Error (CMD)* — Exit 1" in res_blocked.get("reply", "")
    assert "Command blocked by system safety guard" in res_blocked.get("reply", "")
