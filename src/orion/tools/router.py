"""Natural-language tool routing and presentation layer."""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Any
from pydantic import ValidationError

if TYPE_CHECKING:
    from orion.integrations.ollama import OllamaClient

from orion.tools.app_launcher import (
    _get_short_display_name,
    find_matching_apps,
    get_approved_apps,
    is_app_available,
    is_app_blocked,
    resolve_app_alias,
)
from orion.tools.registry import ToolRegistry, default_tool_registry
from orion.tools.security import prettify_path, resolve_folder_alias, validate_safe_path

TOOL_ROUTER_SYSTEM_PROMPT = (
    "You are ORION's tool selection router. "
    "Analyze the user's message and determine if it requires a sanctioned Windows tool.\n\n"
    "Available read-only tools:\n"
    "- get_battery_status: Get battery percentage and charging status. No arguments.\n"
    "- get_system_resources: Get CPU, RAM, and disk utilization. No arguments.\n"
    '- list_files: List files in an approved directory. Arguments: {"directory": "<alias_or_path>"}\n'
    '- find_files: Search files by filename in an approved directory. Arguments: {"query": "<name>", "directory": "<alias_or_path>"}\n'
    '- get_file_info: Get metadata of an approved file. Arguments: {"path": "<alias_or_path>"}\n'
    "- list_open_windows: List approved applications that currently have open desktop windows. No arguments.\n"
    "- get_active_window: Check which approved application is currently active in the foreground. No arguments.\n"
    '- get_application_window_status: Check window count, state (Normal/Minimized/Maximized), and active status for an approved app. Arguments: {"app_id": "<app_id>"}\n\n'
    "Available controlled file modification tools (require explicit user confirmation):\n"
    '- create_directory: Create a new directory. Arguments: {"path": "<alias_or_path>"}\n'
    '- copy_file: Copy a file to another location. Arguments: {"source": "<alias_or_path>", "destination": "<alias_or_path>"}\n'
    '- move_file: Move a file to another location. Arguments: {"source": "<alias_or_path>", "destination": "<alias_or_path>"}\n'
    '- rename_file: Rename a file in place. Arguments: {"source": "<alias_or_path>", "new_name": "<new_filename_only>"}\n'
    '- create_text_file: Create a new text file. Arguments: {"path": "<alias_or_path>", "content": "<text_content>"}\n\n'
    "Available controlled application launcher tool (requires explicit user confirmation):\n"
    '- launch_application: Launch an approved Windows application. Arguments: {"app_id": "<app_id>"}\n'
    "Approved app_ids: word, excel, powerpoint, notepad, calculator, sticky_notes, file_explorer, vscode, antigravity_ide, chatgpt, claude, edge, comet, whatsapp, teams, zoom, vlc, paint, snipping_tool, winrar.\n\n"
    "Available controlled window management tools (require explicit user confirmation):\n"
    '- focus_application_window: Bring an approved app window to the foreground. Arguments: {"app_id": "<app_id>"} optionally {"window_token": "<token_id>"}.\n'
    '- minimize_application_window: Minimize an approved app window. Arguments: {"app_id": "<app_id>"} optionally {"window_token": "<token_id>"}.\n'
    '- maximize_application_window: Maximize an approved app window. Arguments: {"app_id": "<app_id>"} optionally {"window_token": "<token_id>"}.\n'
    '- restore_application_window: Restore a minimized/maximized approved app window to Normal. Arguments: {"app_id": "<app_id>"} optionally {"window_token": "<token_id>"}.\n\n'
    "Rules for directory and file paths:\n"
    "- For standard folders, use folder aliases: 'Downloads', 'Documents', or 'Desktop' (e.g. {'directory': 'Downloads'}).\n"
    "- Never invent usernames or placeholder paths like 'C:\\\\Users\\\\[username]\\\\' or '%USERPROFILE%'.\n"
    "- For new_name in rename_file, provide ONLY the filename (e.g. 'notes_backup.txt'), never a path.\n\n"
    "Routing Rules:\n"
    '1. If the user asks a general conversation question, return: {"tool": null}\n'
    '2. If the user asks for unsupported actions (e.g., deleting/removing files, overwriting existing files, running shell/scripts/code, launching unapproved apps, browser automation), return: {"tool": null, "unsupported": true}\n'
    '3. If the user asks for a single tool above, return: {"tool": "<tool_name>", "arguments": {...}}\n'
    '4. If the user asks for a multi-step workflow (2 or 3 file operations), return: {"is_workflow": true, "steps": [{"step_id": 1, "tool": "<name>", "arguments": {...}, "description": "..."}, ...]}\n'
    "5. If the user asks to focus/bring to front/bring up/switch to/foreground an approved app window, return focus_application_window.\n"
    "6. If the user asks to minimize an approved app window, return minimize_application_window.\n"
    "7. If the user asks to maximize an approved app window, return maximize_application_window.\n"
    "8. If the user asks to restore an approved app window, return restore_application_window.\n"
    "9. If the user asks a question about window state (is it minimized? is it open?), return get_application_window_status, NOT a write tool.\n"
    "10. Return strict JSON only. Never exceed 3 steps. Do not invent tools."
)


def is_representable_text(content: str) -> bool:
    """Ensure text content contains only readable/printable characters without binary data."""
    if "\x00" in content:
        return False
    unprintable = sum(1 for c in content if ord(c) < 32 and c not in ("\n", "\r", "\t"))
    return unprintable == 0


def format_content_preview(content: str, max_preview_bytes: int = 200) -> str:
    """Format content preview with clear labeling and byte count."""
    encoded = content.encode("utf-8")
    total_bytes = len(encoded)

    if total_bytes <= max_preview_bytes:
        return f"Content ({total_bytes} bytes):\n```\n{content}\n```"

    truncated_text = encoded[:max_preview_bytes].decode("utf-8", errors="ignore")
    return (
        f"Content (preview of first {len(truncated_text.encode('utf-8'))} of {total_bytes} bytes):\n"
        f"```\n{truncated_text}...\n```\n"
        f"*(Note: Truncated preview. Full file will contain {total_bytes} bytes.)*"
    )


def format_action_proposal(tool_name: str, args: dict[str, Any]) -> str:
    """Format an action proposal with complete, understandable details."""
    if tool_name == "create_directory":
        raw = args.get("path", "")
        pretty = prettify_path(raw)
        name = Path(pretty).name or pretty
        parent = str(Path(pretty).parent).replace("\\", "/")
        if parent == ".":
            parent = prettify_path(Path(raw).parent)
        return (
            "⚠️ **Action Confirmation Required**\n\n"
            "ORION proposes creating a new directory:\n"
            f"• **Action:** Create directory\n"
            f"• **Folder Name:** `{name}`\n"
            f"• **Location:** `{parent}`\n\n"
            "*No existing directory will be overwritten. Confirm to proceed.*"
        )
    if tool_name == "create_text_file":
        raw = args.get("path", "")
        pretty = prettify_path(raw)
        name = Path(pretty).name or pretty
        parent = str(Path(pretty).parent).replace("\\", "/")
        if parent == ".":
            parent = prettify_path(Path(raw).parent)
        content = args.get("content", "")
        preview_block = format_content_preview(content)
        return (
            "⚠️ **Action Confirmation Required**\n\n"
            "ORION proposes creating a new text file:\n"
            f"• **Action:** Create text file\n"
            f"• **Filename:** `{name}`\n"
            f"• **Location:** `{parent}`\n"
            f"• **{preview_block}**\n\n"
            "*Overwriting existing files is strictly prohibited. Confirm to proceed.*"
        )
    if tool_name in ("copy_file", "move_file"):
        action_title = "Copy file" if tool_name == "copy_file" else "Move file"
        src_pretty = prettify_path(args.get("source", ""))
        dst_pretty = prettify_path(args.get("destination", ""))
        src_name = Path(src_pretty).name
        src_parent = str(Path(src_pretty).parent).replace("\\", "/")
        return (
            "⚠️ **Action Confirmation Required**\n\n"
            f"ORION proposes {action_title.lower()}ing:\n"
            f"• **Action:** {action_title}\n"
            f"• **Filename:** `{src_name}`\n"
            f"• **Source Location:** `{src_parent}`\n"
            f"• **Destination:** `{dst_pretty}`\n\n"
            "*No files will be overwritten. Confirm to proceed.*"
        )
    if tool_name == "rename_file":
        src_pretty = prettify_path(args.get("source", ""))
        src_name = Path(src_pretty).name
        src_parent = str(Path(src_pretty).parent).replace("\\", "/")
        new_name = args.get("new_name", "")
        return (
            "⚠️ **Action Confirmation Required**\n\n"
            "ORION proposes renaming a file:\n"
            f"• **Action:** Rename file\n"
            f"• **Original File:** `{src_name}`\n"
            f"• **Location:** `{src_parent}`\n"
            f"• **New Filename:** `{new_name}`\n\n"
            "*No files will be overwritten. Confirm to proceed.*"
        )
    if tool_name == "launch_application":
        app_id = args.get("app_id", "")
        app_obj = get_approved_apps().get(app_id)
        display_name = app_obj.display_name if app_obj else app_id
        return (
            "⚠️ **Action Confirmation Required**\n\n"
            f"Launch **{display_name}** on your laptop?\n\n"
            "*Confirm to launch the application.*"
        )
    if tool_name == "composite_app_action":
        return args.get(
            "proposal_text",
            "⚠️ **Action Confirmation Required**\n\nPlease confirm or cancel these application operations.",
        )
    _WINDOW_ACTION_LABELS = {
        "focus_application_window": "Focus window",
        "minimize_application_window": "Minimize window",
        "maximize_application_window": "Maximize window",
        "restore_application_window": "Restore window",
    }
    if tool_name in _WINDOW_ACTION_LABELS:
        action_label = _WINDOW_ACTION_LABELS[tool_name]
        app_id = args.get("app_id", "")
        app_obj = get_approved_apps().get(app_id)
        display_name = app_obj.display_name if app_obj else app_id
        return (
            "⚠️ **Action Confirmation Required**\n\n"
            f"{action_label} **{display_name}**?\n\n"
            f"• **Action:** {action_label}\n"
            f"• **Application:** {display_name}\n\n"
            "*Confirm to proceed.*"
        )
    return "⚠️ **Action Confirmation Required**\nPlease confirm or cancel this operation."


