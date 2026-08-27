from __future__ import annotations


class KandevError(RuntimeError):
    """Base error for Kandev adapter failures."""


class KandevToolError(KandevError):
    def __init__(self, tool: str, message: str) -> None:
        super().__init__(f"Kandev tool {tool} failed: {message}")
        self.tool = tool
        self.message = message


class KandevProtocolError(KandevError):
    """Raised when Kandev returns a response shape the bridge cannot safely interpret."""


class KandevCapabilityError(KandevError):
    def __init__(self, missing_tools: list[str]) -> None:
        message = "Kandev external MCP is missing required tools: " + ", ".join(missing_tools)
        super().__init__(message)
        self.missing_tools = missing_tools
