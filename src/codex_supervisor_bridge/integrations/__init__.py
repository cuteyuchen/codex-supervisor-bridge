"""External system adapters used behind the supervisor MCP boundary."""

from .agent_backends import ControlPlaneAgentBackend, LocalCodexBridgeAgentBackend

__all__ = ["ControlPlaneAgentBackend", "LocalCodexBridgeAgentBackend"]