def format_tool_result(tool_name: str, result: dict[str, Any]) -> str:
    """Format tool execution results into concise, human-readable text without raw JSON."""
    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}

    # Check for requires_selection (multi-window disambiguation)
    if result.get("requires_selection") or data.get("requires_selection"):
        sel_source = data if data.get("requires_selection") else result
        disp = sel_source.get("display_name", sel_source.get("app_id", "Application"))
        windows = sel_source.get("windows", [])
        if not windows:
            return f"{disp} has multiple open windows but no selection options could be generated."
        count_str = "two" if len(windows) == 2 else str(len(windows))
        lines = [f"I found {count_str} {disp} windows. Which one?"]
        for idx, w in enumerate(windows, start=1):
            lbl = w.get("label") or f"{disp} — Window {idx}"
            lines.append(f"{idx}. {lbl}")
        return "\n".join(lines)

    if not result.get("success") or (isinstance(data, dict) and data.get("success") is False):
        error_msg = result.get("error") or data.get("error", "Unknown error")
        lower_err = error_msg.lower()
        if "already exists" in lower_err:
            return "Cannot proceed: The destination file or directory already exists. Overwriting is strictly prohibited."
        if "outside permitted" in lower_err or "access denied" in lower_err:
            return "Access denied: That directory or file is outside your permitted folders. You can authorize folders in ORION_ALLOWED_DIRECTORIES."
        if "disabled" in lower_err:
            return "Filesystem access is currently disabled. Configure ORION_ALLOWED_DIRECTORIES in your .env file to enable it."
        if "does not exist" in lower_err or "not found" in lower_err:
            return "The requested file or directory was not found."
        if "placeholder" in lower_err or "unpermitted" in lower_err:
            return "Invalid folder path or unrecognized placeholder."
        if "symlinks" in lower_err:
            return "Cannot proceed: Symlinks and junctions are not permitted."
        if "regular file" in lower_err:
            return "Cannot proceed: Only regular files can be processed."
        if "exceeds maximum" in lower_err:
            return "Cannot proceed: File content exceeds the maximum allowed size limit (1 MB)."
        if (
            "blocked" in lower_err
            or "prevented" in lower_err
            or "not brought to the foreground" in lower_err
            or "failed to verify" in lower_err
            or "no longer available" in lower_err
            or "does not have any open windows" in lower_err
            or "application unavailable" in lower_err
            or "not in the approved" in lower_err
        ):
            return error_msg
        return f"Unable to complete request: {error_msg}"

    data = result.get("data", {})

    if tool_name == "get_battery_status":
        if not data.get("available"):
            return "Battery sensor is not available on this system."
        pct = data.get("percent")
        plugged = data.get("plugged_in")
        status_str = "Plugged in / Charging" if plugged else "On battery"
        secs = data.get("secs_left")
        time_str = f" (~{secs // 60} mins remaining)" if secs else ""
        return f"**Battery:** {pct}%\n**Status:** {status_str}{time_str}"

    if tool_name == "get_system_resources":
        cpu = data.get("cpu_percent")
        ram_used = data.get("ram_used_gb")
        ram_total = data.get("ram_total_gb")
        ram_pct = data.get("ram_percent")
        disk_used = data.get("disk_used_gb")
        disk_free = data.get("disk_free_gb")
        disk_total = data.get("disk_total_gb")
        disk_pct = data.get("disk_percent")

        lines = [
            "**System Resources:**",
            f"• **CPU:** {cpu}%",
            f"• **RAM:** {ram_used}/{ram_total} GB ({ram_pct}%)",
        ]
        if disk_free is not None:
            lines.append(f"• **Disk:** {disk_free} GB free / {disk_total} GB ({disk_pct}% used)")
        else:
            lines.append(f"• **Disk:** {disk_used} GB / {disk_total} GB ({disk_pct}%)")
        return "\n".join(lines)

    if tool_name == "get_system_overview":
        from orion.tools.system import format_system_overview
        return format_system_overview(data)

    if tool_name == "get_gpu_status":
        from orion.tools.system import format_gpu_status_natural
        return format_gpu_status_natural(data)

    if tool_name == "list_files":
        dirname = data.get("directory", "")
        items = data.get("items", [])
        if not items:
            return f"No files found in `{dirname}`."
        lines = [f"**Files in** `{dirname}` ({data.get('count')} items):"]
        for item in items[:25]:
            kind = "[Dir] " if item.get("is_dir") else ""
            lines.append(f"• {kind}{item.get('name')}")
        if data.get("truncated"):
            lines.append(f"*(Showing first 25 of {data.get('total_in_folder')} items)*")
        return "\n".join(lines)

    if tool_name == "find_files":
        query = data.get("query", "")
        dirname = data.get("directory", "")
        matches = data.get("matches", [])
        if not matches:
            return f"No files matching `{query}` found in `{dirname}`."
        lines = [f"**Found {len(matches)} files matching** `{query}` in `{dirname}`:"]
        for m in matches[:25]:
            lines.append(f"• {m.get('relative_path')}")
        if data.get("truncated"):
            lines.append("*(Results truncated at limit)*")
        return "\n".join(lines)

    if tool_name == "get_file_info":
        name = data.get("name", "")
        is_dir = data.get("is_dir")
        size = data.get("size_bytes")
        size_str = f"{round(size / 1024, 1)} KB" if size is not None else "N/A"
        mod = data.get("modified_at", "N/A")
        return (
            f"**File Info for** `{name}`:\n"
            f"• **Type:** {'Directory' if is_dir else 'File'}\n"
            f"• **Size:** {size_str}\n"
            f"• **Modified:** {mod}"
        )

    if tool_name == "create_directory":
        return f"Successfully created directory `{data.get('name')}` in `{data.get('parent')}`."

    if tool_name == "create_text_file":
        return f"Successfully created text file `{data.get('name')}` ({data.get('size_bytes')} bytes)."

    if tool_name == "copy_file":
        return f"Successfully copied `{data.get('name')}` to `{data.get('destination')}`."

    if tool_name == "move_file":
        return f"Successfully moved `{data.get('name')}` to `{data.get('destination')}`."

    if tool_name == "rename_file":
        return f"Successfully renamed `{data.get('old_name')}` to `{data.get('new_name')}`."

    if tool_name == "launch_application":
        disp = data.get("display_name", "Application")
        return f"🚀 Launch requested for **{disp}**."

    if tool_name == "composite_app_action":
        return data.get("message", "Application operations completed.")

    if tool_name == "list_open_windows":
        windows = data.get("open_windows", [])
        if not windows:
            return "No approved applications currently have open windows."
        lines = ["**ORION — Open Application Windows**\n"]
        for w in windows:
            counts: dict[str, int] = {}
            for s in w.get("states", []):
                counts[s] = counts.get(s, 0) + 1
            state_str = ", ".join(f"{cnt} {st}" for st, cnt in counts.items())
            active_tag = " [Active]" if w.get("is_active") else ""
            lines.append(f"• **{w.get('display_name')}:** {w.get('window_count')} window(s) ({state_str}){active_tag}")
        lines.append("\n*Only approved applications are monitored.*")
        return "\n".join(lines)

    if tool_name == "get_active_window":
        if data.get("has_active_approved_window"):
            return f"**Active Application:** **{data.get('display_name')}** ({data.get('state')})"
        return data.get("message", "The active application is not available to ORION.")

    if tool_name == "get_application_window_status":
        disp = data.get("display_name", "Application")
        if data.get("has_open_window"):
            cnt = data.get("window_count", 1)
            states = data.get("states", [])
            state_counts: dict[str, int] = {}
            for s in states:
                state_counts[s] = state_counts.get(s, 0) + 1
            state_str = ", ".join(f"{c} {st}" for st, c in state_counts.items()) if state_counts else "Normal"
            active_tag = " [Active]" if data.get("is_active") else ""
            return f"Yes, **{disp}** is currently running with {cnt} open window(s) ({state_str}){active_tag}."
        return f"No, **{disp}** is not currently running."

    _WINDOW_MGMT_TOOLS = {
        "focus_application_window",
        "minimize_application_window",
        "maximize_application_window",
        "restore_application_window",
    }
    if tool_name in _WINDOW_MGMT_TOOLS:
        # Success result
        return data.get("message", "Window operation completed.")

    return "Action completed successfully."


def resolve_app_or_clarification(target: str, action_desc: str = "open") -> tuple[str | None, dict[str, Any] | None]:
    """Resolve target to an approved app_id, or return a clarification response if ambiguous.

    Returns:
        (app_id, None) if exactly 1 match found.
        (None, {"handled": True, "reply": ...}) if multiple approved apps are plausible.
        (None, None) if 0 matches found.
    """
    matches = find_matching_apps(target)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        approved = get_approved_apps()
        names = [approved[aid].display_name for aid in matches if aid in approved]
        return None, {
            "handled": True,
            "reply": f"Multiple approved applications matched '{target}': {', '.join(names)}. Which one would you like to {action_desc}?",
        }
    return None, None


PRONOUN_APP_TARGETS = {
    "it",
    "that",
    "this",
    "the app",
    "that app",
    "the window",
    "that window",
    "app",
    "window",
    "",
}


def resolve_target_app_from_context(
    candidate: str,
    context: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a pronoun reference (e.g. 'it', 'that') to the last targeted approved app.

    Returns:
        (app_id, None) if resolved to an approved application.
        (None, "expired" | "ambiguous" | "no_context") if resolution failed.
    """
    if candidate.strip().lower() not in PRONOUN_APP_TARGETS:
        return None, None

    if not context:
        return None, "no_context"

    if "recent_app_id" in context:
        aid = context["recent_app_id"]
        from orion.tools.app_launcher import APPROVED_APPLICATIONS

        if aid in APPROVED_APPLICATIONS:
            return aid, None
        return None, "no_context"

    mem = context.get("memory")
    uid = context.get("user_id") if context.get("user_id") is not None else context.get("owner_id")
    if mem and uid is not None and hasattr(mem, "get_last_targeted_app"):
        return mem.get_last_targeted_app(uid)

    return None, "no_context"


def check_deterministic_window_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check if text is asking about desktop windows, active app, or window management."""
    clean = clean_text.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean).strip()

    # 1. Open windows query
    open_win_patterns = (
        "what apps are open",
        "what applications are open",
        "which apps are open",
        "which applications are open",
        "what windows are open",
        "show open windows",
        "list open windows",
        "open windows",
        "what is open",
        "what apps are running",
        "which apps are running",
    )
    if any(p in clean_no_punct for p in open_win_patterns):
        return {"tool": "list_open_windows", "arguments": {}}

    # 2. Active window query
    active_win_patterns = (
        "which application is active",
        "what application is active",
        "which app is active",
        "what app is active",
        "who is active",
        "what is the active window",
        "what is active",
        "current active application",
        "current active window",
        "active window",
        "active app",
    )
    if any(p in clean_no_punct for p in active_win_patterns):
        return {"tool": "get_active_window", "arguments": {}}

    # 3. Window management commands (Step 11) — imperative actions
    # Patterns for focus
    focus_match = re.search(
        r"(?:focus|bring\s+up|bring\s+to\s+(?:the\s+)?front|switch\s+to|foreground)\s+([a-zA-Z0-9_\s]+?)(?:\s+window)?[\s.!?]*$",
        clean,
    )
    if not focus_match:
        # Alternative: "bring <app> to the front/foreground"
        focus_match = re.search(
            r"(?:bring|put)\s+([a-zA-Z0-9_\s]+?)\s+(?:to\s+(?:the\s+)?(?:front|foreground)|up)[\s.!?]*$",
            clean,
        )
    if focus_match:
        candidate = focus_match.group(1).strip()
        candidate = re.sub(r"^(?:the\s+|my\s+)", "", candidate).strip()
        candidate = re.sub(r"\s+(?:app|application)$", "", candidate).strip()
        if candidate.lower() in PRONOUN_APP_TARGETS:
            resolved_app, reason = resolve_target_app_from_context(candidate, context)
            if resolved_app:
                candidate = resolved_app
            else:
                return {
                    "handled": True,
                    "reply": "Which application would you like to bring to the front?",
                }
        blocked, reason = is_app_blocked(candidate)
        if blocked:
            return {"handled": True, "reply": f"Application '{candidate}' is not in the approved application allowlist. {reason}"}
        app_id, clarif = resolve_app_or_clarification(candidate, "bring to the front")
        if clarif:
            return clarif
        if app_id:
            return {"is_window_action": True, "tool": "focus_application_window", "arguments": {"app_id": app_id}}

    # Patterns for minimize
    minimize_match = re.search(
        r"(?:minimize|minimise)\s+([a-zA-Z0-9_\s]+?)(?:\s+window)?[\s.!?]*$",
        clean,
    )
    if minimize_match:
        candidate = minimize_match.group(1).strip()
        candidate = re.sub(r"^(?:the\s+|my\s+)", "", candidate).strip()
        candidate = re.sub(r"\s+(?:app|application)$", "", candidate).strip()
        if candidate.lower() in PRONOUN_APP_TARGETS:
            resolved_app, reason = resolve_target_app_from_context(candidate, context)
            if resolved_app:
                candidate = resolved_app
            else:
                return {
                    "handled": True,
                    "reply": "Which application would you like to minimize?",
                }
        blocked, reason = is_app_blocked(candidate)
        if blocked:
            return {"handled": True, "reply": f"Application '{candidate}' is not in the approved application allowlist. {reason}"}
        app_id, clarif = resolve_app_or_clarification(candidate, "minimize")
        if clarif:
            return clarif
        if app_id:
            return {"is_window_action": True, "tool": "minimize_application_window", "arguments": {"app_id": app_id}}

    # Patterns for maximize
    maximize_match = re.search(
        r"(?:maximize|maximise)\s+([a-zA-Z0-9_\s]+?)(?:\s+window)?[\s.!?]*$",
        clean,
    )
    if maximize_match:
        candidate = maximize_match.group(1).strip()
        candidate = re.sub(r"^(?:the\s+|my\s+)", "", candidate).strip()
        candidate = re.sub(r"\s+(?:app|application)$", "", candidate).strip()
        if candidate.lower() in PRONOUN_APP_TARGETS:
            resolved_app, reason = resolve_target_app_from_context(candidate, context)
            if resolved_app:
                candidate = resolved_app
            else:
                return {
                    "handled": True,
                    "reply": "Which application would you like to maximize?",
                }
        blocked, reason = is_app_blocked(candidate)
        if blocked:
            return {"handled": True, "reply": f"Application '{candidate}' is not in the approved application allowlist. {reason}"}
        app_id, clarif = resolve_app_or_clarification(candidate, "maximize")
        if clarif:
            return clarif
        if app_id:
            return {"is_window_action": True, "tool": "maximize_application_window", "arguments": {"app_id": app_id}}

    # Patterns for restore
    restore_match = re.search(
        r"restore\s+([a-zA-Z0-9_\s]+?)(?:\s+window)?[\s.!?]*$",
        clean,
    )
    if restore_match:
        candidate = restore_match.group(1).strip()
        candidate = re.sub(r"^(?:the\s+|my\s+)", "", candidate).strip()
        candidate = re.sub(r"\s+(?:app|application)$", "", candidate).strip()
        if candidate.lower() in PRONOUN_APP_TARGETS:
            resolved_app, reason = resolve_target_app_from_context(candidate, context)
            if resolved_app:
                candidate = resolved_app
            else:
                return {
                    "handled": True,
                    "reply": "Which application would you like to restore?",
                }
        blocked, reason = is_app_blocked(candidate)
        if blocked:
            return {"handled": True, "reply": f"Application '{candidate}' is not in the approved application allowlist. {reason}"}
        app_id, clarif = resolve_app_or_clarification(candidate, "restore")
        if clarif:
            return clarif
        if app_id:
            return {"is_window_action": True, "tool": "restore_application_window", "arguments": {"app_id": app_id}}

    # 4. Status for specific application (read-only):
    # "is vs code open", "is whatsapp minimized", "how many excel windows are open", "is word open"
    match_status = re.search(
        r"(?:is|how\s+many)\s+(?:window(?:s)?\s+(?:of|for)\s+)?([a-zA-Z0-9_\s]+?)\s+(?:windows?\s+)?(?:are\s+|is\s+)?(?:open|running|minimized|maximized|active)",
        clean,
    )
    if match_status:
        candidate_app = match_status.group(1).strip()
        candidate_app = re.sub(r"^(?:the\s+|my\s+)", "", candidate_app)
        candidate_app = re.sub(r"\s+(?:window|windows|app|application)$", "", candidate_app).strip()
        if candidate_app.lower() in PRONOUN_APP_TARGETS:
            resolved_app, reason = resolve_target_app_from_context(candidate_app, context)
            if resolved_app:
                candidate_app = resolved_app
            else:
                return {
                    "handled": True,
                    "reply": "Which application window would you like to check?",
                }
        blocked, reason = is_app_blocked(candidate_app)
        if blocked:
            return {
                "handled": True,
                "reply": f"Application '{candidate_app}' is not in the approved application allowlist. {reason}",
            }

        app_id, clarif = resolve_app_or_clarification(candidate_app, "check")
        if clarif:
            return clarif
        if app_id:
            return {"tool": "get_application_window_status", "arguments": {"app_id": app_id}}
        return {
            "handled": True,
            "reply": f"Application '{candidate_app}' is not in the approved application allowlist. ORION can only monitor approved applications.",
        }

    return None


