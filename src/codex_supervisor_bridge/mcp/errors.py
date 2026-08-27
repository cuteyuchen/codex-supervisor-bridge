from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from codex_supervisor_bridge.memory.errors import MemoryErrorBase

P = ParamSpec("P")
R = TypeVar("R")


def expose_memory_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Convert anticipated memory-domain failures into MCP ToolError.

    MCPServer intentionally redacts unexpected exception messages. Only our
    bounded, intentionally user/model-readable MemoryErrorBase hierarchy is
    exposed. Unexpected exceptions still follow the SDK's generic-error path
    so implementation details, paths, and tracebacks do not leak to the MCP
    caller.
    """

    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except MemoryErrorBase as exc:
            raise ToolError(str(exc)) from exc

    return wrapped


def tool_argument_error(message: str) -> ToolError:
    """Create an anticipated, model-readable tool-boundary validation error."""
    return ToolError(message)
