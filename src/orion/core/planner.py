"""Lightweight structured task-planning module."""

from pydantic import ValidationError
from orion.config.settings import get_settings
from orion.core.models import TaskPlan
from orion.integrations.ollama import OllamaClient

PLANNER_SYSTEM_PROMPT = (
    "You are ORION's structured action planner. "
    "Your purpose is to break down a user's natural-language goal into an ordered, realistic action plan.\n"
    "Rules:\n"
    "1. Decompose the goal into 1 to 8 logical, sequential steps.\n"
    "2. Each step must have an integer 'id' (starting from 1), a concise 'description', and 'status'='proposed'.\n"
    "3. You are only outlining planned proposals; you DO NOT execute actions.\n"
    "4. Respond strictly with a JSON object matching this schema:\n"
    "{\n"
    '  "goal": "<goal description>",\n'
    '  "steps": [\n'
    '    {"id": 1, "description": "<step description>", "status": "proposed"}\n'
    "  ]\n"
    "}\n"
    "Do not include any text or markdown before or after the JSON."
)


async def generate_task_plan(
    goal: str,
    client: OllamaClient | None = None,
) -> TaskPlan | None:
    """Generate a structured action plan for a natural-language goal.

    Returns a validated TaskPlan or None if input is invalid or generation fails.
    """
    clean_goal = goal.strip()
    settings = get_settings()
    max_len = getattr(settings, "max_goal_length", 500)

    if not clean_goal or len(clean_goal) > max_len:
        return None

    ollama = client or OllamaClient()
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": f"Create an action plan for: {clean_goal}"},
    ]

    raw_json = await ollama.generate_structured_json(messages)
    if not raw_json:
        return None

    try:
        # If the model didn't echo goal, backfill it
        if not raw_json.get("goal"):
            raw_json["goal"] = clean_goal

        return TaskPlan.model_validate(raw_json)
    except (ValidationError, Exception):
        return None
