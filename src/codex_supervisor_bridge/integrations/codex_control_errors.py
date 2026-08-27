from __future__ import annotations


class CodexControlError(Exception):
    """Base class for bounded, model-readable Codex control-plane failures."""


class CodexControlUnavailableError(CodexControlError):
    def __init__(self) -> None:
        super().__init__("Codex Control Plane MCP is unavailable")


class CodexContractError(CodexControlError):
    def __init__(self, message: str) -> None:
        super().__init__(f"CODEX_CONTRACT_ERROR: {message}")


class CodexToolError(CodexControlError):
    def __init__(
        self,
        tool: str,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.tool = tool
        self.code = code
        self.retryable = retryable
        super().__init__(
            f"CODEX_TOOL_ERROR tool={tool} code={code} retryable={str(retryable).lower()}: {message}"
        )


class CodexPlanGateError(CodexControlError):
    def __init__(self, message: str) -> None:
        super().__init__(f"CODEX_PLAN_GATE: {message}")


class CodexCompensationError(CodexControlError):
    """A stale local decision was detected after a remote write and stop compensation failed."""

    def __init__(self, stale_context: str) -> None:
        super().__init__(
            "CODEX_COMPENSATION_REQUIRED: "
            f"{stale_context}; remote action may still be active because compensation interrupt failed"
        )
