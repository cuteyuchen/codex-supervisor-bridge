from __future__ import annotations


class LocalCodexBridgeError(RuntimeError):
    """Expected Local-Codex-Bridge integration failure."""


class LocalCodexBridgeUnavailableError(LocalCodexBridgeError):
    pass


class LocalCodexBridgeCapabilityError(LocalCodexBridgeError):
    def __init__(self, missing_tools: list[str]) -> None:
        self.missing_tools = sorted(set(missing_tools))
        super().__init__("Local Codex Bridge is missing required capabilities")


class LocalCodexBridgeToolError(LocalCodexBridgeError):
    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        self.message = message
        super().__init__("Local Codex Bridge operation failed")


class LocalCodexBridgeProtocolError(LocalCodexBridgeError):
    pass


class LocalCodexBridgeUnknownOutcomeError(LocalCodexBridgeError):
    """A mutating request may have been accepted despite an acknowledgement timeout."""

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__("Local Codex Bridge mutating request outcome is unknown")
