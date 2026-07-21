"""Structured exception types for TA_main2main_workflow.

Replaces bare ``except Exception`` and string-based error propagation
(``UpgradeFailed = "UpgradeFailed"``) with typed exceptions that carry
context about what failed and whether it can be retried.
"""

from __future__ import annotations


class TAWorkflowError(Exception):
    """Base exception for all workflow errors."""


# ── Step-level errors ──────────────────────────────────────────────────────────

class StepError(TAWorkflowError):
    """A pipeline step failed and cannot be recovered automatically.

    Attributes:
        step_name: Name of the step that failed (e.g. "merge", "build").
        reason: Human-readable description of what went wrong.
    """

    def __init__(self, step_name: str, reason: str = "") -> None:
        self.step_name = step_name
        self.reason = reason
        super().__init__(f"[{step_name}] {reason}" if reason else f"[{step_name}] failed")


class RetryableError(TAWorkflowError):
    """A transient error that may succeed on retry.

    Attributes:
        step_name: Name of the step that failed.
        reason: Human-readable description.
        max_retries: Suggested maximum number of retries.
    """

    def __init__(self, step_name: str, reason: str = "", max_retries: int = 3) -> None:
        self.step_name = step_name
        self.reason = reason
        self.max_retries = max_retries
        super().__init__(f"[{step_name}] {reason} (retryable, max {max_retries})")


# ── Domain-specific errors ─────────────────────────────────────────────────────

class ConfigError(TAWorkflowError):
    """Configuration is missing or invalid."""


class GitError(StepError):
    """A git operation failed."""

    def __init__(self, operation: str, reason: str = "") -> None:
        super().__init__(step_name=f"git:{operation}", reason=reason)


class MergeConflictError(GitError):
    """Merge conflicts could not be resolved after max retries."""

    def __init__(self, conflict_files: list[str] | None = None) -> None:
        self.conflict_files = conflict_files or []
        super().__init__(
            operation="merge",
            reason=f"{len(self.conflict_files)} file(s) still conflicted: "
            f"{', '.join(self.conflict_files[:5])}",
        )


class BuildError(StepError):
    """Build (compilation) failed."""

    def __init__(self, reason: str = "", log_file: str = "") -> None:
        self.log_file = log_file
        super().__init__(step_name="build", reason=reason)


class LLVMBuildError(BuildError):
    """LLVM build specifically failed."""

    def __init__(self, reason: str = "", log_file: str = "") -> None:
        super().__init__(reason=reason, log_file=log_file)
        self.step_name = "llvm-build"


class TestFailureError(StepError):
    """Tests did not pass."""

    def __init__(self, reason: str = "", failed_count: int = 0, error_count: int = 0) -> None:
        self.failed_count = failed_count
        self.error_count = error_count
        super().__init__(
            step_name="test",
            reason=reason or f"{failed_count} failed, {error_count} errors",
        )


class AIBackendError(RetryableError):
    """AI backend (opencode/claude) call failed — retryable."""

    def __init__(self, reason: str = "", max_retries: int = 3) -> None:
        super().__init__(step_name="ai", reason=reason, max_retries=max_retries)


class AIStaleTimeoutError(AIBackendError):
    """AI backend produced no output for too long."""

    def __init__(self, stale_seconds: int = 1200) -> None:
        super().__init__(
            reason=f"no output for {stale_seconds}s",
            max_retries=3,
        )


class PushError(StepError):
    """Push or PR creation failed."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(step_name="push", reason=reason)


class IRPatchError(StepError):
    """IR compatibility patch generation or application failed."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(step_name="ir-patch", reason=reason)
