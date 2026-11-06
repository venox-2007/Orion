"""Natural-language tool routing and presentation layer."""

from __future__ import annotations

from pathlib import Path
import re
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
        return (
            "**System Resources:**\n"
            f"• **CPU:** {data.get('cpu_percent')}%\n"
            f"• **RAM:** {data.get('ram_used_gb')} GB / {data.get('ram_total_gb')} GB ({data.get('ram_percent')}%)\n"
            f"• **Disk:** {data.get('disk_used_gb')} GB / {data.get('disk_total_gb')} GB ({data.get('disk_percent')}%)"
        )

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
        return data.get("message", "Window status retrieved.")

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

