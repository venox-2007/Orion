"""Safe read-only tool registry and schema validation."""

import logging
from typing import Any, Callable
from pydantic import BaseModel, Field, ValidationError

from orion.tools.filesystem import find_files, get_file_info, list_files
from orion.tools.system import get_battery_status, get_system_resources

logger = logging.getLogger("orion.tools")


class EmptyArgs(BaseModel):
    """Schema for tools requiring no arguments."""


class ListFilesArgs(BaseModel):
    """Schema for directory listing."""

    directory: str = Field(min_length=1, max_length=500, description="Approved directory path to list")


class FindFilesArgs(BaseModel):
    """Schema for filename searching."""

    query: str = Field(min_length=1, max_length=100, description="Filename substring to search for")
    directory: str = Field(min_length=1, max_length=500, description="Approved directory path to search within")


class GetFileInfoArgs(BaseModel):
    """Schema for file metadata inspection."""

    path: str = Field(min_length=1, max_length=500, description="Approved path of file to inspect")


class ToolDefinition:
    """Registered tool container."""

    def __init__(
        self,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        func: Callable[..., dict[str, Any]],
    ) -> None:
        self.name = name
        self.description = description
        self.args_schema = args_schema
        self.func = func


class ToolRegistry:
    """Explicit registry of sanctioned read-only Windows tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register(
            name="get_battery_status",
            description="Retrieve battery level percentage, plugged-in status, and remaining time.",
            args_schema=EmptyArgs,
            func=get_battery_status,
        )
        self.register(
            name="get_system_resources",
            description="Retrieve current CPU, RAM, and disk utilization percentages.",
            args_schema=EmptyArgs,
            func=get_system_resources,
        )
        self.register(
            name="list_files",
            description="List file and directory names in an approved folder.",
            args_schema=ListFilesArgs,
            func=list_files,
        )
        self.register(
            name="find_files",
            description="Search for files by name matching a query inside an approved folder.",
            args_schema=FindFilesArgs,
            func=find_files,
        )
        self.register(
            name="get_file_info",
            description="Retrieve metadata (size, modified time, directory status) for an approved file.",
            args_schema=GetFileInfoArgs,
            func=get_file_info,
        )

    def register(
        self,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        func: Callable[..., dict[str, Any]],
    ) -> None:
        """Register an explicitly approved read-only function."""
        self._tools[name] = ToolDefinition(name, description, args_schema, func)

    def is_registered(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        """Return schema descriptions for LLM tool selection."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.args_schema.model_json_schema(),
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Validate arguments against Pydantic schema and execute tool.

        Never runs arbitrary code, shell commands, or unapproved functions.
        """
        tool = self.get_tool(name)
        if not tool:
            logger.warning("Attempted execution of unrecognized tool: %s", name)
            return {
                "success": False,
                "error": f"Tool '{name}' is not recognized or permitted.",
            }

        try:
            validated_args = tool.args_schema.model_validate(arguments or {})
            args_dict = validated_args.model_dump()
            result = tool.func(**args_dict)
            logger.info("Executed tool %s successfully", name)
            return {"success": True, "tool": name, "data": result}
        except ValidationError as err:
            logger.warning("Validation error for tool %s: %s", name, err)
            return {
                "success": False,
                "tool": name,
                "error": f"Invalid arguments for tool '{name}': {err.errors()[0].get('msg')}",
            }
        except (PermissionError, FileNotFoundError, NotADirectoryError) as err:
            logger.warning("Filesystem access error for tool %s: %s", name, err)
            return {"success": False, "tool": name, "error": str(err)}
        except Exception as err:
            logger.error("Error executing tool %s: %s", name, err)
            return {"success": False, "tool": name, "error": f"Tool execution failed: {type(err).__name__}"}


# Global registry instance
default_tool_registry = ToolRegistry()
