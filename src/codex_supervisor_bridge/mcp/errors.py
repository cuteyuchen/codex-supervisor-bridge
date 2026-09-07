from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

from codex_supervisor_bridge.integrations.codex_control_errors import CodexControlError
from codex_supervisor_bridge.integrations.devspace_errors import DevSpaceError
from codex_supervisor_bridge.integrations.kandev_errors import KandevError
from codex_supervisor_bridge.integrations.local_codex_bridge_errors import (
    LocalCodexBridgeError,
)
from codex_supervisor_bridge.memory.codex_runtime import (
    CodexRuntimeAffinityError,
    CodexRuntimeCircuitOpenError,
)
from codex_supervisor_bridge.memory.errors import MemoryErrorBase
from codex_supervisor_bridge.supervisor.agent_execution import (
    AgentCompensationRequiredError,
    AgentPlanGateError,
    AgentStaleContextError,
)

P = ParamSpec("P")
R = TypeVar("R")


def _integration_message(exc: Exception) -> str:
    """Return a bounded, provider-safe message for expected integration errors.

    Provider responses may contain local paths, tool arguments, request payloads,
    or authentication material.  Those details are useful in diagnostics but
    must never become part of the normal MCP tool result.
    """

    if isinstance(exc, DevSpaceError):
        if exc.__class__.__name__ == "DevSpaceUnavailableError":
            return "Local workspace is unavailable. Check local workspace health or repair it."
        if exc.__class__.__name__ == "DevSpaceCapabilityError":
            return "Local workspace needs repair before this operation can continue."
        if exc.__class__.__name__ == "DevSpaceProtocolError":
            return "Local workspace returned an invalid response."
        return "Local workspace operation failed."
    if isinstance(exc, LocalCodexBridgeError):
        if exc.__class__.__name__ == "LocalCodexBridgeUnavailableError":
            return "Codex is unavailable. Check Codex health or repair the local connection."
        if exc.__class__.__name__ == "LocalCodexBridgeCapabilityError":
            return "Codex needs repair before this operation can continue."
        if exc.__class__.__name__ == "LocalCodexBridgeProtocolError":
            return "Codex returned an invalid response."
        return "Codex operation failed."
    if isinstance(exc, AgentStaleContextError):
        return "STALE_CONTEXT: re-read canonical task state before retrying."
    if isinstance(exc, AgentCompensationRequiredError):
        return "Codex runtime requires reconciliation before continuing."
    if isinstance(exc, AgentPlanGateError):
        return str(exc)
    if isinstance(exc, CodexRuntimeAffinityError):
        return "Codex runtime requires reconciliation before continuing."
    if isinstance(exc, CodexRuntimeCircuitOpenError):
        return "CODEX_RUNTIME_CIRCUIT_OPEN: explicit runtime recovery is required."
    return str(exc)


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


def expose_integration_errors(
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Expose only anticipated cross-system domain failures to the model."""

    @wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (
            MemoryErrorBase,
            KandevError,
            CodexControlError,
            DevSpaceError,
            LocalCodexBridgeError,
            AgentPlanGateError,
            CodexRuntimeAffinityError,
            CodexRuntimeCircuitOpenError,
        ) as exc:
            raise ToolError(_integration_message(exc)) from exc

    return wrapped


def tool_argument_error(message: str) -> ToolError:
    """Create an anticipated, model-readable tool-boundary validation error."""
    return ToolError(message)
