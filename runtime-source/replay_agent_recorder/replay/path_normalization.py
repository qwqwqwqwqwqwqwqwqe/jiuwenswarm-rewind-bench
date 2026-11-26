from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence
from urllib.parse import unquote

from .errors import WorkspaceSafetyError


LOGICAL_WORKSPACE_PREFIX = "replay://workspace/"
_ESCAPED_CONTROL_BOUNDARY_CHARS = frozenset("nrtfv0")
_UNICODE_URI_BOUNDARY_CHARS = frozenset("，。；：！？、（）【】《》“”‘’")
_LOGICAL_URI_BOUNDARY_CHARS = frozenset("\"'`()[]{}<>,;|&") | _UNICODE_URI_BOUNDARY_CHARS
_ANSI_BOUNDARY_RE = re.compile(
    "(?:\x1b|\\\\x1b|\\\\u001b|\\\\033)\\[[0-9;?]*[ -/]*[@-~]$"
)


@dataclass(frozen=True)
class PathMapping:
    workspace_id: str
    real_root: Path
    logical_root: str
    aliases: tuple[str, ...] = ()
    denormalize_root: str | Path | None = None


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def normalize_string(text: str, mappings: Sequence[PathMapping]) -> str:
    """Replace real workspace paths in text with logical workspace URIs."""

    return _PathCanonicalizer(mappings).normalize_string(text)


def denormalize_string(text: str, mappings: Sequence[PathMapping]) -> str:
    """Replace logical workspace URIs in text with current real roots."""

    return _PathCanonicalizer(mappings).denormalize_string(text)


def normalize_workspace_paths(value: Any, mappings: Sequence[PathMapping]) -> Any:
    if not mappings:
        return value
    if isinstance(value, str):
        return normalize_string(value, mappings)
    if isinstance(value, Path):
        return normalize_string(str(value), mappings)
    if isinstance(value, list):
        return [normalize_workspace_paths(item, mappings) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_workspace_paths(item, mappings) for item in value)
    if isinstance(value, dict):
        return {
            _normalize_key(key, mappings): normalize_workspace_paths(item, mappings)
            for key, item in value.items()
        }
    return value


def denormalize_workspace_paths(value: Any, mappings: Sequence[PathMapping]) -> Any:
    if not mappings:
        return value
    if isinstance(value, str):
        return denormalize_string(value, mappings)
    if isinstance(value, Path):
        return denormalize_string(str(value), mappings)
    if isinstance(value, list):
        return [denormalize_workspace_paths(item, mappings) for item in value]
    if isinstance(value, tuple):
        return tuple(denormalize_workspace_paths(item, mappings) for item in value)
    if isinstance(value, dict):
        return {
            _denormalize_key(key, mappings): denormalize_workspace_paths(item, mappings)
            for key, item in value.items()
        }
    return value


def _normalize_key(key: Any, mappings: Sequence[PathMapping]) -> Any:
    if isinstance(key, str):
        return normalize_string(key, mappings)
    if isinstance(key, Path):
        return normalize_string(str(key), mappings)
    return key


def _denormalize_key(key: Any, mappings: Sequence[PathMapping]) -> Any:
    if isinstance(key, str):
        return denormalize_string(key, mappings)
    if isinstance(key, Path):
        return denormalize_string(str(key), mappings)
    return key


def _mappings_by_longest_root(mappings: Sequence[PathMapping]) -> list[PathMapping]:
    return sorted(
        mappings,
        key=lambda mapping: max(
            [len(str(mapping.real_root.resolve())), *(len(alias.rstrip("/")) for alias in mapping.aliases)]
        ),
        reverse=True,
    )