CONCEPTUAL_PREFIXES: tuple[str, ...] = (
    "what is a",
    "what is an",
    "what are",
    "what does",
    "how does a",
    "how does the",
    "how does",
    "how do",
    "why do",
    "why does",
    "explain",
    "define",
    "difference between",
)

CONVERSATIONAL_GREETINGS: tuple[str, ...] = (
    "hello",
    "hi",
    "hey",
    "thanks",
    "thank you",
    "good morning",
    "good afternoon",
    "good evening",
    "good night",
    "how are you",
    "how are you doing",
    "whats up",
    "what's up",
    "bye",
    "goodbye",
)


def should_bypass_llm_tool_router(clean_text: str) -> bool:
    """Fast check to determine if text is conversational or conceptual and should bypass tool LLM routing."""
    clean = clean_text.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean).strip()

    if any(clean_no_punct.startswith(p) for p in CONCEPTUAL_PREFIXES):
        return True

    if clean_no_punct in CONVERSATIONAL_GREETINGS:
        return True

    words = clean_no_punct.split()
    if any(clean_no_punct.startswith(g + " ") for g in ("hello", "hi", "hey", "good morning", "good evening", "good afternoon")):
        action_words = {"open", "launch", "start", "run", "minimize", "maximize", "restore", "close", "remind", "delete", "create", "list", "find", "show"}
        if not any(w in action_words for w in words):
            return True

    return False


def check_deterministic_system_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check and answer questions about system resources, battery, GPU, and status (Step 18)."""
    clean = clean_text.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean).strip()

    # Conceptual / educational question exclusion: "what is a cpu", "how does a gpu work"
    if any(clean_no_punct.startswith(p) for p in CONCEPTUAL_PREFIXES):
        return None

    category = None

    # 1. System overview: "how's my laptop doing", "system status", etc.
    overview_patterns = (
        "hows my laptop doing",
        "how is my laptop doing",
        "hows my pc doing",
        "how is my pc doing",
        "hows my computer doing",
        "how is my computer doing",
        "hows my computer",
        "how is my computer",
        "hows my laptop",
        "how is my laptop",
        "hows my pc",
        "how is my pc",
        "how is the laptop running",
        "how is my laptop running",
        "how is my pc running",
        "how is my computer running",
        "system status",
        "laptop status",
        "pc status",
        "computer status",
        "whats my system status",
        "what is my system status",
        "how is my system doing",
        "how is my system",
        "hows my system",
    )
    if any(clean_no_punct == p or clean_no_punct.startswith(p) for p in overview_patterns):
        category = "overview"

    # 2. CPU: "what's my cpu usage", "what is my cpu load", etc.
    elif (
        re.search(
            r"\b(?:cpu\s+(?:usage|load|status)|(?:usage|load)\s+of\s+(?:the\s+|my\s+)?cpu|(?:my\s+)?cpu\s+doing)\b",
            clean_no_punct,
        )
        or re.search(r"\bwhats\s+(?:my\s+|the\s+)?cpu\b", clean_no_punct)
    ):
        category = "cpu"

    # 3. RAM / Memory: "how much ram am i using", "what's my ram usage", etc.
    elif (
        re.search(
            r"\b(?:ram\s+(?:usage|status)|memory\s+usage|how\s+much\s+(?:ram|memory)|ram\s+(?:left|free))\b",
            clean_no_punct,
        )
        or re.search(r"\bwhats\s+(?:my\s+|the\s+)?ram\b", clean_no_punct)
    ):
        category = "ram"

    # 4. Storage / Disk: "how much storage is left", "storage free", etc.
    elif (
        re.search(
            r"\b(?:storage|disk)\s+(?:free|left|usage|status|space)\b",
            clean_no_punct,
        )
        or re.search(r"\bhow\s+much\s+(?:storage|disk\s+space)\b", clean_no_punct)
    ):
        category = "storage"

    # 5. Charging status: "is my laptop charging", "is my pc plugged in", "is it charging"
    elif re.search(
        r"\b(?:is\s+(?:my\s+)?(?:laptop|pc|computer|it)\s+(?:charging|plugged\s+in))\b",
        clean_no_punct,
    ):
        category = "charging"

    # 6. Battery percentage / status: "what's my battery percentage", "how much battery is left"
    elif re.search(
        r"\b(?:battery\s+(?:percentage|status|level)|how\s+much\s+battery|whats\s+(?:my\s+)?battery)\b",
        clean_no_punct,
    ):
        category = "battery"

    # 7. GPU: "what's my gpu usage", "gpu status", "what's my gpu temperature"
    elif (
        re.search(
            r"\b(?:gpu\s+(?:usage|load|status|temp|temperature)|(?:my\s+)?gpu\s+doing)\b",
            clean_no_punct,
        )
        or re.search(r"\bwhats\s+(?:my\s+|the\s+)?gpu\b", clean_no_punct)
    ):
        category = "gpu"

    if not category:
        return None

    # Owner authorization check
    ctx = context or {}
    user_id = ctx.get("user_id") or ctx.get("owner_id")
    owner_id = ctx.get("owner_id")
    if owner_id and user_id != owner_id:
        return {"handled": True, "reply": "Access restricted: unauthorized user or chat."}

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

    if category == "overview":
        data = get_system_overview()
        reply = format_system_overview(data)
    elif category == "cpu":
        data = get_system_resources()
        reply = format_cpu_status(data)
    elif category == "ram":
        data = get_system_resources()
        reply = format_ram_status(data)
    elif category == "storage":
        data = get_system_resources()
        reply = format_storage_status(data)
    elif category == "charging":
        battery = get_battery_status()
        reply = format_charging_status(battery)
    elif category == "battery":
        battery = get_battery_status()
        reply = format_battery_status_natural(battery)
    elif category == "gpu":
        gpu = get_gpu_status()
        reply = format_gpu_status_natural(gpu)
    else:
        return None

    return {"handled": True, "reply": reply}


def check_deterministic_task_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check and execute task management queries (Step 19)."""
    clean = clean_text.strip()
    clean_lower = clean.lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean_lower).strip()

    # 'remind me to' must NOT be captured here; let reminder router handle reminders
    if clean_no_punct.startswith("remind") or re.search(r"\bremind\s+me\b", clean_no_punct):
        return None

    from orion.core import tasks

    mem = context.get("memory") if context else None

    # B. LIST / VIEW TASKS
    list_match = re.match(
        r"(?i)^(?:show|list|view|what\s+are|get)\s+(?:my\s+)?(?:all\s+)?(?:tasks|todos|to-?do\s+list)(?:\s+all)?\??$",
        clean,
    ) or re.match(
        r"^(?:show|list|view|what are|get)\s+(?:my\s+)?(?:all\s+)?(?:tasks|todos|todo list|to do list)(?:\s+all)?$",
        clean_no_punct,
    ) or clean_no_punct in ("tasks", "todos", "all tasks", "show tasks", "my tasks", "list tasks")

    if list_match:
        all_flag = bool(re.search(r"\ball\b", clean_no_punct))
        task_list = tasks.list_tasks(include_completed=all_flag, memory=mem)
        reply = tasks.format_task_list(task_list, show_completed=all_flag)
        return {"handled": True, "reply": reply}

    # C. COMPLETE TASK
    complete_match = re.match(
        r"(?i)^(?:complete|finish|done|mark)\s+(?:task|todo)\s+(\d+)(?:\s+as\s+done)?[\.!]?$",
        clean,
    ) or re.match(
        r"(?i)^mark\s+(?:task\s+)?(\d+)\s+(?:as\s+)?(?:done|complete|completed)[\.!]?$",
        clean,
    ) or re.match(
        r"^(?:complete|finish|done|mark)\s+(?:task|todo)\s+(\d+)(?:\s+as\s+done)?$",
        clean_no_punct,
    ) or re.match(
        r"^mark\s+(?:task\s+)?(\d+)\s+(?:as\s+)?(?:done|complete|completed)$",
        clean_no_punct,
    )

    if complete_match:
        task_id = int(complete_match.group(1))
        updated = tasks.complete_task(task_id, memory=mem)
        if updated:
            return {
                "handled": True,
                "reply": f"🎉 Marked task #{task_id} (*{updated['title']}*) as completed!",
            }
        return {
            "handled": True,
            "reply": f"Task #{task_id} was not found.",
        }

    # D. DELETE / REMOVE TASK
    delete_match = re.match(
        r"(?i)^(?:delete|remove|cancel)\s+(?:task|todo)\s+(\d+)[\.!]?$",
        clean,
    ) or re.match(
        r"^(?:delete|remove|cancel)\s+(?:task|todo)\s+(\d+)$",
        clean_no_punct,
    )

    if delete_match:
        task_id = int(delete_match.group(1))
        deleted = tasks.delete_task(task_id, memory=mem)
        if deleted:
            return {
                "handled": True,
                "reply": f"🗑️ Deleted task #{task_id}.",
            }
        return {
            "handled": True,
            "reply": f"Task #{task_id} was not found.",
        }

    # A. ADD TASK
    add_match = re.match(
        r"(?i)^(?:add|create|new)\s+(?:a\s+)?(?:task|todo)(?:\s+(high|medium|low|normal)\s+priority)?\s*[:\-]?\s*(.+)$",
        clean,
    )

    if not add_match:
        add_match = re.match(
            r"(?i)^(?:add|create|new)\s+(?:a\s+)?(?:task|todo)\s+to\s+(.+)$",
            clean,
        )

    if add_match:
        prio_group = add_match.group(1) if add_match.lastindex and add_match.lastindex >= 2 else None
        raw_title = add_match.group(add_match.lastindex).strip() if add_match.lastindex else ""

        if raw_title.lower().startswith("to "):
            raw_title = raw_title[3:].strip()

        prio_prefix = re.match(r"(?i)^(high|medium|low|normal)\s+priority\s*[:\-]?\s*(.+)$", raw_title)
        if prio_prefix:
            prio_group = prio_prefix.group(1)
            raw_title = prio_prefix.group(2).strip()

        prio_suffix = re.search(r"(?i)\s+(?:with\s+)?(high|medium|low|normal)\s+priority$", raw_title)
        if prio_suffix:
            prio_group = prio_suffix.group(1)
            raw_title = raw_title[:prio_suffix.start()].strip()

        raw_title = re.sub(r"^[\s:\-]+", "", raw_title).strip()
        if not raw_title:
            return None

        priority = (prio_group or "normal").lower()
        if priority == "medium":
            priority = "normal"

        try:
            task = tasks.add_task(title=raw_title, priority=priority, memory=mem)
            return {
                "handled": True,
                "reply": f"✅ Added task: *{task['title']}* (Priority: {task['priority']}, ID: {task['id']})",
            }
        except ValueError as err:
            return {
                "handled": True,
                "reply": f"Could not add task: {err}",
            }

    return None


def check_deterministic_screen_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check if query is requesting a desktop screenshot capture (Step 20)."""
    clean = clean_text.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean).strip()

    # Avoid capturing queries that are about tasks, reminders, or file actions
    if clean.startswith(("add", "create", "new", "delete", "remove", "mark", "complete", "remind")):
        return None

    screen_patterns = (
        r"^(?:take\s+(?:a\s+)?)?screenshot$",
        r"^(?:take\s+(?:a\s+)?|capture\s+(?:a\s+)?)?(?:screen|desktop)\s*(?:shot|snapshot|capture)?$",
        r"^(?:show|what(?:s|'s|\s+is))\s+(?:on\s+my\s+|my\s+|the\s+|on\s+the\s+|on\s+)?(?:screen|desktop)$",
        r"^see\s+(?:my\s+)?(?:screen|desktop)$",
        r"^(?:send|give\s+me)\s+(?:a\s+)?(?:screenshot|screen\s+capture)$",
    )

    is_match = False
    for pat in screen_patterns:
        if re.match(pat, clean) or re.match(pat, clean_no_punct):
            is_match = True
            break

    if not is_match:
        return None

    from orion.tools.screen_capture import ScreenCaptureError, take_desktop_screenshot
    from datetime import datetime

    try:
        image_bytes = take_desktop_screenshot()
        now_str = datetime.now().strftime("%I:%M %p").lstrip("0")
        caption = f"📸 Desktop snapshot • {now_str}"
        if context is not None and isinstance(context, dict):
            context["last_action"] = {
                "type": "screenshot",
                "timestamp": time.time(),
            }
        return {
            "handled": True,
            "type": "photo",
            "photo_bytes": image_bytes,
            "caption": caption,
        }
    except ScreenCaptureError as e:
        logger.warning("Screen capture failed in router: %s", e)
        return {
            "handled": True,
            "reply": "⚠️ Unable to capture screenshot. The screen might be locked or sleeping.",
        }


def execute_workstation_action(
    action: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a workstation session or media action and update context (Step 21)."""
    from orion.tools import workstation

    if action == "lock":
        try:
            workstation.lock_workstation()
            reply = "🔒 Laptop locked successfully."
        except Exception as e:
            logger.warning("LockWorkStation failed: %s", e)
            reply = "⚠️ Failed to lock workstation."
    elif action == "toggle_play_pause":
        workstation.toggle_play_pause()
        reply = "⏯️ Toggled media playback."
    elif action == "next_track":
        workstation.next_track()
        reply = "⏭️ Skipped to next track."
    elif action == "previous_track":
        workstation.previous_track()
        reply = "⏮️ Returned to previous track."
    elif action == "mute":
        workstation.toggle_mute()
        reply = "🔇 Toggled mute."
    elif action == "volume_up":
        workstation.adjust_volume("up", steps=5)
        reply = "🔊 Volume increased."
    elif action == "volume_down":
        workstation.adjust_volume("down", steps=5)
        reply = "🔉 Volume decreased."
    else:
        raise ValueError(f"Unknown workstation action: {action}")

    if context is not None and isinstance(context, dict):
        context["last_action"] = {
            "type": "workstation",
            "action": action,
            "timestamp": time.time(),
        }

    return {
        "handled": True,
        "reply": reply,
    }


