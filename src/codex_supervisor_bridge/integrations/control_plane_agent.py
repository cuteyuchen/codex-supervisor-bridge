"""Backend-neutral adapter for the existing Codex Control Plane integration."""

from .agent_backends import ControlPlaneAgentBackend, snapshot_from_payload

__all__ = ["ControlPlaneAgentBackend", "snapshot_from_payload"]