class _PathCanonicalizer:
    def __init__(self, mappings: Sequence[PathMapping]) -> None:
        self.mappings = list(mappings)

    def normalize_string(self, text: str) -> str:
        normalized = text
        for mapping in _mappings_by_longest_root(self.mappings):
            real_root = str(mapping.real_root.resolve())
            logical_root = mapping.logical_root.rstrip("/")
            normalized = self._replace_real_root(normalized, real_root, logical_root)
            for alias in sorted(mapping.aliases, key=len, reverse=True):
                alias_root = alias.rstrip("/") or "/"
                normalized = self._replace_alias_root(normalized, alias_root, logical_root)
        return normalized

    def denormalize_string(self, text: str) -> str:
        by_id = {mapping.workspace_id: mapping for mapping in self.mappings}
        result: list[str] = []
        cursor = 0
        while True:
            start = text.find(LOGICAL_WORKSPACE_PREFIX, cursor)
            if start == -1:
                result.append(text[cursor:])
                break

            result.append(text[cursor:start])
            end = _logical_uri_end(text, start)
            uri = text[start:end]
            result.append(_denormalize_uri(uri, by_id))
            cursor = end
        return "".join(result)

    def _replace_alias_root(self, text: str, alias_root: str, logical_root: str) -> str:
        result: list[str] = []
        cursor = 0
        while True:
            start = text.find(alias_root, cursor)
            if start == -1:
                result.append(text[cursor:])
                break
            end = start + len(alias_root)
            if not self._has_path_start_boundary(text, start) or not self._has_path_boundary(text, end):
                result.append(text[cursor:end])
                cursor = end
                continue

            candidate_end = self._path_candidate_end(text, start)
            candidate = text[start:candidate_end]
            result.append(text[cursor:start])
            if candidate == alias_root:
                result.append(logical_root)
            else:
                raw_suffix = candidate[len(alias_root) :]
                trailing_slash = raw_suffix.endswith("/")
                raw_relative = raw_suffix.lstrip("/")
                relative = _normalize_alias_relative(raw_relative)
                if relative:
                    result.append(f"{logical_root}/{relative}")
                else:
                    result.append(logical_root + ("/" if trailing_slash else ""))
            cursor = candidate_end
        return "".join(result)

    def _replace_real_root(self, text: str, real_root: str, logical_root: str) -> str:
        resolved_root = Path(real_root).resolve()
        result: list[str] = []
        cursor = 0
        while True:
            start = text.find(real_root, cursor)
            if start == -1:
                result.append(text[cursor:])
                break
            end = start + len(real_root)
            if not self._has_path_start_boundary(text, start) or not self._has_path_boundary(
                text, end
            ):
                result.append(text[cursor:end])
                cursor = end
                continue

            candidate_end = self._path_candidate_end(text, start)
            resolved_candidate = Path(text[start:candidate_end]).resolve()
            if is_relative_to(resolved_candidate, resolved_root):
                relative = resolved_candidate.relative_to(resolved_root).as_posix()
                result.append(text[cursor:start])
                if relative == ".":
                    result.append(logical_root + ("/" if text[start:candidate_end].endswith("/") else ""))
                else:
                    result.append(f"{logical_root}/{relative}")
                cursor = candidate_end
            else:
                result.append(text[cursor:candidate_end])
                cursor = candidate_end
        return "".join(result)

    def _has_path_start_boundary(self, text: str, index: int) -> bool:
        if index == 0:
            return True
        if self._has_escaped_control_boundary(text, index):
            return True
        if self._has_ansi_boundary(text, index):
            return True
        return not self._is_path_continuation_char(text[index - 1])

    def _has_path_boundary(self, text: str, index: int) -> bool:
        return (
            index == len(text)
            or text[index] == "/"
            or self._is_path_terminator(text[index])
            or self._starts_escaped_terminator(text, index)
        )

    def _path_candidate_end(self, text: str, start: int) -> int:
        end = start
        while end < len(text):
            if self._is_path_terminator(text[end]) or self._starts_escaped_terminator(
                text, end
            ):
                break
            end += 1
        while end > start and text[end - 1] in ".,;:)]}\"'":
            end -= 1
        return end

    def _has_escaped_control_boundary(self, text: str, index: int) -> bool:
        return (
            index >= 2
            and text[index - 2] == "\\"
            and text[index - 1] in _ESCAPED_CONTROL_BOUNDARY_CHARS
        )

    def _has_ansi_boundary(self, text: str, index: int) -> bool:
        prefix = text[max(0, index - 64) : index]
        return _ANSI_BOUNDARY_RE.search(prefix) is not None

    def _starts_escaped_terminator(self, text: str, index: int) -> bool:
        return _starts_escaped_terminator(text, index)

    def _is_path_terminator(self, char: str) -> bool:
        return (char in "\x00\x1b" or char.isspace() or char in "\"'`()[]{}<>,;:|&"
                or char in _UNICODE_URI_BOUNDARY_CHARS)

    def _is_path_continuation_char(self, char: str) -> bool:
        return char.isalnum() or char in "/._-~"