def check_deterministic_workstation_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check if query is a workstation lock or media/volume control command (Step 21)."""
    clean = clean_text.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean).strip()

    # Avoid capturing queries that are tasks, reminders, or file actions
    if clean.startswith(("add ", "create ", "remind ", "task ", "delete ", "remove ")):
        return None

    # A. Lock workstation
    lock_patterns = (
        r"^(?:lock(?:\s+my)?\s+(?:laptop|pc|computer|screen|workstation)|lock\s+it|lock)$",
    )
    for pat in lock_patterns:
        if re.match(pat, clean) or re.match(pat, clean_no_punct):
            return execute_workstation_action("lock", context=context)

    # B. Media controls
    play_pause_pattern = r"^(?:play|pause|resume|toggle\s+playback|play\s+music|pause\s+music)$"
    if re.match(play_pause_pattern, clean) or re.match(play_pause_pattern, clean_no_punct):
        return execute_workstation_action("toggle_play_pause", context=context)

    next_pattern = r"^(?:next(?:\s+track|\s+song)?|skip(?:\s+track|\s+song)?)$"
    if re.match(next_pattern, clean) or re.match(next_pattern, clean_no_punct):
        return execute_workstation_action("next_track", context=context)

    prev_pattern = r"^(?:previous|prev)(?:\s+track|\s+song)?$"
    if re.match(prev_pattern, clean) or re.match(prev_pattern, clean_no_punct):
        return execute_workstation_action("previous_track", context=context)

    # C. Volume controls
    vol_exact_match = re.match(r"^(?:set\s+)?volume(?:\s+to)?\s+(\d{1,3})%?$", clean) or re.match(r"^(?:set\s+)?volume(?:\s+to)?\s+(\d{1,3})%?$", clean_no_punct)
    if vol_exact_match:
        lvl = int(vol_exact_match.group(1))
        lvl = max(0, min(100, lvl))
        from orion.tools import workstation

        workstation.set_volume_percentage(lvl)
        if context is not None and isinstance(context, dict):
            context["last_action"] = {
                "type": "workstation",
                "action": "set_volume",
                "level": lvl,
                "timestamp": time.time(),
            }
        return {
            "handled": True,
            "reply": f"🔊 Volume set to {lvl}%.",
        }

    mute_pattern = r"^(?:mute|unmute|toggle\s+mute)(?:\s+(?:volume|audio|sound|laptop))?$"
    if re.match(mute_pattern, clean) or re.match(mute_pattern, clean_no_punct):
        return execute_workstation_action("mute", context=context)

    vol_up_pattern = r"^(?:volume\s+up|increase\s+volume|turn\s+(?:it\s+)?up)$"
    if re.match(vol_up_pattern, clean) or re.match(vol_up_pattern, clean_no_punct):
        return execute_workstation_action("volume_up", context=context)

    vol_down_pattern = r"^(?:volume\s+down|decrease\s+volume|turn\s+(?:it\s+)?down)$"
    if re.match(vol_down_pattern, clean) or re.match(vol_down_pattern, clean_no_punct):
        return execute_workstation_action("volume_down", context=context)

    return None


def check_deterministic_repeat_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check if query is requesting to repeat the previous action (Step 21)."""
    clean = clean_text.strip().lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean).strip()

    repeat_pattern = r"^(?:again|one\s+more|another(?:\s+one)?|repeat|do\s+it\s+again|retake)$"
    if not (re.match(repeat_pattern, clean) or re.match(repeat_pattern, clean_no_punct)):
        return None

    if not context or not isinstance(context, dict):
        return None

    last_action = context.get("last_action")
    if not isinstance(last_action, dict):
        return None

    ts = last_action.get("timestamp", 0)
    # Context expiration window: 5 minutes (300 seconds)
    if time.time() - ts > 300:
        return None

    act_type = last_action.get("type")
    if act_type == "screenshot":
        return check_deterministic_screen_query("screenshot", context=context)
    elif act_type == "file_fetch":
        fp = last_action.get("file_path")
        if fp and Path(fp).is_file():
            p = Path(fp)
            return {
                "handled": True,
                "type": "document",
                "file_path": str(p),
                "caption": f"📄 {p.name}",
            }
    elif act_type == "workstation":
        act = last_action.get("action")
        if act == "set_volume":
            lvl = last_action.get("level", 50)
            from orion.tools import workstation

            workstation.set_volume_percentage(lvl)
            last_action["timestamp"] = time.time()
            return {"handled": True, "reply": f"🔊 Volume set to {lvl}%."}
        elif act:
            return execute_workstation_action(act, context=context)

    return None


def check_deterministic_file_fetch_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check if query is requesting to fetch/send a file from laptop to Telegram (Step 22)."""
    from orion.tools import file_bridge

    clean = clean_text.strip().rstrip(".?! ")
    clean_lower = clean.lower()

    # Avoid capturing queries that are tasks, reminders, workstation actions, or clipboard
    if clean_lower.startswith(("add ", "create ", "remind ", "task ", "delete ", "remove ", "lock ", "volume ", "turn ", "play", "pause", "skip", "mute", "unmute")):
        return None
    if "clipboard" in clean_lower:
        return None

    # 1. Latest file pattern
    latest_pat = r"^(?:send|fetch|get)(?:\s+me)?\s+(?:the\s+)?latest(?:\s+([a-zA-Z0-9]+))?\s*(?:file|document)?(?:\s+from\s+(downloads|documents|desktop))?$"
    m_latest = re.match(latest_pat, clean, re.IGNORECASE)
    if m_latest:
        raw_ext = m_latest.group(1)
        ext = None if not raw_ext or raw_ext.lower() in ("file", "document") else raw_ext
        dir_alias = m_latest.group(2) or "downloads"

        file_path = file_bridge.get_latest_file(directory_alias=dir_alias, extension=ext)
        if not file_path or not file_bridge.is_path_allowed(file_path):
            return {
                "handled": True,
                "reply": "⚠️ File not found or access is restricted.",
            }

        st_size = file_path.stat().st_size
        if st_size > file_bridge.MAX_FILE_BYTES:
            file_size_mb = st_size / (1024 * 1024)
            return {
                "handled": True,
                "reply": f"⚠️ *{file_path.name}* is too large to send via Telegram ({file_size_mb:.1f} MB, limit is 50 MB).",
            }

        if context is not None and isinstance(context, dict):
            context["last_action"] = {
                "type": "file_fetch",
                "file_path": str(file_path),
                "timestamp": time.time(),
            }

        return {
            "handled": True,
            "type": "document",
            "file_path": str(file_path),
            "caption": f"📄 {file_path.name}",
        }

    # 2. Specific file pattern (with or without extension)
    specific_pat = r"^(?:send|fetch|get)(?:\s+me)?\s+(?:the\s+)?([a-zA-Z0-9_\-\. ]+?)(?:\s+from\s+(downloads|documents|desktop))?$"
    m_specific = re.match(specific_pat, clean, re.IGNORECASE)
    if m_specific:
        filename = m_specific.group(1).strip()
        dir_alias = m_specific.group(2)

        # Disallow non-file terms
        if filename.lower() in ("screenshot", "screen capture", "desktop", "it", "that", "clipboard"):
            return None

        try:
            file_path = file_bridge.find_approved_file(filename, directory_alias=dir_alias)
        except file_bridge.FileTooLargeError as err:
            mb = err.size_bytes / (1024 * 1024)
            return {
                "handled": True,
                "reply": f"⚠️ *{err.path.name}* is too large to send via Telegram ({mb:.1f} MB, limit is 50 MB).",
            }

        if not file_path or not file_bridge.is_path_allowed(file_path):
            return {
                "handled": True,
                "reply": "⚠️ File not found or access is restricted.",
            }

        st_size = file_path.stat().st_size
        if st_size > file_bridge.MAX_FILE_BYTES:
            file_size_mb = st_size / (1024 * 1024)
            return {
                "handled": True,
                "reply": f"⚠️ *{file_path.name}* is too large to send via Telegram ({file_size_mb:.1f} MB, limit is 50 MB).",
            }

        if context is not None and isinstance(context, dict):
            context["last_action"] = {
                "type": "file_fetch",
                "file_path": str(file_path),
                "timestamp": time.time(),
            }

        return {
            "handled": True,
            "type": "document",
            "file_path": str(file_path),
            "caption": f"📄 {file_path.name}",
        }

    return None


def check_deterministic_process_and_power_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check for process management, power states, and web navigation (Step 23)."""
    clean = clean_text.strip().rstrip(".?! ")
    clean_lower = clean.lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean_lower).strip()

    # Avoid capturing tasks or reminders
    if clean_lower.startswith(("add ", "create ", "remind ", "task ", "delete task", "remove task")):
        return None

    # 1. Display & Screen Control
    disp_pat = r"^(?:turn\s+off\s+(?:the\s+)?(?:screen|display|monitor)|screen\s+off|display\s+off)$"
    if re.match(disp_pat, clean_lower) or re.match(disp_pat, clean_no_punct):
        from orion.tools import workstation

        workstation.turn_off_screen()
        return {"handled": True, "reply": "🖥️ Display turned off."}

    # 2. System Sleep Mode
    sleep_pat = r"^(?:sleep(?:\s+(?:laptop|pc|computer))?|put\s+(?:laptop|pc|computer)\s+to\s+sleep)$"
    if re.match(sleep_pat, clean_lower) or re.match(sleep_pat, clean_no_punct):
        from orion.tools import workstation

        workstation.sleep_system()
        return {"handled": True, "reply": "💤 System putting itself to sleep."}

    # 3. Web Search & Direct URL Navigation
    # Direct URL
    url_match = re.match(
        r"^(?:open|go\s+to)\s+(https?://[^\s]+|[a-zA-Z0-9_\-]+\.(?:com|org|io|net|edu|dev|in|ai|gov|co|app|xyz)(?:/[^\s]*)?)$",
        clean,
        re.IGNORECASE,
    )
    if url_match:
        from orion.tools import workstation

        raw_url = url_match.group(1).strip()
        full_url = raw_url if raw_url.startswith(("http://", "https://")) else f"https://{raw_url}"
        workstation.open_browser_url(raw_url)
        return {"handled": True, "reply": f"🌐 Opening *{full_url}* in default browser."}

    # Web search
    search_match = re.match(r"^(?:search\s+(?:for\s+)?|google\s+)(.+)$", clean, re.IGNORECASE)
    if search_match:
        from orion.tools import workstation

        query_text = search_match.group(1).strip()
        engine = "google"
        display_engine = "Google"
        yt_match = re.match(r"^(?:youtube\s+for\s+|on\s+youtube\s+)(.+)$", query_text, re.IGNORECASE) or re.match(
            r"^(.+)\s+(?:on\s+youtube)$", query_text, re.IGNORECASE
        )
        if yt_match:
            engine = "youtube"
            display_engine = "YouTube"
            query_text = yt_match.group(1).strip()

        workstation.open_web_search(query_text, engine=engine)
        return {"handled": True, "reply": f"🔍 Searching {display_engine} for: _{query_text}_"}

    # 4. Top Processes / Resource Hogs
    proc_pat1 = r"^(?:what(?:'s|\s+is)\s+eating\s+(?:my\s+)?(?:ram|memory|cpu)|top\s+(?:cpu|ram|memory)\s+processes?|what\s+processes?\s+are\s+running)$"
    proc_pat2 = r"^(?:top\s+processes|list\s+processes|show\s+processes)$"
    if (
        re.match(proc_pat1, clean_lower)
        or re.match(proc_pat1, clean_no_punct)
        or re.match(proc_pat2, clean_lower)
        or re.match(proc_pat2, clean_no_punct)
    ):
        from orion.tools import process_manager

        metric = "ram" if ("ram" in clean_lower or "memory" in clean_lower) else "cpu"
        procs = process_manager.get_top_resource_processes(metric=metric, limit=5)
        reply = process_manager.format_top_processes(procs, metric=metric)
        return {"handled": True, "reply": reply}

    # 5. Safe Process Termination
    term_match = re.match(r"^(?:close|kill|terminate|stop|exit)\s+([a-zA-Z0-9_\-\. ]+)$", clean, re.IGNORECASE)
    if term_match:
        target = term_match.group(1).strip()
        target_lower = target.lower()
        clean_target = re.sub(r"^(?:the\s+|my\s+)", "", target_lower).strip()

        non_app_words = {
            "window",
            "windows",
            "tab",
            "tabs",
            "playback",
            "music",
            "audio",
            "task",
            "tasks",
            "reminder",
            "reminders",
            "screen",
            "display",
            "monitor",
            "watchdog",
        }
        if clean_target in non_app_words or clean_target.startswith(("task ", "reminder ")):
            return None

        from orion.tools import process_manager

        is_force = clean_lower.startswith("kill")
        success, reply_msg = process_manager.terminate_application(target, force=is_force)
        return {"handled": True, "reply": reply_msg}

    return None


