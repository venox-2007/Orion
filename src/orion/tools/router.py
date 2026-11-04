"""Natural-language tool routing and presentation layer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from pydantic import ValidationError

if TYPE_CHECKING:
    from orion.integrations.ollama import OllamaClient

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
    '- get_file_info: Get metadata of an approved file. Arguments: {"path": "<alias_or_path>"}\n\n'
    "Available controlled file modification tools (require explicit user confirmation):\n"
    '- create_directory: Create a new directory. Arguments: {"path": "<alias_or_path>"}\n'
    '- copy_file: Copy a file to another location. Arguments: {"source": "<alias_or_path>", "destination": "<alias_or_path>"}\n'
    '- move_file: Move a file to another location. Arguments: {"source": "<alias_or_path>", "destination": "<alias_or_path>"}\n'
    '- rename_file: Rename a file in place. Arguments: {"source": "<alias_or_path>", "new_name": "<new_filename_only>"}\n'
    '- create_text_file: Create a new text file. Arguments: {"path": "<alias_or_path>", "content": "<text_content>"}\n\n'
    "Rules for directory and file paths:\n"
    "- For standard folders, use folder aliases: 'Downloads', 'Documents', or 'Desktop' (e.g. {'directory': 'Downloads'}).\n"
    "- Never invent usernames or placeholder paths like 'C:\\\\Users\\\\[username]\\\\' or '%USERPROFILE%'.\n"
    "- For new_name in rename_file, provide ONLY the filename (e.g. 'notes_backup.txt'), never a path.\n\n"
    "Routing Rules:\n"
    '1. If the user asks a general conversation question, return: {"tool": null}\n'
    '2. If the user asks for unsupported actions (e.g., deleting/removing files, overwriting existing files, running shell/scripts/code, launching apps, browser automation), return: {"tool": null, "unsupported": true}\n'
    '3. If the user asks for a single tool above, return: {"tool": "<tool_name>", "arguments": {...}}\n'
    '4. If the user asks for a multi-step workflow (2 or 3 file operations), return: {"is_workflow": true, "steps": [{"step_id": 1, "tool": "<name>", "arguments": {...}, "description": "..."}, ...]}\n'
    "5. Return strict JSON only. Never exceed 3 steps. Do not invent tools."
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
    return "⚠️ **Action Confirmation Required**\nPlease confirm or cancel this operation."


def format_tool_result(tool_name: str, result: dict[str, Any]) -> str:
    """Format tool execution results into concise, human-readable text without raw JSON."""
    if not result.get("success"):
        error_msg = result.get("error", "Unknown error")
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

    return "Action completed successfully."


async def route_and_execute_tool(
    user_text: str,
    ollama_client: OllamaClient | None = None,
    registry: ToolRegistry | None = None,
) -> dict[str, Any] | None:
    """Analyze user message for tool invocation, validate, and propose or execute at most one tool.

    For read-only tools: executes immediately and returns reply text.
    For write action tools: validates and returns a pending action proposal requiring user confirmation.
    Returns None if message should proceed through standard conversation chat.
    """
    clean_text = user_text.strip()
    if not clean_text:
        return None

    if ollama_client is None:
        from orion.integrations.ollama import OllamaClient

        ollama = OllamaClient()
    else:
        ollama = ollama_client
    reg = registry or default_tool_registry

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
    exec_result = reg.execute(tool_name, clean_args)
    reply_msg = format_tool_result(tool_name, exec_result)

    return {
        "handled": True,
        "tool": tool_name,
        "result": exec_result,
        "reply": reply_msg,
    }

