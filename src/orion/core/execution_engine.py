"""Controlled multi-step filesystem execution engine for ORION (Step 7).

Accepts natural-language goals, produces validated multi-step execution plans
(max 3 operations), simulates step dependencies without modifying the disk,
requires single explicit user approval, and executes sequentially with strict
safety and failure-halt guarantees.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
from pydantic import BaseModel, Field, ValidationError

from orion.config.settings import get_settings
from orion.tools.registry import ToolRegistry, default_tool_registry
from orion.tools.security import prettify_path, resolve_folder_alias, validate_safe_path

if TYPE_CHECKING:
    from orion.integrations.ollama import OllamaClient
    from orion.memory.database import ConversationMemory

APPROVED_WRITE_TOOLS = {
    "create_directory",
    "copy_file",
    "move_file",
    "rename_file",
    "create_text_file",
}

# Concurrency lock to prevent conflicting concurrent executions on filesystem
_workflow_execution_lock = asyncio.Lock()

EXECUTION_PLANNER_SYSTEM_PROMPT = (
    "You are ORION's multi-step execution planner. "
    "Convert the user's natural language goal into a structured multi-step execution plan.\n\n"
    "Available tools (use ONLY these 5 approved write tools):\n"
    "1. create_directory: Create a new directory. Arguments: {\"path\": \"<alias_or_path>\"}\n"
    "2. copy_file: Copy a file. Arguments: {\"source\": \"<alias_or_path>\", \"destination\": \"<alias_or_path>\"}\n"
    "3. move_file: Move a file. Arguments: {\"source\": \"<alias_or_path>\", \"destination\": \"<alias_or_path>\"}\n"
    "4. rename_file: Rename a file in place. Arguments: {\"source\": \"<alias_or_path>\", \"new_name\": \"<new_filename_only>\"}\n"
    "5. create_text_file: Create a text file. Arguments: {\"path\": \"<alias_or_path>\", \"content\": \"<text>\"}\n\n"
    "Strict Constraints:\n"
    "- Maximum 3 operations per workflow. Never produce more than 3 steps.\n"
    "- Use folder aliases ('Downloads', 'Documents', 'Desktop') for standard folders.\n"
    "- For rename_file, new_name must be a simple filename without slashes or directories.\n"
    "- If the user asks for unsupported actions (file deletion, file removal, overwriting existing files, "
    "running shell/Python/code, launching apps, browser automation), return: {\"unsupported\": true, \"reason\": \"<reason>\"}\n"
    "- If the request is ambiguous (e.g., 'move the pdf files' without specifying exact filename), "
    "return: {\"ambiguous\": true, \"reason\": \"Please specify the exact filename to move.\"}\n"
    "- If the goal is not a file modification workflow, return: {\"is_workflow\": false}\n"
    "- Return strict JSON matching the schema."
)


class ExecutionStep(BaseModel):
    """A single executable tool operation within a workflow."""

    step_id: int = Field(ge=1, le=3, description="Sequential step index (1-3)")
    tool: str = Field(description="Name of approved write tool")
    arguments: dict[str, Any] = Field(description="Strict arguments for the tool")
    description: str = Field(min_length=1, max_length=200, description="Short human-readable step description")


class ExecutionPlan(BaseModel):
    """Structured multi-step execution plan (1 to 3 operations)."""

    goal: str = Field(min_length=1, max_length=500, description="Original goal")
    steps: list[ExecutionStep] = Field(min_length=1, max_length=3, description="Ordered steps (max 3)")
    summary: str = Field(default="", max_length=300, description="Brief summary of proposed workflow")


async def generate_execution_plan(
    goal: str,
    client: OllamaClient | None = None,
) -> tuple[ExecutionPlan | None, str | None]:
    """Generate a structured execution plan from a natural-language goal.

    Returns (plan, None) on success, or (None, rejection_reason) if rejected/unsupported/ambiguous.
    """
    clean_goal = goal.strip()
    if not clean_goal:
        return None, "Empty goal provided."

    if client is None:
        from orion.integrations.ollama import OllamaClient

        ollama = OllamaClient()
    else:
        ollama = client

    messages = [
        {"role": "system", "content": EXECUTION_PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": clean_goal},
    ]

    raw_data = await ollama.generate_structured_json(messages, num_predict=512)
    if not raw_data or not isinstance(raw_data, dict):
        return None, "Unable to generate a structured execution plan."

    if raw_data.get("unsupported"):
        reason = raw_data.get("reason", "Requested action is not supported.")
        return None, f"Cannot execute workflow: {reason}"

    if raw_data.get("ambiguous"):
        reason = raw_data.get("reason", "The request contains ambiguous file or path references.")
        return None, f"Cannot execute workflow: Ambiguous request — {reason}"

    if raw_data.get("is_workflow") is False:
        return None, None

    steps_raw = raw_data.get("steps")
    if not steps_raw or not isinstance(steps_raw, list):
        return None, "No executable steps were proposed."

    if len(steps_raw) > 3:
        return None, "Plan exceeds maximum allowed limit of 3 operations per workflow."

    try:
        plan = ExecutionPlan.model_validate(raw_data)
    except ValidationError as err:
        return None, f"Invalid plan structure: {err.errors()[0].get('msg')}"

    # Validate tool names against approved write tools
    for step in plan.steps:
        if step.tool not in APPROVED_WRITE_TOOLS:
            return None, f"Plan contains unauthorized tool '{step.tool}'. Only approved write tools are permitted."

    return plan, None


def simulate_and_validate_workflow(
    plan: ExecutionPlan,
    registry: ToolRegistry | None = None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Validate initial filesystem state and simulate step dependencies without modifying the disk.

    Returns (True, "", validated_steps_list) if the plan is completely safe and valid,
    or (False, error_message, []) if any step is invalid, impossible, or unsafe.
    """
    reg = registry or default_tool_registry
    settings = get_settings()
    allowed_dirs = settings.get_allowed_directories()

    if not allowed_dirs:
        return False, "Filesystem access is disabled. No directories configured in ORION_ALLOWED_DIRECTORIES.", []

    if not plan.steps or len(plan.steps) > 3:
        return False, "Execution plan must contain between 1 and 3 operations.", []

    # Simulated filesystem tracking
    simulated_created_dirs: set[Path] = set()
    simulated_created_files: set[Path] = set()
    simulated_moved_or_renamed_sources: set[Path] = set()

    validated_steps: list[dict[str, Any]] = []

    for step in plan.steps:
        tool_def = reg.get_tool(step.tool)
        if not tool_def or not tool_def.is_write_action:
            return False, f"Step {step.step_id} uses unapproved tool '{step.tool}'.", []

        # 1. Validate arguments using tool's Pydantic schema
        try:
            val_args = tool_def.args_schema.model_validate(step.arguments)
            clean_args = val_args.model_dump()
        except ValidationError as err:
            return False, f"Step {step.step_id} argument validation error: {err.errors()[0].get('msg')}", []

        # 2. Simulate step behavior
        try:
            if step.tool == "create_directory":
                raw_path = clean_args["path"]
                dest = validate_safe_path(raw_path, must_exist=False)

                # Check if it already exists or was created earlier in the simulation
                if dest.exists() or dest in simulated_created_dirs:
                    return False, f"Step {step.step_id}: Directory `{dest.name}` already exists. Overwriting is strictly prohibited.", []

                # Parent must exist on disk or in simulated created dirs
                if not dest.parent.is_dir() and dest.parent not in simulated_created_dirs:
                    return False, f"Step {step.step_id}: Parent directory `{dest.parent.name}` does not exist.", []

                simulated_created_dirs.add(dest)

            elif step.tool == "create_text_file":
                raw_path = clean_args["path"]
                content = clean_args.get("content", "")
                if not is_representable_text(content):
                    return False, f"Step {step.step_id}: File content contains unprintable or binary characters that cannot be clearly displayed for review.", []
                dest = validate_safe_path(raw_path, must_exist=False)

                if dest.exists() or dest in simulated_created_files:
                    return False, f"Step {step.step_id}: File `{dest.name}` already exists. Overwriting is strictly prohibited.", []

                if not dest.parent.is_dir() and dest.parent not in simulated_created_dirs:
                    return False, f"Step {step.step_id}: Parent directory does not exist.", []

                simulated_created_files.add(dest)

            elif step.tool in ("copy_file", "move_file"):
                src_raw = clean_args["source"]
                dst_raw = clean_args["destination"]

                # Source resolution: either exists on disk or was created in an earlier step
                src_alias = resolve_folder_alias(src_raw)
                src_candidate = Path(src_alias)
                if src_candidate in simulated_created_files:
                    src_path = validate_safe_path(src_candidate, must_exist=False)
                else:
                    src_path = validate_safe_path(src_raw, must_exist=True)

                if src_path in simulated_moved_or_renamed_sources:
                    return False, f"Step {step.step_id}: Source file `{src_path.name}` was already moved in an earlier step.", []

                if not src_path.is_file() and src_path not in simulated_created_files:
                    return False, f"Step {step.step_id}: Source must be a regular file: `{src_path.name}`.", []

                # Destination resolution
                dst_alias = resolve_folder_alias(dst_raw)
                dst_candidate = Path(dst_alias)

                # Check if destination refers to a directory (on disk or simulated)
                if (dst_candidate.exists() and dst_candidate.is_dir()) or dst_candidate in simulated_created_dirs:
                    final_dest = dst_candidate / src_path.name
                else:
                    final_dest = dst_candidate

                dest_path = validate_safe_path(final_dest, must_exist=False)

                if dest_path.exists() or dest_path in simulated_created_files:
                    return False, f"Step {step.step_id}: Destination `{dest_path.name}` already exists. Overwriting is strictly prohibited.", []

                if not dest_path.parent.is_dir() and dest_path.parent not in simulated_created_dirs:
                    return False, f"Step {step.step_id}: Destination parent directory does not exist.", []

                if step.tool == "move_file":
                    simulated_moved_or_renamed_sources.add(src_path)

                simulated_created_files.add(dest_path)

            elif step.tool == "rename_file":
                src_raw = clean_args["source"]
                new_name = clean_args["new_name"].strip().strip("'\"")

                if not new_name or "/" in new_name or "\\" in new_name or ".." in new_name:
                    return False, f"Step {step.step_id}: new_name must be a simple filename without path separators or directory traversal.", []

                src_alias = resolve_folder_alias(src_raw)
                src_candidate = Path(src_alias)
                if src_candidate in simulated_created_files:
                    src_path = validate_safe_path(src_candidate, must_exist=False)
                else:
                    src_path = validate_safe_path(src_raw, must_exist=True)

                if src_path in simulated_moved_or_renamed_sources:
                    return False, f"Step {step.step_id}: Source file `{src_path.name}` was already moved in an earlier step.", []

                dest_path = validate_safe_path(src_path.parent / new_name, must_exist=False)

                if dest_path.exists() or dest_path in simulated_created_files:
                    return False, f"Step {step.step_id}: Destination `{dest_path.name}` already exists. Overwriting is strictly prohibited.", []

                simulated_moved_or_renamed_sources.add(src_path)
                simulated_created_files.add(dest_path)

        except (PermissionError, FileNotFoundError, NotADirectoryError, ValueError) as err:
            return False, f"Step {step.step_id} validation failed: {err}", []

        validated_steps.append(
            {
                "step_id": step.step_id,
                "tool": step.tool,
                "arguments": clean_args,
                "description": step.description,
                "status": "planned",
                "result": None,
            }
        )

    return True, "", validated_steps


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


