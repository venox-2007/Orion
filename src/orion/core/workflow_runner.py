"""Sequential workflow runner and command chain sequencer for ORION (Step 30)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

logger = logging.getLogger("orion.core.workflow_runner")

MAX_WORKFLOW_STEPS = 6


def split_chained_commands(text: str) -> list[str]:
    """Split text by chained delimiters ('then', '&&', ';') into sequential steps."""
    clean = text.strip()
    if not clean:
        return []

    # Delimiters: ' then ', '&&', or ';'
    pattern = r"\s+then\s+|\s*&&\s*|;\s*"
    raw_steps = re.split(pattern, clean, flags=re.IGNORECASE)
    steps = [s.strip() for s in raw_steps if s and s.strip()]

    # Guard: Max 6 steps per chain to prevent runaway execution
    return steps[:MAX_WORKFLOW_STEPS]


async def execute_workflow(
    steps: list[str],
    router_fn: Callable[..., Any],
    context: dict[str, Any] | None = None,
    delay_sec: float = 0.5,
) -> dict[str, Any]:
    """Execute a list of command steps sequentially with safe delay pacing."""
    if not steps:
        return {
            "handled": True,
            "success": False,
            "completed": 0,
            "total": 0,
            "reply": "⚠️ No workflow steps provided.",
        }

    total_steps = len(steps)
    completed_steps = 0
    step_results: list[dict[str, Any]] = []

    wf_context = dict(context or {})
    wf_context["_in_workflow"] = True

    for idx, step_cmd in enumerate(steps, 1):
        if idx > 1 and delay_sec > 0:
            await asyncio.sleep(delay_sec)

        # Check if step is an application launch command
        app_target = None
        m_app = re.match(r"^(?:open|launch|start|run)\s+(.+)$", step_cmd, re.IGNORECASE)
        if m_app:
            raw_app = m_app.group(1).strip()
            cand_app = re.sub(r"^(?:the\s+|my\s+)", "", raw_app, flags=re.IGNORECASE).strip()
            cand_app = re.sub(r"(?i)\s+(?:app|application|program)[\.!\?]?$", "", cand_app).strip(" .!?\"'")
            if not (cand_app.startswith(("http://", "https://")) or ":" in cand_app or cand_app in ("watchdog",)):
                app_target = cand_app

        try:
            res = await router_fn(step_cmd, context=wf_context)
            if res is None:
                success = False
                sub_reply = "Command not recognized or unsupported."
            elif not res.get("handled"):
                success = False
                sub_reply = res.get("reply") or "Command not handled."
            elif res.get("is_proposal"):
                # Execute action proposal in automated workflow
                action_type = res.get("action_type")
                args = res.get("arguments", {})
                if action_type == "launch_application" and "app_id" in args:
                    from orion.tools.app_launcher import launch_application

                    exec_res = launch_application(args["app_id"])
                    if exec_res.get("success"):
                        success = True
                        disp = exec_res.get("display_name", args["app_id"])
                        sub_reply = f"🚀 Launch requested for {disp}."
                    else:
                        success = False
                        sub_reply = f"⚠️ {exec_res.get('error', 'Failed to launch application.')}"
                else:
                    success = False
                    sub_reply = f"⚠️ Action proposal requires confirmation: {action_type}"
            else:
                sub_reply = res.get("reply") or "Executed successfully."
                # Check for explicit failure markers
                if sub_reply.startswith(("⚠️", "❌", "Cannot execute")):
                    success = False
                else:
                    success = True

            # If application launch succeeded, poll for window readiness
            if success and app_target:
                from orion.tools import input_control

                poll_interval = 0.5 if delay_sec >= 0.5 else max(0.01, delay_sec)
                for _ in range(6):
                    await asyncio.sleep(poll_interval)
                    try:
                        if input_control.focus_window_by_name(app_target):
                            break
                    except Exception as poll_err:
                        logger.debug("Window focus polling error for '%s': %s", app_target, poll_err)

        except Exception as e:
            logger.warning("Workflow step %d ('%s') raised exception: %s", idx, step_cmd, e)
            success = False
            sub_reply = f"Error: {e}"

        step_results.append({
            "step": step_cmd,
            "success": success,
            "reply": sub_reply,
        })

        if success:
            completed_steps += 1
        else:
            # Abort subsequent steps on failure
            break

    # Format aggregate Markdown response
    all_success = completed_steps == total_steps
    status_header = (
        f"⚡ *Workflow Completed ({completed_steps}/{total_steps} steps):*"
        if all_success
        else f"⚡ *Workflow Stopped ({completed_steps}/{total_steps} steps):*"
    )

    lines = [status_header]
    for idx, item in enumerate(step_results, 1):
        icon = "✅" if item["success"] else "❌"
        lines.append(f"{idx}. {icon} *{item['step']}*")
        sub_msg = item["reply"].strip()
        if sub_msg and sub_msg not in ("Executed successfully.", "Done."):
            # Indent output under step
            for sub_line in sub_msg.splitlines():
                if sub_line.strip():
                    lines.append(f"   {sub_line}")

    final_reply = "\n".join(lines)

    return {
        "handled": True,
        "is_workflow": True,
        "success": all_success,
        "completed": completed_steps,
        "total": total_steps,
        "step_results": step_results,
        "reply": final_reply,
    }
