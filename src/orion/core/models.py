"""Pydantic data models for structured task planning."""

from typing import List
from pydantic import BaseModel, Field, field_validator


class TaskStep(BaseModel):
    """A single sequential step within an action plan."""

    id: int = Field(ge=1, le=8, description="Ordered step index")
    description: str = Field(min_length=1, max_length=300, description="Step description")
    status: str = Field(default="proposed", description="Step status: proposed, pending, or planned")


class TaskPlan(BaseModel):
    """Structured action plan containing up to 8 ordered steps."""

    goal: str = Field(min_length=1, max_length=500, description="Original objective")
    steps: List[TaskStep] = Field(min_length=1, max_length=8, description="Ordered action steps")
    status: str = Field(default="planned", description="Overall plan status")

    @field_validator("steps")
    @classmethod
    def validate_step_count(cls, steps: List[TaskStep]) -> List[TaskStep]:
        if not (1 <= len(steps) <= 8):
            raise ValueError("Task plan must contain between 1 and 8 steps.")
        return steps