def format_workflow_proposal(goal: str, steps: list[dict[str, Any]]) -> str:
    """Format the complete proposed multi-step workflow for Telegram user approval."""
    lines = [
        "ORION — Proposed Task\n",
    ]

    for i, step in enumerate(steps, start=1):
        tool = step.get("tool", "")
        args = step.get("arguments", {})

        if tool == "create_directory":
            path_str = args.get("path", "")
            pretty = prettify_path(path_str)
            name = Path(pretty).name or pretty
            parent = str(Path(pretty).parent).replace("\\", "/")
            if parent == ".":
                parent = prettify_path(Path(path_str).parent)
            lines.append(f"{i}. Create folder: `{name}`\n   Location: `{parent}`\n")
        elif tool == "create_text_file":
            path_str = args.get("path", "")
            pretty = prettify_path(path_str)
            name = Path(pretty).name or pretty
            parent = str(Path(pretty).parent).replace("\\", "/")
            if parent == ".":
                parent = prettify_path(Path(path_str).parent)
            content = args.get("content", "")
            preview_block = format_content_preview(content)
            lines.append(
                f"{i}. Create text file: `{name}`\n"
                f"   Location: `{parent}`\n"
                f"   {preview_block}\n"
            )
        elif tool in ("copy_file", "move_file"):
            action_name = "Copy file" if tool == "copy_file" else "Move file"
            src_pretty = prettify_path(args.get("source", ""))
            dst_pretty = prettify_path(args.get("destination", ""))
            src_name = Path(src_pretty).name
            src_parent = str(Path(src_pretty).parent).replace("\\", "/")
            lines.append(
                f"{i}. {action_name}: `{src_name}`\n"
                f"   From: `{src_parent}`\n"
                f"   Destination: `{dst_pretty}`\n"
            )
        elif tool == "rename_file":
            src_pretty = prettify_path(args.get("source", ""))
            src_name = Path(src_pretty).name
            src_parent = str(Path(src_pretty).parent).replace("\\", "/")
            new_name = args.get("new_name", "")
            lines.append(
                f"{i}. Rename file: `{src_name}`\n"
                f"   Location: `{src_parent}`\n"
                f"   New Name: `{new_name}`\n"
            )
        else:
            desc = step.get("description", "").strip()
            lines.append(f"{i}. {desc}\n")

    count = len(steps)
    lines.append(f"{count} action{'s' if count > 1 else ''} require approval.")
    lines.append("Nothing has been changed yet.")
    return "\n".join(lines)


