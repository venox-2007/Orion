"""Asynchronous terminal execution engine and shell command bridge for ORION (Step 27 & 30.1)."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

logger = logging.getLogger("orion.tools.shell_bridge")

# Destructive command patterns blocked from remote shell execution
DANGEROUS_PATTERNS = [
    r"\bformat\b",
    r"del\s+[/fFqsS ]+\s+[cC]:\\",
    r"rmdir\s+[/sS/qQ ]+\s+[cC]:\\",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
]

MAX_OUTPUT_CHARS = 3500


def is_command_dangerous(command: str) -> bool:
    """Check if command matches any prohibited destructive pattern."""
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


async def execute_shell_command(
    command: str,
    shell_type: str = "powershell",
    timeout_sec: int = 25,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Execute a shell command asynchronously and return exit code, output, and duration."""
    clean_command = command.strip()
    if not clean_command:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "No command provided.",
            "duration_sec": 0.0,
        }

    # 1. Safety Guard Check
    if is_command_dangerous(clean_command):
        logger.warning("Blocked dangerous shell command: %s", clean_command)
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "⚠️ Command blocked by system safety guard.",
            "duration_sec": 0.0,
        }

    # 2. Determine shell executable and arguments
    norm_shell = shell_type.strip().lower()
    if norm_shell in ("powershell", "ps"):
        cmd_args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", clean_command]
        resolved_shell = "powershell"
    else:
        cmd_args = ["cmd.exe", "/c", clean_command]
        resolved_shell = "cmd"

    # 3. Resolve working directory (defaults to current working directory, fallback to home)
    if cwd:
        resolved_cwd = str(Path(cwd).expanduser().resolve())
    else:
        try:
            cwd_str = os.getcwd()
            if Path(cwd_str).is_dir():
                resolved_cwd = str(Path(cwd_str).resolve())
            else:
                resolved_cwd = str(Path.home())
        except Exception:
            resolved_cwd = str(Path.home())

    start_time = time.perf_counter()

    # 4. Asynchronous Subprocess Execution
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resolved_cwd,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            duration = time.perf_counter() - start_time
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"⏱️ Command timed out after {timeout_sec}s.",
                "duration_sec": round(duration, 2),
            }

        duration = time.perf_counter() - start_time
        clean_stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        clean_stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        # 5. Output Truncation
        if len(clean_stdout) > MAX_OUTPUT_CHARS:
            clean_stdout = clean_stdout[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        if len(clean_stderr) > MAX_OUTPUT_CHARS:
            clean_stderr = clean_stderr[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"

        exit_code = process.returncode if process.returncode is not None else 0
        return {
            "exit_code": exit_code,
            "stdout": clean_stdout,
            "stderr": clean_stderr,
            "duration_sec": round(duration, 2),
        }

    except Exception as e:
        duration = time.perf_counter() - start_time
        logger.error("Failed to execute shell command '%s': %s", clean_command, e)
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Failed to execute command: {e}",
            "duration_sec": round(duration, 2),
        }


def format_shell_output(result: dict[str, Any], command: str, shell_type: str) -> str:
    """Format shell command execution results into pure Telegram Markdown."""
    exit_code = result.get("exit_code", 0)
    duration = result.get("duration_sec", 0.0)
    stdout = result.get("stdout", "").strip()
    stderr = result.get("stderr", "").strip()
    st_upper = "POWERSHELL" if shell_type.strip().lower() in ("powershell", "ps") else "CMD"

    if exit_code == 0:
        if stdout:
            return f"💻 *Terminal ({st_upper})* — Exit 0 ({duration}s)\n```\n{stdout}\n```"
        return f"💻 *Terminal ({st_upper})* — Exit 0 ({duration}s)\n_Command executed with no output._"
    else:
        error_body = stderr or stdout or "Command failed with no output."
        return f"⚠️ *Terminal Error ({st_upper})* — Exit {exit_code} ({duration}s)\n```\n{error_body}\n```"