def _logical_uri_end(text: str, start: int) -> int:
    end = start
    while (
        end < len(text)
        and not text[end].isspace()
        and text[end] != "`"
        and text[end] not in _LOGICAL_URI_BOUNDARY_CHARS
        and not _starts_escaped_terminator(text, end)
    ):
        end += 1
    while end > start and text[end - 1] in ",;:)]}\"'":
        end -= 1
    return end


def _starts_escaped_terminator(text: str, index: int) -> bool:
    if index >= len(text) or text[index] != "\\":
        return False
    end = index
    while end < len(text) and text[end] == "\\":
        end += 1
    if end >= len(text):
        return True
    if end - index >= 2:
        return True
    # Tool arguments/output may themselves contain JSON strings. A quoted
    # terminator can therefore have 1, 3, 7, ... leading backslashes. Keep the
    # entire escape sequence outside the URI instead of appending it to the ID.
    if text[end] in _ESCAPED_CONTROL_BOUNDARY_CHARS or text[end] in "\"'`":
        return True
    return (
        text.startswith("x1b[", end)
        or text.startswith("u001b[", end)
        or text.startswith("033[", end)
    )


def _denormalize_uri(uri: str, mappings_by_id: dict[str, PathMapping]) -> str:
    suffix = uri[len(LOGICAL_WORKSPACE_PREFIX) :]
    if not suffix:
        raise WorkspaceSafetyError("Logical workspace path is missing workspace id.")

    workspace_id, separator, relative = suffix.partition("/")
    workspace_id = unquote(workspace_id)
    if not workspace_id:
        raise WorkspaceSafetyError("Logical workspace path has empty workspace id.")
    mapping = mappings_by_id.get(workspace_id)
    if mapping is None:
        if "..." in workspace_id:
            return uri
        raise WorkspaceSafetyError(f"Unknown logical workspace id: {workspace_id!r}.")
    display_root = _mapping_display_root(mapping)

    if separator == "":
        return display_root

    relative = unquote(relative)
    if relative == "":
        return display_root + ("/" if uri.endswith("/") and not display_root.endswith("/") else "")
    trailing_slash = relative.endswith("/")
    relative = relative.rstrip("/")
    if relative == "":
        return display_root + ("/" if trailing_slash and not display_root.endswith("/") else "")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise WorkspaceSafetyError(f"Unsafe logical workspace path: {uri!r}.")
    pure_relative = PurePosixPath(relative)
    if pure_relative.is_absolute():
        raise WorkspaceSafetyError(f"Unsafe logical workspace path: {uri!r}.")

    denormalized = _join_display_root(display_root, pure_relative.parts)
    if trailing_slash and not denormalized.endswith("/"):
        return f"{denormalized}/"
    return denormalized


def _normalize_alias_relative(relative: str) -> str:
    parts = relative.split("/")
    if ".." in parts:
        return relative
    normalized = "/".join(part for part in parts if part not in ("", "."))
    if normalized and relative.endswith("/"):
        return f"{normalized}/"
    return normalized


def _mapping_display_root(mapping: PathMapping) -> str:
    if mapping.denormalize_root is not None:
        root = str(mapping.denormalize_root)
        if root == "/":
            return root
        return root.rstrip("/")
    return str(mapping.real_root.resolve())


def _join_display_root(root: str, parts: tuple[str, ...]) -> str:
    if root == "/":
        return "/" + "/".join(parts)
    return root.rstrip("/") + "/" + "/".join(parts)
