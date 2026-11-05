"""Safe read-only tool registry and schema validation."""

import logging
from typing import Any, Callable
from pydantic import BaseModel, Field, ValidationError

from orion.tools.app_launcher import execute_composite_app_action, launch_application
from orion.tools.desktop_inspector import (
    focus_application_window,
    get_active_window,
    get_application_window_status,
    list_open_windows,
    maximize_application_window,
    minimize_application_window,
    restore_application_window,
)
from orion.tools.filesystem import find_files, get_file_info, list_files
from orion.tools.filesystem_actions import (
    copy_file,
    create_directory,
    create_text_file,
    move_file,
    rename_file,
)
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


class CreateDirectoryArgs(BaseModel):
    """Schema for directory creation."""

    path: str = Field(min_length=1, max_length=500, description="Path of new directory to create")


class CopyFileArgs(BaseModel):
    """Schema for copying a file."""

    source: str = Field(min_length=1, max_length=500, description="Path of source file to copy")
    destination: str = Field(min_length=1, max_length=500, description="Destination path or directory")


class MoveFileArgs(BaseModel):
    """Schema for moving a file."""

    source: str = Field(min_length=1, max_length=500, description="Path of source file to move")
    destination: str = Field(min_length=1, max_length=500, description="Destination path or directory")


class RenameFileArgs(BaseModel):
    """Schema for renaming a file."""

    source: str = Field(min_length=1, max_length=500, description="Path of file to rename")
    new_name: str = Field(min_length=1, max_length=255, description="New filename (name only, no slashes)")


class CreateTextFileArgs(BaseModel):
    """Schema for creating a text file."""

    path: str = Field(min_length=1, max_length=500, description="Path of new text file to create")
    content: str = Field(default="", max_length=100000, description="Text content to write")


class LaunchApplicationArgs(BaseModel):
    """Schema for launching an approved application."""

    app_id: str = Field(min_length=1, max_length=50, description="Approved application ID")


class AppWindowStatusArgs(BaseModel):
    """Schema for inspecting window status of an approved application."""

    app_id: str = Field(min_length=1, max_length=50, description="Approved application ID or friendly name")


class WindowManagementArgs(BaseModel):
    """Schema for controlled window management (focus/minimize/maximize/restore)."""

    app_id: str = Field(min_length=1, max_length=50, description="Approved application ID")
    window_token: str | None = Field(default=None, max_length=100, description="Opaque window selection token for multi-window apps")


class CompositeAppActionArgs(BaseModel):
    """Schema for composite multi-application or sequential actions."""

    command_type: str = Field(description="multi_launch or launch_and_window")
    app_ids: list[str] = Field(default_factory=list, description="Approved application IDs to launch")
    target_app_id: str | None = Field(default=None, description="Target app ID for sequential window action")
    window_action: str | None = Field(default=None, description="Window action: maximize, minimize, restore, focus")
    summary: str = Field(default="Application action", description="Human-readable summary")
    display_names: list[str] = Field(default_factory=list)
    display_name: str | None = Field(default=None)
    actions: list[dict[str, Any]] = Field(default_factory=list)


class ToolDefinition:
    """Registered tool container."""

    def __init__(
        self,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        func: Callable[..., dict[str, Any]],
        is_write_action: bool = False,
    ) -> None:
        self.name = name
        self.description = description
        self.args_schema = args_schema
        self.func = func
        self.is_write_action = is_write_action


