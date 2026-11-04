"""Natural-language tool routing and presentation layer."""

from typing import Any
from orion.integrations.ollama import OllamaClient
from orion.tools.registry import ToolRegistry, default_tool_registry

TOOL_ROUTER_SYSTEM_PROMPT = (
    "You are ORION's tool selection router. "
    "Analyze the user's message and determine if it requires a read-only Windows tool.\n"
    "Available tools:\n"
    "- get_battery_status: Get battery percentage and charging status. No arguments.\n"
    "- get_system_resources: Get CPU, RAM, and disk utilization. No arguments.\n"
    '- list_files: List files in an approved directory. Arguments: {"directory": "<path>"}\n'
    '- find_files: Search files by filename in an approved directory. Arguments: {"query": "<name>", "directory": "<path>"}\n'
    '- get_file_info: Get metadata (size, timestamp) of an approved file. Arguments: {"path": "<path>"}\n\n'
    "Rules:\n"
    '1. If the user asks a general conversation question, return: {"tool": null}\n'
    '2. If the user asks for unsupported actions (e.g., launching apps, modifying files, executing code, browser tools), return: {"tool": null, "unsupported": true}\n'
    '3. If the user asks for one of the 5 tools above, return: {"tool": "<tool_name>", "arguments": {...}}\n'
    "4. Return strict JSON only. Do not invent tools."
)


def format_tool_result(tool_name: str, result: dict[str, Any]) -> str:
    """Format tool execution results into concise, human-readable text without raw JSON."""
    if not result.get("success"):
        error_msg = result.get("error", "Unknown error")
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

    return "Action completed successfully."


async def route_and_execute_tool(
    user_text: str,
    ollama_client: OllamaClient | None = None,
    registry: ToolRegistry | None = None,
) -> dict[str, Any] | None:
    """Analyze user message for tool invocation, validate, and execute at most one read-only tool.

    Returns dict with {"handled": True, "reply": str} if a tool or unsupported action was routed,
    or None if the message should proceed through standard conversation chat.
    """
    clean_text = user_text.strip()
    if not clean_text:
        return None

    ollama = ollama_client or OllamaClient()
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
            "reply": "I cannot perform that action. I only have access to a small set of read-only system monitoring and approved file search tools.",
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

    # Execute exactly one read-only tool
    exec_result = reg.execute(tool_name, args)
    reply_msg = format_tool_result(tool_name, exec_result)

    return {
        "handled": True,
        "tool": tool_name,
        "result": exec_result,
        "reply": reply_msg,
    }