def check_deterministic_watchdog_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check for autonomous watchdog status and controls (Step 24)."""
    clean = clean_text.strip().rstrip(".?! ")
    clean_lower = clean.lower()
    clean_no_punct = re.sub(r"[^\w\s]", "", clean_lower).strip()

    from orion.core import watchdog as wd_module

    engine = None
    if context and "watchdog" in context:
        engine = context["watchdog"]
    else:
        engine = wd_module.get_watchdog_engine()

    # 1. Watchdog status
    status_pat = r"^(?:watchdog\s+status|status\s+of\s+watchdog|is\s+watchdog\s+running)$"
    if re.match(status_pat, clean_lower) or re.match(status_pat, clean_no_punct):
        is_active = engine.state.enabled if engine else True
        state_str = "Active" if is_active else "Paused"
        interval = engine.check_interval_sec if engine else 30
        mon_str = "Active" if is_active else "Paused"
        reply = (
            f"🛡️ *Watchdog Status:* {state_str}\n"
            f"• Interval: {interval}s\n"
            f"• Battery Monitoring: {mon_str}\n"
            f"• Thermal Monitoring: {mon_str}"
        )
        return {"handled": True, "reply": reply}

    # 2. Pause watchdog
    pause_pat = r"^(?:pause\s+watchdog|disable\s+watchdog|stop\s+watchdog)$"
    if re.match(pause_pat, clean_lower) or re.match(pause_pat, clean_no_punct):
        if engine:
            engine.pause()
        return {"handled": True, "reply": "⏸️ *Autonomous Watchdog paused.*"}

    # 3. Resume watchdog
    resume_pat = r"^(?:resume\s+watchdog|enable\s+watchdog|start\s+watchdog)$"
    if re.match(resume_pat, clean_lower) or re.match(resume_pat, clean_no_punct):
        if engine:
            engine.resume()
        return {"handled": True, "reply": "▶️ *Autonomous Watchdog resumed.*"}

    return None


def _parse_typing_target_and_payload(raw: str) -> tuple[str, str | None]:
    """Parse raw typing payload into (clean_payload, target_app)."""
    raw = raw.strip()

    # 1. Quoted string with optional trailing 'in <app>'
    # e.g. '"ghee khtm" in notepad' -> ('ghee khtm', 'notepad')
    # or '"hello"' -> ('hello', None)
    m_quoted = re.match(r'^(["\'])(.*?)\1(?:\s+in\s+([a-zA-Z0-9_\-\. ]+))?$', raw, re.IGNORECASE | re.DOTALL)
    if m_quoted:
        payload = m_quoted.group(2)
        target_app = m_quoted.group(3).strip() if m_quoted.group(3) else None
        return payload, target_app

    # 2. Unquoted string followed by 'in <app>'
    # e.g. 'hello in notepad' -> ('hello', 'notepad')
    m_unquoted = re.match(r"^(.+?)\s+in\s+([a-zA-Z0-9_\-\. ]+)$", raw, re.IGNORECASE)
    if m_unquoted:
        payload = m_unquoted.group(1).strip()
        target_app = m_unquoted.group(2).strip()
        # Strip enclosing quotes from payload if any
        if (payload.startswith('"') and payload.endswith('"')) or (payload.startswith("'") and payload.endswith("'")):
            if len(payload) >= 2:
                payload = payload[1:-1]
        return payload, target_app

    # 3. Simple text without window target
    cleaned = raw
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        if len(cleaned) >= 2:
            cleaned = cleaned[1:-1]
    return cleaned, None


def _clean_type_payload(raw: str) -> str:
    """Extract clean text to type by stripping enclosing quotes and trailing window targets."""
    payload, _ = _parse_typing_target_and_payload(raw)
    return payload


def check_deterministic_input_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check for remote clipboard inspection/setting, virtual typing, and keystrokes (Step 25 / 25.1 / 25.2)."""
    clean = clean_text.strip()
    clean_lower = clean.lower().rstrip(".?! ")
    clean_no_punct = re.sub(r"[^\w\s]", "", clean_lower).strip()

    from orion.tools import input_control

    # A. Get Clipboard
    get_clip_pat = r"^(?:what(?:'s|s|\s+is)\s+(?:on|in)\s+(?:my\s+)?clipboard|get\s+clipboard|read\s+clipboard|show\s+clipboard|check\s+clipboard|clipboard)$"
    if re.match(get_clip_pat, clean_lower) or re.match(get_clip_pat, clean_no_punct):
        clip_text = input_control.get_clipboard_text()
        if not clip_text:
            return {"handled": True, "reply": "📋 *Clipboard is currently empty.*"}
        return {"handled": True, "reply": f"📋 *Clipboard Content:*\n```\n{clip_text}\n```"}

    # B. Set Clipboard
    # 1. "copy to clipboard: <text>", "copy this: <text>", "copy: <text>"
    set_clip_m1 = re.match(r"^(?:copy\s+(?:this\s+)?to\s+clipboard|copy\s+this|copy):\s*(.+)$", clean, re.IGNORECASE)
    # 2. "set clipboard to <text>", "copy <text>"
    set_clip_m2 = re.match(r"^(?:set\s+clipboard\s+to|copy)\s+(.+)$", clean, re.IGNORECASE)
    set_clip_match = set_clip_m1 or set_clip_m2
    if set_clip_match:
        target_text = set_clip_match.group(1).strip()
        # Exclude file operations like "copy file report.pdf to documents"
        if not (
            target_text.lower().startswith(("file ", "folder ", "directory "))
            or re.search(r"\bto\s+(?:downloads|documents|desktop)\b", target_text.lower())
        ):
            input_control.set_clipboard_text(target_text)
            return {"handled": True, "reply": "📋 Copied to laptop clipboard."}

    # C. Compound Typing & Key Press (e.g. "type d and press enter", "type 'hello' and hit enter in notepad")
    compound_pat = r"^(?:type|inject\s+text)\s+(.+?)\s+and\s+(?:press|hit)\s+(enter|escape|esc|tab|space|backspace)(?:\s+in\s+([a-zA-Z0-9_\-\. ]+))?$"
    m_compound = re.match(compound_pat, clean, re.IGNORECASE)
    if m_compound:
        raw_payload = m_compound.group(1).strip()
        special_key = m_compound.group(2).strip()
        target_app = m_compound.group(3).strip() if m_compound.group(3) else None

        clean_payload = _clean_type_payload(raw_payload)
        effective_app = target_app or input_control.get_last_target_app()

        if target_app:
            if not input_control.focus_window_by_name(target_app):
                return {
                    "handled": True,
                    "reply": f"⚠️ Could not find an active window for *{target_app}*. Please open it first.",
                }
        elif effective_app:
            input_control.focus_window_by_name(effective_app)

        input_control.type_text(clean_payload)
        input_control.press_special_key(special_key)
        if effective_app:
            return {"handled": True, "reply": f"⌨️ Typed *{clean_payload}* and pressed *{special_key.upper()}* in *{effective_app}*."}
        return {"handled": True, "reply": f"⌨️ Typed *{clean_payload}* and pressed *{special_key.upper()}*."}

    # D. Special Keys & Single Key Press (e.g. "press enter", "press d")
    key_match = re.match(r"^(?:press|hit)\s+(enter|escape|esc|tab|space|backspace|[a-zA-Z0-9])$", clean_lower)
    if key_match:
        key_name = key_match.group(1).strip()
        effective_app = input_control.get_last_target_app()
        if effective_app:
            input_control.focus_window_by_name(effective_app)
        input_control.press_special_key(key_name)
        if effective_app:
            return {"handled": True, "reply": f"🔘 Pressed *{key_name.upper()}* in *{effective_app}*."}
        return {"handled": True, "reply": f"🔘 Pressed *{key_name.upper()}*."}

    # E. Type Text (Remote Typing with target window support)
    # 1. "type in <app>: <text>", "type in: <text>", "type this: <text>", "type: <text>"
    type_m1 = re.match(
        r"^(?:type\s+(?:this\s+)?in\s+([a-zA-Z0-9_\-\. ]+)|type\s+(?:this\s+)?in|type\s+this|type):\s*(.+)$",
        clean,
        re.IGNORECASE,
    )
    # 2. "type <text>", "inject text <text>"
    type_m2 = re.match(r"^(?:type|inject\s+text)\s+(.+)$", clean, re.IGNORECASE)
    # 3. "type key <char>"
    type_m3 = re.match(r"^type\s+key\s+([a-zA-Z0-9])$", clean, re.IGNORECASE)

    if type_m1:
        target_app = type_m1.group(1).strip() if type_m1.group(1) else None
        raw_payload = type_m1.group(2).strip()
        clean_payload, inferred_target = _parse_typing_target_and_payload(raw_payload)
        target_app = target_app or inferred_target
        effective_app = target_app or input_control.get_last_target_app()
        if not clean_payload.lower().startswith("of "):
            if target_app:
                if not input_control.focus_window_by_name(target_app):
                    return {
                        "handled": True,
                        "reply": f"⚠️ Could not find an active window for *{target_app}*. Please open it first.",
                    }
                input_control.type_text(clean_payload)
                return {"handled": True, "reply": f"⌨️ Typed text into *{target_app}*."}
            elif effective_app:
                input_control.focus_window_by_name(effective_app)
                input_control.type_text(clean_payload)
                return {"handled": True, "reply": f"⌨️ Typed text into *{effective_app}*."}
            else:
                input_control.type_text(clean_payload)
                return {"handled": True, "reply": "⌨️ Typed text into active window."}

    elif type_m2 or type_m3:
        m = type_m2 or type_m3
        raw_payload = m.group(1).strip()
        clean_payload, target_app = _parse_typing_target_and_payload(raw_payload)
        effective_app = target_app or input_control.get_last_target_app()
        if not clean_payload.lower().startswith("of "):
            if target_app:
                if not input_control.focus_window_by_name(target_app):
                    return {
                        "handled": True,
                        "reply": f"⚠️ Could not find an active window for *{target_app}*. Please open it first.",
                    }
                input_control.type_text(clean_payload)
                return {"handled": True, "reply": f"⌨️ Typed text into *{target_app}*."}
            elif effective_app:
                input_control.focus_window_by_name(effective_app)
                input_control.type_text(clean_payload)
                return {"handled": True, "reply": f"⌨️ Typed text into *{effective_app}*."}
            else:
                input_control.type_text(clean_payload)
                return {"handled": True, "reply": "⌨️ Typed text into active window."}

    return None


def check_deterministic_memory_and_briefing_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check for long-term memory operations and executive daily briefings (Step 26)."""
    clean = clean_text.strip()
    clean_lower = clean.lower().rstrip(".?! ")

    from orion.core.briefing import generate_executive_briefing
    from orion.core.memory_store import get_memory_store

    db_path = context.get("db_path") if context else None
    mem_store = get_memory_store(db_path=db_path)

    # A. DAILY BRIEFING
    briefing_pat = r"^(?:give\s+me\s+(?:my\s+)?(?:daily\s+|morning\s+)?briefing|daily\s+briefing|morning\s+briefing|briefing)$"
    if re.match(briefing_pat, clean_lower):
        briefing_md = generate_executive_briefing(db_path=db_path)
        return {"handled": True, "reply": briefing_md}

    # B. REMEMBER FACT
    rem_pat = r"^(?:remember\s+(?:that\s+)?|store\s+memory:\s*)(.+)$"
    m_rem = re.match(rem_pat, clean, re.IGNORECASE)
    if m_rem:
        fact_text = m_rem.group(1).strip()
        if fact_text:
            mem_store.save_fact(fact_text)
            return {"handled": True, "reply": f"🧠 *Memory stored:*\n_{fact_text}_"}

    # C. LIST MEMORIES
    list_pat = r"^(?:what\s+do\s+you\s+remember(?:\s+about\s+me)?|show\s+memories|list\s+memories|my\s+memories)$"
    if re.match(list_pat, clean_lower):
        memories = mem_store.get_all_memories()
        return {"handled": True, "reply": mem_store.format_memories_list(memories)}

    # D. FORGET MEMORY
    forget_pat = r"^(?:forget\s+memory|delete\s+memory)\s+([a-zA-Z0-9_\-]+)$"
    m_forget = re.match(forget_pat, clean, re.IGNORECASE)
    if m_forget:
        identifier = m_forget.group(1).strip()
        deleted = mem_store.delete_memory(identifier)
        if deleted:
            return {"handled": True, "reply": f"🗑️ *Memory forgotten:* `{identifier}`"}
        else:
            return {"handled": True, "reply": f"⚠️ Memory `{identifier}` not found."}

    return None


async def check_deterministic_shell_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check for remote shell/cmd execution commands (Step 27)."""
    clean = clean_text.strip()

    from orion.tools import shell_bridge

    # A. PowerShell: "ps: <cmd>", "powershell: <cmd>"
    m_ps = re.match(r"^(?:ps|powershell):\s*(.+)$", clean, re.IGNORECASE | re.DOTALL)
    if m_ps:
        cmd = m_ps.group(1).strip()
        if cmd:
            res = await shell_bridge.execute_shell_command(cmd, shell_type="powershell")
            reply = shell_bridge.format_shell_output(res, cmd, shell_type="powershell")
            return {"handled": True, "reply": reply}

    # B. CMD: "cmd: <cmd>", "terminal: <cmd>", "run: <cmd>", "sh: <cmd>"
    m_cmd = re.match(r"^(?:cmd|terminal|run|sh):\s*(.+)$", clean, re.IGNORECASE | re.DOTALL)
    if m_cmd:
        cmd = m_cmd.group(1).strip()
        if cmd:
            res = await shell_bridge.execute_shell_command(cmd, shell_type="cmd")
            reply = shell_bridge.format_shell_output(res, cmd, shell_type="cmd")
            return {"handled": True, "reply": reply}

    return None


