from __future__ import annotations


class DevSpaceError(RuntimeError):
    """Expected DevSpace integration failure safe to classify at the Supervisor boundary."""


class DevSpaceUnavailableError(DevSpaceError):
    pass


class DevSpaceCapabilityError(DevSpaceError):
    def __init__(self, missing_tools: list[str]) -> None:
        self.missing_tools = sorted(set(missing_tools))
        super().__init__(
            "Local workspace backend is missing required capabilities: "
            + ", ".join(self.missing_tools)
        )


class DevSpaceToolError(DevSpaceError):
    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        super().__init__(f"Local workspace operation failed ({tool}): {message}")


class DevSpaceProtocolError(DevSpaceError):
    pass