class ToolRegistry:
    """Explicit registry of sanctioned read-only and confirmed write Windows tools."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        # Read-only tools
        self.register(
            name="get_battery_status",
            description="Retrieve battery level percentage, plugged-in status, and remaining time.",
            args_schema=EmptyArgs,
            func=get_battery_status,
            is_write_action=False,
        )
        self.register(
            name="get_system_resources",
            description="Retrieve current CPU, RAM, and disk utilization percentages.",
            args_schema=EmptyArgs,
            func=get_system_resources,
            is_write_action=False,
        )
        self.register(
            name="list_files",
            description="List file and directory names in an approved folder.",
            args_schema=ListFilesArgs,
            func=list_files,
            is_write_action=False,
        )
        self.register(
            name="find_files",
            description="Search for files by name matching a query inside an approved folder.",
            args_schema=FindFilesArgs,
            func=find_files,
            is_write_action=False,
        )
        self.register(
            name="get_file_info",
            description="Retrieve metadata (size, modified time, directory status) for an approved file.",
            args_schema=GetFileInfoArgs,
            func=get_file_info,
            is_write_action=False,
        )
        self.register(
            name="list_open_windows",
            description="List approved applications that currently have open desktop windows.",
            args_schema=EmptyArgs,
            func=list_open_windows,
            is_write_action=False,
        )
        self.register(
            name="get_active_window",
            description="Identify which approved application is currently active in the foreground.",
            args_schema=EmptyArgs,
            func=get_active_window,
            is_write_action=False,
        )
        self.register(
            name="get_application_window_status",
            description="Check window count, state (Normal/Minimized/Maximized), and active status for an approved application.",
            args_schema=AppWindowStatusArgs,
            func=get_application_window_status,
            is_write_action=False,
        )

        # Controlled write action tools (require user confirmation before execution)
        self.register(
            name="create_directory",
            description="Create a new directory inside an approved directory.",
            args_schema=CreateDirectoryArgs,
            func=create_directory,
            is_write_action=True,
        )
        self.register(
            name="copy_file",
            description="Copy a file to another location within approved directories without overwriting.",
            args_schema=CopyFileArgs,
            func=copy_file,
            is_write_action=True,
        )
        self.register(
            name="move_file",
            description="Move a file to another location within approved directories without overwriting.",
            args_schema=MoveFileArgs,
            func=move_file,
            is_write_action=True,
        )
        self.register(
            name="rename_file",
            description="Rename a file within its parent directory without overwriting.",
            args_schema=RenameFileArgs,
            func=rename_file,
            is_write_action=True,
        )
        self.register(
            name="create_text_file",
            description="Create a new text file with specified content without overwriting.",
            args_schema=CreateTextFileArgs,
            func=create_text_file,
            is_write_action=True,
        )
        self.register(
            name="launch_application",
            description="Launch an approved Windows application on the owner's laptop. Requires user confirmation.",
            args_schema=LaunchApplicationArgs,
            func=launch_application,
            is_write_action=True,
        )
        # Controlled window management tools (Step 11 — require user confirmation before execution)
        self.register(
            name="focus_application_window",
            description="Bring an approved application window to the foreground. Requires user confirmation.",
            args_schema=WindowManagementArgs,
            func=focus_application_window,
            is_write_action=True,
        )
        self.register(
            name="minimize_application_window",
            description="Minimize an approved application window. Requires user confirmation.",
            args_schema=WindowManagementArgs,
            func=minimize_application_window,
            is_write_action=True,
        )
        self.register(
            name="maximize_application_window",
            description="Maximize an approved application window. Requires user confirmation.",
            args_schema=WindowManagementArgs,
            func=maximize_application_window,
            is_write_action=True,
        )
        self.register(
            name="restore_application_window",
            description="Restore a minimized/maximized approved application window to Normal state. Requires user confirmation.",
            args_schema=WindowManagementArgs,
            func=restore_application_window,
            is_write_action=True,
        )
        self.register(
            name="composite_app_action",
            description="Execute multiple application launches or sequential window actions. Requires confirmation.",
            args_schema=CompositeAppActionArgs,
            func=execute_composite_app_action,
            is_write_action=True,
        )

    def register(
        self,
        name: str,
        description: str,
        args_schema: type[BaseModel],
        func: Callable[..., dict[str, Any]],
        is_write_action: bool = False,
    ) -> None:
        """Register an explicitly approved function."""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            args_schema=args_schema,
            func=func,
            is_write_action=is_write_action,
        )

    def is_registered(self, name: str) -> bool:
        return name in self._tools

    def is_write_action(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool.is_write_action if tool else False

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
            if isinstance(result, dict) and result.get("success") is False:
                return {
                    "success": False,
                    "tool": name,
                    "error": result.get("error", "Tool execution failed."),
                    "data": result,
                }
            return {"success": True, "tool": name, "data": result}
        except ValidationError as err:
            logger.warning("Validation error for tool %s: %s", name, err)
            return {
                "success": False,
                "tool": name,
                "error": f"Invalid arguments for tool '{name}': {err.errors()[0].get('msg')}",
            }
        except (PermissionError, FileNotFoundError, NotADirectoryError, FileExistsError, ValueError) as err:
            logger.warning("Filesystem access error for tool %s: %s", name, err)
            return {"success": False, "tool": name, "error": str(err)}
        except Exception as err:
            logger.error("Error executing tool %s: %s", name, err)
            return {"success": False, "tool": name, "error": f"Tool execution failed: {type(err).__name__}"}


# Global registry instance
default_tool_registry = ToolRegistry()