def check_deterministic_mouse_query(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check for remote mouse cursor and click queries (Step 28)."""
    clean = clean_text.strip()
    clean_lower = clean.lower().rstrip(".?! ")

    from orion.tools import mouse_control

    # A. CLICK & DOUBLE CLICK
    # 1. Double Click
    m_dbl = re.match(r"^(?:double\s+click)(?:\s+at\s+(\d+)[,\s]+(\d+))?$", clean_lower)
    if m_dbl:
        x_str, y_str = m_dbl.group(1), m_dbl.group(2)
        if x_str and y_str:
            x, y = int(x_str), int(y_str)
            mouse_control.mouse_click(x, y, button="left", clicks=2)
            return {"handled": True, "reply": f"🖱️ Double-clicked at ({x}, {y})."}
        else:
            mouse_control.mouse_click(button="left", clicks=2)
            return {"handled": True, "reply": "🖱️ Double-clicked."}

    # 2. Right Click
    m_right = re.match(r"^(?:right\s+click)(?:\s+at\s+(\d+)[,\s]+(\d+))?$", clean_lower)
    if m_right:
        x_str, y_str = m_right.group(1), m_right.group(2)
        if x_str and y_str:
            x, y = int(x_str), int(y_str)
            mouse_control.mouse_click(x, y, button="right", clicks=1)
            return {"handled": True, "reply": f"🖱️ Right-clicked at ({x}, {y})."}
        else:
            mouse_control.mouse_click(button="right", clicks=1)
            return {"handled": True, "reply": "🖱️ Right-clicked."}

    # 3. Left Click / Click
    m_click = re.match(r"^(?:click|left\s+click)(?:\s+at\s+(\d+)[,\s]+(\d+))?$", clean_lower)
    if m_click:
        x_str, y_str = m_click.group(1), m_click.group(2)
        if x_str and y_str:
            x, y = int(x_str), int(y_str)
            mouse_control.mouse_click(x, y, button="left", clicks=1)
            return {"handled": True, "reply": f"🖱️ Clicked at ({x}, {y})."}
        else:
            mouse_control.mouse_click(button="left", clicks=1)
            return {"handled": True, "reply": "🖱️ Left clicked."}

    # B. MOVE CURSOR
    m_move = re.match(r"^(?:move\s+mouse\s+(?:to\s+)?|move\s+cursor\s+(?:to\s+)?)(\d+)[,\s]+(\d+)$", clean_lower)
    if m_move:
        x, y = int(m_move.group(1)), int(m_move.group(2))
        mouse_control.move_cursor(x, y)
        return {"handled": True, "reply": f"🖱️ Moved cursor to ({x}, {y})."}

    # C. MOUSE POSITION
    m_pos = re.match(r"^(?:where\s+is\s+my\s+mouse|get\s+mouse\s+pos(?:ition)?|cursor\s+pos(?:ition)?)$", clean_lower)
    if m_pos:
        x, y = mouse_control.get_cursor_position()
        return {"handled": True, "reply": f"🖱️ *Cursor Position:* ({x}, {y})"}

    # D. SCROLL
    m_scroll = re.match(r"^(?:scroll\s+(up|down))(?:\s+(\d+))?$", clean_lower)
    if m_scroll:
        direction = m_scroll.group(1)
        steps = int(m_scroll.group(2)) if m_scroll.group(2) else 3
        mouse_control.mouse_scroll(steps=steps, direction=direction)
        return {"handled": True, "reply": f"📜 Scrolled {direction} ({steps} steps)."}

    # E. CLICK UI ELEMENT BY NAME (Steps 29 & 29.1)
    ui_click_pat = r"^(?:click|tap|press)\s+(?:on\s+)?(?:the\s+)?([a-zA-Z0-9_\- ]+?)(?:\s+button|\s+tab|\s+menu)?(?:\s+(?:in|on)\s+([a-zA-Z0-9_\-\. ]+))?$"
    m_ui = re.match(ui_click_pat, clean.rstrip(".?! "), re.IGNORECASE)
    if m_ui:
        element_name = m_ui.group(1).strip()
        target_app = m_ui.group(2).strip() if m_ui.group(2) else None
        excluded = {"mouse", "cursor", "left", "right", "middle", "double"}
        special_keys = {"enter", "return", "escape", "esc", "tab", "space", "backspace"}
        if (
            element_name.lower() not in excluded
            and element_name.lower() not in special_keys
            and not (len(element_name) == 1 and clean_lower.startswith("press "))
        ):
            from orion.tools import ui_locator

            if target_app:
                success, reply_msg = ui_locator.click_element_by_name(element_name, target_app=target_app)
            else:
                success, reply_msg = ui_locator.click_element_by_name(element_name)
            return {"handled": True, "reply": reply_msg}

    return None


SPLIT_CLAUSE_REGEX = re.compile(
    r",\s*(?:and\s+)?then\s+"
    r"|\s+(?:and\s+)?then\s+"
    r"|,\s*(?:and\s+)?(?=(?:open|launch|start|run|minimize|minimise|maximize|maximise|restore|focus|bring|switch)\b)"
    r"|\s+and\s+(?=(?:open|launch|start|run|minimize|minimise|maximize|maximise|restore|focus|bring|switch)\b)",
    re.IGNORECASE,
)


def parse_sequential_command(raw_target: str) -> tuple[str, str] | None:
    """Check if raw_target is '<app> then/and <window_action> [it]'.

    Returns (app_candidate, normalized_action) or None.
    e.g. ('calculator', 'maximize')
    """
    pattern = (
        r"^(.+?)\s+(?:then\s+|and\s+then\s+|and\s+)"
        r"(maximize|maximise|minimize|minimise|restore|focus|bring\s+(?:it\s+)?to\s+(?:the\s+)?(?:front|foreground)|bring\s+up)"
        r"(?:\s+it)?[\s.!?]*$"
    )
    m = re.match(pattern, raw_target.strip(), re.IGNORECASE)
    if m:
        app_part = m.group(1).strip()
        action_verb = m.group(2).strip().lower()
        if "front" in action_verb or "foreground" in action_verb or "up" in action_verb:
            norm_action = "focus"
        elif "max" in action_verb:
            norm_action = "maximize"
        elif "min" in action_verb:
            norm_action = "minimize"
        elif "restore" in action_verb:
            norm_action = "restore"
        elif "focus" in action_verb:
            norm_action = "focus"
        else:
            norm_action = action_verb
        return app_part, norm_action
    return None


def parse_multi_app_targets(raw_target: str) -> list[str]:
    """Split targets string by commas, 'and', '&' into individual app query candidates."""
    parts = re.split(r",\s*(?:and\s+|&\s+)?|\s+(?:and|&)\s+", raw_target.strip(), flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def check_deterministic_multi_action(clean_text: str) -> dict[str, Any] | None:
    """Parse and validate natural language multi-action or sequential Windows operations.

    Supports:
    - Multi-launch: "open calc and vs code", "launch notepad, calculator and explorer"
    - Sequential launch + window action: "open calc then maximize it", "open calc then maximise it"
    - Interdependent multi-action: "Open notepad and calculator, then minimize notepad"
    - Focus with pronoun: "Open VS Code and bring it to the front"
    - Window action then launch: "Minimize calculator then open explorer"
    - Resolution of 'it' / 'the app' to previously mentioned application
    - Safe execution limit (max 4 actions)
    """
    text = clean_text.strip()
    text = re.sub(
        r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    clauses = [c.strip() for c in SPLIT_CLAUSE_REGEX.split(text) if c.strip()]
    if not clauses:
        return None

    verbs_pat = r"^(?:open|launch|start|run|minimize|minimise|maximize|maximise|restore|focus|bring|switch|put)\b"
    if not any(re.match(verbs_pat, c, re.IGNORECASE) for c in clauses):
        return None

    read_only_starts = (
        "what", "which", "show", "list", "is", "how", "who", "are", "tell"
    )
    first_word = text.split()[0].lower() if text.split() else ""
    if first_word in read_only_starts:
        return None

    parsed_actions: list[dict[str, Any]] = []
    last_app_id: str | None = None

    for clause in clauses:
        c = clause.strip(" .!?\"'")
        c = re.sub(
            r"(?i)\s+(?:on\s+my\s+laptop|on\s+my\s+pc|on\s+my\s+computer|on\s+windows)[\.!\?]?$",
            "",
            c,
        ).strip()

        # 1. Launch verbs
        m_launch = re.match(r"^(?:open|launch|start|run)\s+(.+)$", c, re.IGNORECASE)
        if m_launch:
            raw_target = m_launch.group(1).strip()
            raw_target = re.sub(r"(?i)\s+(?:app|application|program)[\.!\?]?$", "", raw_target).strip(" .!?\"'")

            # Ignore conversational phrases
            conversational_words = {
                "up", "down", "out", "away", "faster", "slower", "again", "over", "by", "with",
                "your", "my", "our", "their", "his", "her", "me", "you", "us", "them",
                "talking", "thinking", "explaining", "doing", "helping", "looking", "listening",
                "conversation", "dialogue", "meeting", "discussion", "eyes", "door", "heart", "mind",
            }
            target_tokens = set(re.findall(r"\b\w+\b", raw_target.lower()))
            if target_tokens & conversational_words:
                return None

            seq = parse_sequential_command(raw_target)
            if seq:
                app_cand, norm_act = seq
                blocked, reason = is_app_blocked(app_cand)
                if blocked:
                    return {"handled": True, "reply": f"Application '{app_cand}' is not in the approved application allowlist. {reason}"}
                aid, clarif = resolve_app_or_clarification(app_cand, norm_act)
                if clarif:
                    return clarif
                if not aid:
                    return {"handled": True, "reply": f"Application '{app_cand}' is not in the approved application allowlist. ORION only has permission to launch 20 explicitly sanctioned applications. Send `/apps` to see the full list."}
                disp = _get_short_display_name(aid)
                is_avail, _, _ = is_app_available(aid)
                if not is_avail:
                    return {"handled": True, "reply": f"Cannot launch {disp}: Application is not currently installed or available on this system."}
                parsed_actions.append({"action": "launch", "app_id": aid, "display_name": disp})
                parsed_actions.append({"action": norm_act, "app_id": aid, "display_name": disp})
                last_app_id = aid
                continue

            sub_targets = parse_multi_app_targets(raw_target)
            if len(clauses) == 1 and len(sub_targets) < 2:
                # Single target with no sequential action - delegate to check_deterministic_app_launch
                return None
            for st in sub_targets:
                st_clean = st.strip(" .!?\"'")
                st_lower = st_clean.lower()
                if st_lower in ("it", "the app", "this", "that"):
                    if not last_app_id:
                        return None
                    aid = last_app_id
                else:
                    blocked, reason = is_app_blocked(st_clean)
                    if blocked:
                        return {"handled": True, "reply": f"Application '{st_clean}' is not in the approved application allowlist. {reason}"}
                    aid, clarif = resolve_app_or_clarification(st_clean, "open")
                    if clarif:
                        return clarif
                    if not aid:
                        return {"handled": True, "reply": f"Application '{st_clean}' is not in the approved application allowlist. ORION only has permission to launch 20 explicitly sanctioned applications. Send `/apps` to see the full list."}
                disp = _get_short_display_name(aid)
                is_avail, _, _ = is_app_available(aid)
                if not is_avail:
                    return {"handled": True, "reply": f"Cannot launch {disp}: Application is not currently installed or available on this system."}
                parsed_actions.append({"action": "launch", "app_id": aid, "display_name": disp})
                last_app_id = aid
            continue

        # 2. Window management verbs
        win_action = None
        raw_win_target = ""

        m_foc = re.match(
            r"^(?:focus|bring\s+up|bring\s+(?:to\s+(?:the\s+)?(?:front|foreground)|up)|switch\s+to|foreground)\s*(.*)$",
            c,
            re.IGNORECASE,
        )
        if not m_foc:
            m_foc = re.match(
                r"^(?:bring|put)\s+(.+?)\s+(?:to\s+(?:the\s+)?(?:front|foreground)|up)$",
                c,
                re.IGNORECASE,
            )
        if m_foc:
            win_action = "focus"
            raw_win_target = m_foc.group(1).strip()
        elif re.match(r"^(?:maximize|maximise)\b", c, re.IGNORECASE):
            win_action = "maximize"
            raw_win_target = re.sub(r"^(?:maximize|maximise)\s*", "", c, flags=re.IGNORECASE).strip()
        elif re.match(r"^(?:minimize|minimise)\b", c, re.IGNORECASE):
            win_action = "minimize"
            raw_win_target = re.sub(r"^(?:minimize|minimise)\s*", "", c, flags=re.IGNORECASE).strip()
        elif re.match(r"^restore\b", c, re.IGNORECASE):
            win_action = "restore"
            raw_win_target = re.sub(r"^restore\s*", "", c, flags=re.IGNORECASE).strip()

        if win_action:
            raw_win_target = raw_win_target.strip(" .!?\"'")
            m_idx = re.search(r"\bwindow\s+(\d+)\b", raw_win_target, re.IGNORECASE)
            win_idx = int(m_idx.group(1)) if m_idx else None
            raw_win_target = re.sub(r"\bwindow\s+\d+\b", "", raw_win_target).strip(" .!?\"'")
            raw_win_target = re.sub(r"(?i)\s+(?:app|application|window|program)[\.!\?]?$", "", raw_win_target).strip(" .!?\"'")

            t_lower = raw_win_target.lower()
            if t_lower in ("it", "the app", "this", "that", ""):
                if not last_app_id:
                    return None
                aid = last_app_id
            else:
                blocked, reason = is_app_blocked(raw_win_target)
                if blocked:
                    return {"handled": True, "reply": f"Application '{raw_win_target}' is not in the approved application allowlist. {reason}"}
                aid, clarif = resolve_app_or_clarification(raw_win_target, win_action)
                if clarif:
                    return clarif
                if not aid:
                    return {"handled": True, "reply": f"Application '{raw_win_target}' is not in the approved application allowlist. ORION only has permission to launch 20 explicitly sanctioned applications. Send `/apps` to see the full list."}

            disp = _get_short_display_name(aid)
            act_dict: dict[str, Any] = {"action": win_action, "app_id": aid, "display_name": disp}
            if win_idx is not None:
                act_dict["window_index"] = win_idx
            parsed_actions.append(act_dict)
            last_app_id = aid

    if len(parsed_actions) > 4:
        return {
            "handled": True,
            "reply": "For security and system stability, ORION can execute at most 4 application operations per request.",
        }

    if len(parsed_actions) < 2:
        return None

    if (
        len(parsed_actions) == 2
        and parsed_actions[0]["action"] == "launch"
        and parsed_actions[1]["action"] != "launch"
        and parsed_actions[0]["app_id"] == parsed_actions[1]["app_id"]
    ):
        aid = parsed_actions[0]["app_id"]
        disp = parsed_actions[0]["display_name"]
        w_act = parsed_actions[1]["action"]
        act_verb = {
            "maximize": "maximize",
            "minimize": "minimize",
            "focus": "bring to the foreground",
            "restore": "restore",
        }.get(w_act, w_act)
        proposal_text = (
            "⚠️ **Action Confirmation Required**\n\n"
            f"Launch and {act_verb} **{disp}** on your laptop?\n\n"
            "*Confirm to proceed.*"
        )
        clean_args = {
            "command_type": "launch_and_window",
            "target_app_id": aid,
            "window_action": w_act,
            "summary": f"Launch and {act_verb} {disp}",
            "display_name": disp,
            "actions": parsed_actions,
        }
        return {
            "handled": True,
            "is_proposal": True,
            "action_type": "composite_app_action",
            "arguments": clean_args,
            "proposal_text": proposal_text,
        }

    if all(a["action"] == "launch" for a in parsed_actions):
        disp_names = [a["display_name"] for a in parsed_actions]
        if len(disp_names) == 2:
            joined_bold = f"**{disp_names[0]}** and **{disp_names[1]}**"
            joined_plain = f"{disp_names[0]} and {disp_names[1]}"
        else:
            joined_bold = f"{', '.join(f'**{n}**' for n in disp_names[:-1])}, and **{disp_names[-1]}**"
            joined_plain = f"{', '.join(disp_names[:-1])} and {disp_names[-1]}"

        proposal_text = (
            "⚠️ **Action Confirmation Required**\n\n"
            f"Launch {joined_bold} on your laptop?\n\n"
            "*Confirm to proceed.*"
        )
        clean_args = {
            "command_type": "multi_launch",
            "app_ids": [a["app_id"] for a in parsed_actions],
            "summary": f"Launch {joined_plain}",
            "display_names": disp_names,
            "actions": parsed_actions,
        }
        return {
            "handled": True,
            "is_proposal": True,
            "action_type": "composite_app_action",
            "arguments": clean_args,
            "proposal_text": proposal_text,
        }

    prop_lines = ["⚠️ **Action Confirmation Required**\n\nORION proposes the following operations:"]
    for a in parsed_actions:
        v_label = {
            "launch": "Launch",
            "maximize": "Maximize",
            "minimize": "Minimize",
            "focus": "Bring to front",
            "restore": "Restore",
        }.get(a["action"], a["action"].title())
        prop_lines.append(f"• {v_label} **{a['display_name']}**")
    prop_lines.append("\n*Confirm to proceed.*")
    proposal_text = "\n".join(prop_lines)

    clean_args = {
        "command_type": "composite",
        "actions": parsed_actions,
        "summary": f"{len(parsed_actions)} application operations",
    }
    return {
        "handled": True,
        "is_proposal": True,
        "action_type": "composite_app_action",
        "arguments": clean_args,
        "proposal_text": proposal_text,
    }


def check_deterministic_app_launch(
    clean_text: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Deterministically check if text is an imperative app launch, multi-app, or blocked app command.

    Avoids false positives in ordinary conversation while providing instant, reliable
    routing for approved launch commands and instant rejection for blocked apps.
    """
    # Check multi-action / sequential command first
    multi_res = check_deterministic_multi_action(clean_text)
    if multi_res:
        return multi_res
    clean = clean_text.strip()
    match = re.match(
        r"^(?:(?:hey\s+|hi\s+)?orion[,\s]+)?(?:please\s+)?(?:can\s+you\s+|could\s+you\s+)?(open|launch|start|run)\s+(.+)$",
        clean,
        re.IGNORECASE,
    )
    if not match:
        return None

    raw_target = match.group(2).strip()
    raw_target = re.sub(
        r"(?i)\s+(?:on\s+my\s+laptop|on\s+my\s+pc|on\s+my\s+computer|on\s+windows|app|application|program)[\.!\?]?$",
        "",
        raw_target,
    )
    raw_target = raw_target.strip(" .!?\"'")

    if not raw_target:
        return {
            "handled": True,
            "reply": "Which application would you like to launch? Send `/apps` to view the list of approved applications.",
        }

    # Ignore ordinary conversational phrases starting with open/launch/start/run
    conversational_words = {
        "up", "down", "out", "away", "faster", "slower", "again", "over", "by", "with",
        "your", "my", "our", "their", "his", "her", "me", "you", "us", "them",
        "talking", "thinking", "explaining", "doing", "helping", "looking", "listening",
        "conversation", "dialogue", "meeting", "discussion", "eyes", "door", "heart", "mind",
    }
    target_tokens = set(re.findall(r"\b\w+\b", raw_target.lower()))
    if target_tokens & conversational_words:
        return None

    # Check if target is a web URL or domain (Step 23): let process & web navigation router handle it
    if re.match(
        r"^(?:https?://|[a-zA-Z0-9_\-]+\.(?:com|org|io|net|edu|dev|in|ai|gov|co|app|xyz)(?:/[^\s]*)?$)",
        raw_target,
        re.IGNORECASE,
    ):
        return None

    # Check if target is a watchdog control command (Step 24): let watchdog router handle it
    if raw_target.lower() in ("watchdog", "autonomous watchdog"):
        return None

    if raw_target.lower() in PRONOUN_APP_TARGETS:
        resolved_app, reason = resolve_target_app_from_context(raw_target, context)
        if resolved_app:
            raw_target = resolved_app
        else:
            return {
                "handled": True,
                "reply": "Which application would you like to launch? Send `/apps` to view the list of approved applications.",
            }

    # 1. Sequential command check: e.g. "open calculator then maximize it", "calc and maximize it"
    seq = parse_sequential_command(raw_target)
    if seq:
        app_cand, norm_action = seq
        blocked, reason = is_app_blocked(app_cand)
        if blocked:
            return {"handled": True, "reply": f"Application '{app_cand}' is not in the approved application allowlist. {reason}"}

        app_id, clarif = resolve_app_or_clarification(app_cand, norm_action)
        if clarif:
            return clarif
        if not app_id:
            return {
                "handled": True,
                "reply": f"Application '{app_cand}' is not in the approved application allowlist. ORION only has permission to launch 20 explicitly sanctioned applications. Send `/apps` to see the full list.",
            }

        disp = _get_short_display_name(app_id)
        is_avail, _, _ = is_app_available(app_id)
        if not is_avail:
            return {"handled": True, "reply": f"Cannot launch {disp}: Application is not currently installed or available on this system."}

        act_verb = {
            "maximize": "maximize",
            "minimize": "minimize",
            "focus": "bring to the foreground",
            "restore": "restore",
        }.get(norm_action, norm_action)

        proposal_text = (
            "⚠️ **Action Confirmation Required**\n\n"
            f"Launch and {act_verb} **{disp}** on your laptop?\n\n"
            "*Confirm to proceed.*"
        )
        clean_args = {
            "command_type": "launch_and_window",
            "target_app_id": app_id,
            "window_action": norm_action,
            "summary": f"Launch and {act_verb} {disp}",
            "display_name": disp,
        }
        return {
            "handled": True,
            "is_proposal": True,
            "action_type": "composite_app_action",
            "arguments": clean_args,
            "proposal_text": proposal_text,
        }

    # 2. Multi-app launch check: e.g. "open calc and vs code", "launch notepad, calculator and explorer"
    targets = parse_multi_app_targets(raw_target)
    if len(targets) > 1:
        # Check if any target is blocked
        for t in targets:
            blocked, reason = is_app_blocked(t)
            if blocked:
                return {"handled": True, "reply": f"Application '{t}' is not in the approved application allowlist. {reason}"}

        # Resolve each target
        resolved_ids: list[str] = []
        for t in targets:
            aid, clarif = resolve_app_or_clarification(t, "open")
            if clarif:
                return clarif
            if not aid:
                return {
                    "handled": True,
                    "reply": f"Application '{t}' is not in the approved application allowlist. ORION only has permission to launch 20 explicitly sanctioned applications. Send `/apps` to see the full list.",
                }
            resolved_ids.append(aid)

        display_names = [_get_short_display_name(aid) for aid in resolved_ids]

        # Check availability
        unavail: list[str] = []
        for aid, disp in zip(resolved_ids, display_names):
            is_avail, _, _ = is_app_available(aid)
            if not is_avail:
                unavail.append(disp)
        if unavail:
            return {"handled": True, "reply": f"Cannot launch {', '.join(unavail)}: Application is not currently installed or available on this system."}

        if len(display_names) == 2:
            joined_bold = f"**{display_names[0]}** and **{display_names[1]}**"
            joined_plain = f"{display_names[0]} and {display_names[1]}"
        else:
            joined_bold = f"{', '.join(f'**{n}**' for n in display_names[:-1])}, and **{display_names[-1]}**"
            joined_plain = f"{', '.join(display_names[:-1])} and {display_names[-1]}"

        proposal_text = (
            "⚠️ **Action Confirmation Required**\n\n"
            f"Launch {joined_bold} on your laptop?\n\n"
            "*Confirm to proceed.*"
        )
        clean_args = {
            "command_type": "multi_launch",
            "app_ids": resolved_ids,
            "summary": f"Launch {joined_plain}",
            "display_names": display_names,
        }
        return {
            "handled": True,
            "is_proposal": True,
            "action_type": "composite_app_action",
            "arguments": clean_args,
            "proposal_text": proposal_text,
        }

    # 3. Single application: check if known blocked application
    blocked, reason = is_app_blocked(raw_target)
    if blocked:
        return {
            "handled": True,
            "reply": f"Application '{raw_target}' is not in the approved application allowlist. {reason}",
        }

    # 4. Check if approved application alias
    app_id, clarif = resolve_app_or_clarification(raw_target, "open")
    if clarif:
        return clarif
    if app_id:
        app = get_approved_apps()[app_id]
        is_avail, avail_msg, _ = is_app_available(app_id)
        if not is_avail:
            return {
                "handled": True,
                "reply": f"Cannot launch {app.display_name}: Application is not currently installed or available on this system.",
            }

        clean_args = {"app_id": app_id}
        proposal_text = format_action_proposal("launch_application", clean_args)
        return {
            "handled": True,
            "is_proposal": True,
            "action_type": "launch_application",
            "arguments": clean_args,
            "proposal_text": proposal_text,
        }

    # 5. If query looks like an unapproved app launch attempt
    words = raw_target.split()
    if len(words) <= 4 and re.match(r"^[a-zA-Z0-9_\-\.\s]+$", raw_target):
        # Exclude ordinary conversational phrases starting with open/launch/start/run
        conversational_words = {
            "up",
            "down",
            "out",
            "away",
            "faster",
            "slower",
            "again",
            "over",
            "by",
            "with",
            "your",
            "my",
            "our",
            "their",
            "his",
            "her",
            "talking",
            "thinking",
            "explaining",
            "doing",
            "helping",
            "looking",
            "listening",
            "conversation",
            "dialogue",
            "meeting",
            "discussion",
            "eyes",
            "door",
            "heart",
            "mind",
        }
        target_tokens = set(re.findall(r"\b\w+\b", raw_target.lower()))
        if target_tokens & conversational_words:
            return None

        return {
            "handled": True,
            "reply": f"Application '{raw_target}' is not in the approved application allowlist. ORION only has permission to launch 20 explicitly sanctioned applications. Send `/apps` to see the full list.",
        }

    return None


async def route_and_execute_tool(
    user_text: str,
    ollama_client: OllamaClient | None = None,
    registry: ToolRegistry | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Analyze user message for tool invocation, validate, and propose or execute at most one tool.

    For read-only tools: executes immediately and returns reply text.
    For write action tools: validates and returns a pending action proposal requiring user confirmation.
    Returns None if message should proceed through standard conversation chat.
    """
    clean_text = user_text.strip()
    if not clean_text:
        return None

    reg = registry or default_tool_registry

    # 0. Deterministic repeat follow-up query ("again", "one more", etc.) (Step 21)
    repeat_res = check_deterministic_repeat_query(clean_text, context=context)
    if repeat_res:
        return repeat_res

    # 1. Deterministic multi-action and sequential command detection
    multi_res = check_deterministic_multi_action(clean_text)
    if multi_res:
        return multi_res

    # 2. Deterministic window and desktop query detection
    win_query = check_deterministic_window_query(clean_text, context=context)
    if win_query:
        if win_query.get("handled"):
            return win_query

        # Window management action (Step 11): needs confirmation proposal
        if win_query.get("is_window_action"):
            tool_name = win_query["tool"]
            tool_args = win_query.get("arguments", {})
            app_id = tool_args.get("app_id", "")

            # Validate app exists in approved list
            approved = get_approved_apps()
            if not app_id or app_id not in approved:
                return {"handled": True, "reply": f"Application is not in the approved application allowlist."}

            # Pre-check: does the app have any open windows?
            from orion.tools.desktop_inspector import _enumerate_windows_native as _enum_wins
            try:
                current_wins = _enum_wins()
            except Exception:
                current_wins = []
            app_windows = [w for w in current_wins if w.app_id == app_id]
            if not app_windows:
                disp = approved[app_id].display_name
                return {"handled": True, "reply": f"{disp} does not have any open windows."}

            # If multiple windows, no token, and no index: check if target is ambiguous
            has_token_or_idx = tool_args.get("window_token") or tool_args.get("window_index")
            if len(app_windows) > 1 and not has_token_or_idx:
                active_wins = [w for w in app_windows if w.is_active]
                if len(active_wins) != 1:
                    from orion.tools.desktop_inspector import create_window_tokens

                    act_short = tool_name.replace("_application_window", "")
                    options = create_window_tokens(app_id, app_windows, action=act_short)
                    sel_res = {
                        "requires_selection": True,
                        "app_id": app_id,
                        "display_name": approved[app_id].display_name,
                        "windows": options,
                    }
                    reply = format_tool_result(tool_name, sel_res)
                    return {
                        "handled": True,
                        "reply": reply,
                        "requires_selection": True,
                        "windows": options,
                        "action_type": act_short,
                        "app_id": app_id,
                        "display_name": approved[app_id].display_name,
                    }

            # Single window (or active window / token provided): return proposal for confirmation
            proposal_text = format_action_proposal(tool_name, tool_args)
            return {
                "handled": True,
                "is_proposal": True,
                "action_type": tool_name,
                "arguments": tool_args,
                "proposal_text": proposal_text,
            }

        # Read-only window/desktop query: execute immediately
        tool_name = win_query["tool"]
        tool_args = win_query.get("arguments", {})
        exec_res = reg.execute(tool_name, tool_args)
        reply = format_tool_result(tool_name, exec_res)
        return {"handled": True, "tool": tool_name, "result": exec_res, "reply": reply}

    # 3. Deterministic app launch detection before model routing
    launch_res = check_deterministic_app_launch(clean_text, context=context)
    if launch_res:
        return launch_res

    # 4. Deterministic reminder commands (Step 17)
    from orion.core.reminders import handle_reminder_text

    reminder_res = handle_reminder_text(clean_text, context=context)
    if reminder_res:
        return reminder_res

    # 5. Deterministic system resource queries (Step 18)
    sys_res = check_deterministic_system_query(clean_text, context=context)
    if sys_res:
        return sys_res

    # 6. Deterministic task management queries (Step 19)
    task_res = check_deterministic_task_query(clean_text, context=context)
    if task_res:
        return task_res

    # 7. Deterministic screen capture query (Step 20)
    screen_res = check_deterministic_screen_query(clean_text, context=context)
    if screen_res:
        return screen_res

    # 8. Deterministic workstation & media control query (Step 21)
    workstation_res = check_deterministic_workstation_query(clean_text, context=context)
    if workstation_res:
        return workstation_res

    # 9. Deterministic remote clipboard & virtual input (Step 25 / 25.1)
    input_res = check_deterministic_input_query(clean_text, context=context)
    if input_res:
        return input_res

    # 10. Deterministic remote file fetch query (Step 22)
    file_fetch_res = check_deterministic_file_fetch_query(clean_text, context=context)
    if file_fetch_res:
        return file_fetch_res

    # 11. Deterministic process management, power states & web navigation (Step 23)
    proc_power_res = check_deterministic_process_and_power_query(clean_text, context=context)
    if proc_power_res:
        return proc_power_res

    # 12. Deterministic autonomous watchdog queries (Step 24)
    watchdog_res = check_deterministic_watchdog_query(clean_text, context=context)
    if watchdog_res:
        return watchdog_res

    # 13. Deterministic long-term memory & executive briefing queries (Step 26)
    memory_briefing_res = check_deterministic_memory_and_briefing_query(clean_text, context=context)
    if memory_briefing_res:
        return memory_briefing_res

    # 14. Deterministic remote shell & PowerShell command bridge (Step 27)
    shell_res = await check_deterministic_shell_query(clean_text, context=context)
    if shell_res:
        return shell_res

    # 15. Deterministic remote mouse & cursor control (Step 28)
    mouse_res = check_deterministic_mouse_query(clean_text, context=context)
    if mouse_res:
        return mouse_res

    # Fast check: bypass LLM tool router for conceptual questions and basic chat greetings
    if should_bypass_llm_tool_router(clean_text):
        return None

    if ollama_client is None:
        from orion.integrations.ollama import OllamaClient

        ollama = OllamaClient()
    else:
        ollama = ollama_client

    messages = [
        {"role": "system", "content": TOOL_ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": clean_text},
    ]

    routing_data = await ollama.generate_structured_json(messages, num_predict=256)
    if not routing_data or not isinstance(routing_data, dict):
        return None

    tool_name = routing_data.get("tool")
    unsupported = routing_data.get("unsupported", False)

    if unsupported:
        return {
            "handled": True,
            "reply": "I cannot perform that action. I only have access to safe system monitoring and controlled file operations (deletion, overwriting, and arbitrary commands are strictly prohibited).",
        }

    # Handle multi-step execution workflows (Step 7)
    if routing_data.get("is_workflow"):
        from orion.core.execution_engine import (
            ExecutionPlan,
            format_workflow_proposal,
            generate_execution_plan,
            simulate_and_validate_workflow,
        )

        steps_raw = routing_data.get("steps")
        plan = None
        if steps_raw and isinstance(steps_raw, list):
            try:
                plan = ExecutionPlan(goal=clean_text, steps=steps_raw)
            except Exception:
                plan = None

        if plan is None:
            plan, err = await generate_execution_plan(clean_text, client=ollama)
            if not plan:
                return {"handled": True, "reply": err or "Unable to construct a valid workflow plan."}

        valid, error_msg, validated_steps = simulate_and_validate_workflow(plan, registry=reg)
        if not valid:
            return {"handled": True, "reply": f"Cannot execute workflow: {error_msg}"}

        proposal_text = format_workflow_proposal(clean_text, validated_steps)
        return {
            "handled": True,
            "is_workflow_proposal": True,
            "goal": clean_text,
            "steps": validated_steps,
            "proposal_text": proposal_text,
        }

    if not tool_name or not isinstance(tool_name, str):
        return None

    # Check that tool exists in registry
    if not reg.is_registered(tool_name):
        return {
            "handled": True,
            "reply": f"The tool '{tool_name}' is not recognized or permitted.",
        }

    args = routing_data.get("arguments", {})
    if not isinstance(args, dict):
        args = {}

    tool_def = reg.get_tool(tool_name)
    if not tool_def:
        return None

    # Validate arguments against Pydantic schema
    try:
        validated_args = tool_def.args_schema.model_validate(args)
        clean_args = validated_args.model_dump()
    except ValidationError as err:
        return {
            "handled": True,
            "reply": f"Invalid arguments for {tool_name}: {err.errors()[0].get('msg')}",
        }

    # If write action: run pre-validations and return proposal for user confirmation
    if reg.is_write_action(tool_name):
        try:
            if tool_name == "create_directory":
                dest = validate_safe_path(clean_args["path"], must_exist=False)
                if dest.exists():
                    return {"handled": True, "reply": f"Cannot create directory: `{dest.name}` already exists."}
                if not dest.parent.is_dir():
                    return {"handled": True, "reply": "Cannot create directory: Parent directory does not exist."}
            elif tool_name == "create_text_file":
                if not is_representable_text(clean_args.get("content", "")):
                    return {"handled": True, "reply": "Cannot proceed: File content contains unprintable or binary characters that cannot be clearly displayed for review."}
                dest = validate_safe_path(clean_args["path"], must_exist=False)
                if dest.exists():
                    return {"handled": True, "reply": f"Cannot create file: `{dest.name}` already exists. Overwriting is strictly prohibited."}
                if not dest.parent.is_dir():
                    return {"handled": True, "reply": "Cannot create file: Parent directory does not exist."}
            elif tool_name in ("copy_file", "move_file"):
                src = validate_safe_path(clean_args["source"], must_exist=True)
                if not src.is_file():
                    return {"handled": True, "reply": "Source must be a regular file."}
                dest_alias = resolve_folder_alias(clean_args["destination"])
                dest_cand = Path(dest_alias)
                dest_target = dest_cand / src.name if dest_cand.is_dir() else dest_cand
                dest = validate_safe_path(dest_target, must_exist=False)
                if dest.exists():
                    return {"handled": True, "reply": f"Cannot proceed: Destination `{dest.name}` already exists. Overwriting is strictly prohibited."}
                if not dest.parent.is_dir():
                    return {"handled": True, "reply": "Destination parent directory does not exist."}
            elif tool_name == "rename_file":
                src = validate_safe_path(clean_args["source"], must_exist=True)
                if not src.is_file():
                    return {"handled": True, "reply": "Source must be a regular file."}
                new_name = clean_args["new_name"].strip().strip("'\"")
                if not new_name or "/" in new_name or "\\" in new_name or ".." in new_name:
                    return {"handled": True, "reply": "New filename must be a simple filename without path separators or directory traversal."}
                dest = validate_safe_path(src.parent / new_name, must_exist=False)
                if dest.exists():
                    return {"handled": True, "reply": f"Cannot rename: Destination `{dest.name}` already exists. Overwriting is strictly prohibited."}
            elif tool_name == "launch_application":
                raw_id = clean_args.get("app_id", "")
                resolved_id = resolve_app_alias(raw_id)
                if not resolved_id:
                    blocked, reason = is_app_blocked(raw_id)
                    if blocked:
                        return {"handled": True, "reply": f"Application '{raw_id}' is not in the approved application allowlist. {reason}"}
                    return {"handled": True, "reply": f"Application '{raw_id}' is not in the approved application allowlist. Use /apps to see approved applications."}
                clean_args["app_id"] = resolved_id
                is_avail, avail_msg, _ = is_app_available(resolved_id)
                if not is_avail:
                    app_obj = get_approved_apps().get(resolved_id)
                    disp = app_obj.display_name if app_obj else resolved_id
                    return {"handled": True, "reply": f"Cannot launch {disp}: Application is not currently installed or available on this system."}
            elif tool_name in (
                "focus_application_window",
                "minimize_application_window",
                "maximize_application_window",
                "restore_application_window",
            ):
                raw_id = clean_args.get("app_id", "")
                resolved_id = resolve_app_alias(raw_id)
                if not resolved_id:
                    blocked, reason = is_app_blocked(raw_id)
                    if blocked:
                        return {"handled": True, "reply": f"Application '{raw_id}' is not in the approved application allowlist. {reason}"}
                    return {"handled": True, "reply": f"Application '{raw_id}' is not in the approved application allowlist. Use /apps to see approved applications."}
                clean_args["app_id"] = resolved_id
        except (PermissionError, FileNotFoundError, NotADirectoryError, ValueError) as err:
            return {"handled": True, "reply": str(err)}

        # Propose action without executing
        proposal_text = format_action_proposal(tool_name, clean_args)
        return {
            "handled": True,
            "is_proposal": True,
            "action_type": tool_name,
            "arguments": clean_args,
            "proposal_text": proposal_text,
        }

    # Execute read-only tool
    if tool_name == "get_application_window_status":
        raw_id = clean_args.get("app_id", "")
        resolved = resolve_app_alias(raw_id)
        if not resolved:
            return {
                "handled": True,
                "reply": f"Application '{raw_id}' is not in the approved application allowlist.",
            }
        clean_args["app_id"] = resolved

    exec_result = reg.execute(tool_name, clean_args)
    reply_msg = format_tool_result(tool_name, exec_result)

    return {
        "handled": True,
        "tool": tool_name,
        "result": exec_result,
        "reply": reply_msg,
    }