async def execute_workflow(
    workflow_id: str,
    steps: list[dict[str, Any]],
    user_id: int,
    memory: ConversationMemory,
    registry: ToolRegistry | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute steps sequentially with strict safety, per-step persistence, and instant halt on failure.

    Non-transactional: Never claims full automatic rollback.
    Reports completed, failed, and skipped steps.
    """
    reg = registry or default_tool_registry
    start_time = time.time()

    completed_descriptions: list[str] = []
    failed_step_desc: str | None = None
    failure_error: str | None = None

    async with _workflow_execution_lock:
        for idx, step in enumerate(steps):
            # Check timeout
            if time.time() - start_time > timeout_seconds:
                failure_error = "Workflow exceeded execution time limit."
                step["status"] = "failed"
                step["result"] = {"error": failure_error}
                # Mark remaining steps skipped
                for rem in steps[idx + 1:]:
                    rem["status"] = "skipped"
                memory.finish_execution_workflow(
                    workflow_id,
                    status="failed",
                    steps=steps,
                    error_summary=failure_error,
                )
                break

            tool_name = step.get("tool", "")
            args = step.get("arguments", {})
            desc = step.get("description", f"Step {idx + 1}")

            # Re-validate filesystem state immediately prior to step execution
            exec_result = reg.execute(tool_name, args)

            if not exec_result.get("success"):
                failure_error = exec_result.get("error", "Tool execution failed")
                failed_step_desc = desc
                step["status"] = "failed"
                step["result"] = exec_result

                # Mark all subsequent steps as skipped
                for rem in steps[idx + 1:]:
                    rem["status"] = "skipped"

                # Persist failed outcome immediately
                memory.finish_execution_workflow(
                    workflow_id,
                    status="failed",
                    steps=steps,
                    error_summary=f"Failed at step {idx + 1}: {failure_error}",
                )
                break

            # Step succeeded
            step["status"] = "completed"
            step["result"] = exec_result
            completed_descriptions.append(desc)

            # Persist progress after each step
            memory.update_execution_workflow_step(workflow_id, steps=steps)

        # Check overall result
        all_completed = all(s.get("status") == "completed" for s in steps)
        if all_completed:
            memory.finish_execution_workflow(workflow_id, status="completed", steps=steps)
            # Friendly summary message
            summary_msg = format_workflow_completion_message(steps)
            return {
                "success": True,
                "workflow_id": workflow_id,
                "message": summary_msg,
                "steps": steps,
            }

        # Partial failure report
        partial_msg = format_workflow_failure_message(steps, failed_step_desc, failure_error)
        return {
            "success": False,
            "workflow_id": workflow_id,
            "message": partial_msg,
            "steps": steps,
            "error": failure_error,
        }


def format_workflow_completion_message(steps: list[dict[str, Any]]) -> str:
    """Format clean, user-friendly completion message."""
    action_summaries = []
    for s in steps:
        tool = s.get("tool", "")
        args = s.get("arguments", {})
        if tool == "create_directory":
            name = Path(args.get("path", "")).name
            action_summaries.append(f"Created {name}")
        elif tool == "create_text_file":
            name = Path(args.get("path", "")).name
            action_summaries.append(f"Created {name}")
        elif tool == "copy_file":
            src = Path(args.get("source", "")).name
            dst = Path(args.get("destination", "")).name
            action_summaries.append(f"Copied {src} to {dst}")
        elif tool == "move_file":
            src = Path(args.get("source", "")).name
            dst = Path(args.get("destination", "")).name
            action_summaries.append(f"Moved {src} to {dst}")
        elif tool == "rename_file":
            src = Path(args.get("source", "")).name
            new_name = args.get("new_name", "")
            action_summaries.append(f"Renamed {src} to {new_name}")
        else:
            action_summaries.append(s.get("description", "Action completed"))

    joined = " and ".join(action_summaries)
    return f"Done!\n{joined}."


def format_workflow_failure_message(
    steps: list[dict[str, Any]],
    failed_desc: str | None,
    error_msg: str | None,
) -> str:
    """Format clear partial failure report without stack traces."""
    completed = [s for s in steps if s.get("status") == "completed"]
    failed = [s for s in steps if s.get("status") == "failed"]
    skipped = [s for s in steps if s.get("status") == "skipped"]

    lines = []
    if completed:
        comp_names = []
        for s in completed:
            tool = s.get("tool", "")
            args = s.get("arguments", {})
            name = Path(args.get("path") or args.get("source") or "").name
            comp_names.append(f"Created/processed {name}" if name else s.get("description", ""))
        lines.append(f"{', '.join(comp_names)}, but encountered an error.")
    else:
        lines.append("Operation failed before changes were made.")

    if failed:
        f_step = failed[0]
        tool = f_step.get("tool", "")
        args = f_step.get("arguments", {})
        target = Path(args.get("destination") or args.get("path") or args.get("source") or "").name
        clean_err = error_msg or "Unknown error"
        lines.append(f"Couldn't complete {tool.replace('_', ' ')} for `{target}`: {clean_err}")

    if skipped:
        lines.append(f"{len(skipped)} remaining action(s) were skipped. No further actions were performed.")

    return "\n".join(lines)
