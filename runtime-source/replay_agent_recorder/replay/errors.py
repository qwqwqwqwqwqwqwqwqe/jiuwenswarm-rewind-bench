class ReplayError(RuntimeError):
    """Base class for replay framework errors."""


class ReplayNotInstalledError(ReplayError):
    """Raised when a replay context is used before install()."""


class ReplayMissError(ReplayError):
    """Raised when strict replay cannot find a matching historical record."""


class AmbiguousReplayError(ReplayError):
    """Raised when replay matching cannot uniquely identify a historical record."""


class InvalidBreakpointError(ReplayError):
    """Raised when a breakpoint record uid is missing or does not point to an LLM record."""


class ToolSerializationError(ReplayError):
    """Raised when a tool input or output cannot be safely recorded for replay."""


class LlmCodecError(ReplayError):
    """Raised when an LLM boundary output cannot be serialized or reconstructed."""


class UnsupportedOverrideInputError(ReplayError):
    """Raised when an override shape is unsupported for the recorded LLM codec."""


class UnsupportedTraceSchemaError(ReplayError):
    """Raised when a trace uses a schema this version cannot replay."""


class ReplayedToolError(ReplayError):
    """Raised when replaying a tool call that historically failed."""

    def __init__(
        self,
        *,
        tool_name: str | None,
        record_uid: str | None,
        original_type: str | None,
        message: str | None,
        original_repr: str | None,
    ) -> None:
        self.tool_name = tool_name
        self.record_uid = record_uid
        self.original_type = original_type
        self.message = message
        self.original_repr = original_repr
        super().__init__(
            "Replayed tool error"
            f" tool_name={tool_name!r}"
            f" record_uid={record_uid!r}"
            f" original_type={original_type!r}"
            f" message={message!r}"
        )


class UnsupportedStreamingError(ReplayError):
    """Raised when stream=True is used in the first-stage demo."""


class WorkspaceError(ReplayError):
    """Raised when workspace state cannot be managed for replay."""


class WorkspaceNotFoundError(WorkspaceError):
    """Raised when a required workspace cannot be found."""


class WorkspaceSafetyError(WorkspaceError):
    """Raised when workspace management would be unsafe."""


class WorkspaceGitError(WorkspaceError):
    """Raised when Git workspace operations fail."""

    def __init__(
        self,
        message: str,
        *,
        args: "Sequence[str] | None" = None,
        stderr: str | None = None,
    ) -> None:
        details = [message]
        if args:
            details.append(f"command={' '.join(args)!r}")
        if stderr:
            details.append(f"stderr={stderr.strip()!r}")
        super().__init__("; ".join(details))


class WorkspaceRestoreError(WorkspaceError):
    """Raised when a workspace restore cannot materialize a target checkpoint."""

    def __init__(
        self,
        message: str,
        *,
        path: "object | None" = None,
        relative_path: str | None = None,
        errno_value: int | None = None,
        commit: str | None = None,
        target_tree: str | None = None,
        is_transient: bool = False,
        holder_processes: "list[dict[str, object]] | None" = None,
        attempted_actions: "list[str] | None" = None,
    ) -> None:
        self.path = path
        self.relative_path = relative_path
        self.errno = errno_value
        self.commit = commit
        self.target_tree = target_tree
        self.is_transient = is_transient
        self.holder_processes = holder_processes or []
        self.attempted_actions = attempted_actions or []
        details = [message]
        if relative_path:
            details.append(f"path={relative_path!r}")
        elif path is not None:
            details.append(f"path={str(path)!r}")
        if errno_value is not None:
            details.append(f"errno={errno_value}")
        if commit:
            details.append(f"commit={commit}")
        if target_tree:
            details.append(f"target_tree={target_tree}")
        if is_transient:
            details.append("transient=True")
        if self.attempted_actions:
            details.append(f"actions={self.attempted_actions!r}")
        super().__init__("; ".join(details))


class FilesystemReplayConflictError(WorkspaceError):
    """Raised when replay would overwrite filesystem state that has diverged."""


class WorkspacePreparationError(ReplayError):
    """Workspace preparation failed."""


class WorkspacePreparationSafetyError(WorkspacePreparationError):
    """Workspace path failed safety validation."""


# Backward-compatible aliases for older callers.
SandboxError = WorkspacePreparationError
SandboxSafetyError = WorkspacePreparationSafetyError
