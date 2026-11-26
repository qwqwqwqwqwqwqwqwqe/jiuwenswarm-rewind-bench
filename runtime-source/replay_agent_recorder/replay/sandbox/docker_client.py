# replay/sandbox/docker_client.py
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from .types import SandboxError


@dataclass(frozen=True)
class DockerResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class DockerError(SandboxError):
    """Raised when a docker CLI invocation fails unexpectedly."""


def make_runner(
    binary: str = "docker", sudo: bool = False
) -> Callable[[Sequence[str]], DockerResult]:
    prefix: tuple[str, ...] = (("sudo", "-n", binary) if sudo else (binary,))

    def run(args: Sequence[str]) -> DockerResult:
        try:
            completed = subprocess.run(
                (*prefix, *args), text=True, capture_output=True
            )
        except FileNotFoundError as exc:
            raise DockerError(f"{binary} binary not found on PATH") from exc
        return DockerResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return run


def default_runner(args: Sequence[str]) -> DockerResult:
    """Backwards-compatible docker runner (kept: existing tests import it)."""
    return make_runner()(args)


class DockerClient:
    """Container CLI wrapper. `binary` may be docker or podman: every verb
    used here is spelled the same in both."""

    def __init__(
        self,
        runner: Callable[[Sequence[str]], DockerResult] | None = None,
        binary: str = "docker",
        sudo: bool = False,
    ) -> None:
        self._binary = binary
        self._sudo = sudo
        self._runner = runner or make_runner(binary, sudo)

    @property
    def binary(self) -> str:
        return self._binary

    def _run(self, args: Sequence[str], check: bool = True) -> DockerResult:
        result = self._runner(tuple(args))
        if check and result.returncode != 0:
            raise DockerError(
                f"{self._binary} {' '.join(args)} failed "
                f"({result.returncode}): {result.stderr.strip()}"
            )
        return result

    def run_detached(
        self,
        *,
        image: str,
        name: str,
        mounts: Sequence[tuple[str, str]],
        labels: Mapping[str, str],
        workdir: str,
        user: str | None,
        env: Mapping[str, str] | None = None,
        add_hosts: Sequence[str] = (),
    ) -> str:
        args: list[str] = ["run", "-d", "--name", name]
        for key, value in labels.items():
            args.extend(["--label", f"{key}={value}"])
        if user is not None:
            args.extend(["--user", user])
        for entry in add_hosts:
            args.extend(["--add-host", entry])
        for key, value in (env or {}).items():
            args.extend(["-e", f"{key}={value}"])
        for source, target in mounts:
            args.extend(["-v", f"{source}:{target}"])
        args.extend(["-w", workdir, image, "tail", "-f", "/dev/null"])
        return self._run(args).stdout.strip()

    def exec_(
        self, name: str, command: str, workdir: str = "/workspace"
    ) -> DockerResult:
        return self._run(
            ["exec", "-w", workdir, name, "sh", "-lc", command], check=False
        )

    def commit(self, name: str, image_tag: str) -> str:
        return self._run(["commit", name, image_tag]).stdout.strip()

    def tag(self, src: str, dst: str) -> None:
        self._run(["tag", src, dst])

    def remove_container(self, name: str) -> None:
        self._run(["rm", "-f", name], check=False)

    def remove_image(self, tag: str) -> None:
        self._run(["rmi", "-f", tag], check=False)

    def container_running(self, name: str) -> bool:
        result = self._run(
            ["inspect", "-f", "{{.State.Running}}", name], check=False
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def start_container(self, name: str) -> bool:
        return self._run(["start", name], check=False).returncode == 0

    def exec_detached(
        self, name: str, command: str, workdir: str = "/workspace"
    ) -> bool:
        result = self._run(
            ["exec", "-d", "-w", workdir, name, "sh", "-lc", command], check=False
        )
        return result.returncode == 0

    def image_exists(self, tag: str) -> bool:
        return self._run(["image", "inspect", tag], check=False).returncode == 0

    def image_layers(self, tag: str) -> int | None:
        """Layer count of an image, or None when the inspect failed or
        answered garbage. Missing image and daemon hiccup are
        indistinguishable here — callers treat None conservatively (skip
        the optimisation), mirroring container_paused's tri-state."""
        result = self._run(
            ["image", "inspect", "-f", "{{len .RootFS.Layers}}", tag],
            check=False,
        )
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def squash_image(self, src_tag: str, dest_tag: str) -> bool:
        """Flatten src_tag into a single-layer dest_tag via export|import.

        The one method that bypasses the runner: export|import needs a real
        pipe between two CLI processes, which the single-command runner
        cannot express. The argv prefix mirrors make_runner, so the
        podman/sudo abstraction still holds. Returns False on any failure
        and never leaves dest_tag behind (an aborted export can make
        `import -` succeed on a truncated stream, so the tag is removed on
        failure); the temporary container is removed best-effort and the
        export process is reaped on every path, even when `import` raises.
        """
        prefix: tuple[str, ...] = (
            ("sudo", "-n", self._binary) if self._sudo else (self._binary,)
        )
        created = subprocess.run(
            (*prefix, "create", src_tag, "true"), text=True, capture_output=True
        )
        if created.returncode != 0:
            return False
        cid = created.stdout.strip()
        try:
            export = subprocess.Popen(
                (*prefix, "export", cid), stdout=subprocess.PIPE
            )
            try:
                imported = subprocess.run(
                    (*prefix, "import", "-", dest_tag),
                    stdin=export.stdout, text=True, capture_output=True,
                )
            finally:
                # Reap the export process even when `import` raised (OSError,
                # KeyboardInterrupt): otherwise the Popen is neither closed
                # nor waited — a zombie holding its pipe open.
                if export.stdout is not None:
                    export.stdout.close()
                export_rc = export.wait()
            ok = export_rc == 0 and imported.returncode == 0
            if not ok:
                subprocess.run(
                    (*prefix, "rmi", "-f", dest_tag),
                    text=True, capture_output=True,
                )
            return ok
        finally:
            subprocess.run(
                (*prefix, "rm", "-f", cid), text=True, capture_output=True
            )

    def pause_container(self, name: str) -> bool:
        return self._run(["pause", name], check=False).returncode == 0

    def unpause_container(self, name: str) -> bool:
        return self._run(["unpause", name], check=False).returncode == 0

    def container_paused(self, name: str) -> bool | None:
        """Tri-state probe: True/False only when the inspect positively
        answered; None when it failed or returned anything else, so callers
        never mistake "could not check" for "verified not paused"."""
        result = self._run(
            ["inspect", "-f", "{{.State.Paused}}", name], check=False
        )
        if result.returncode != 0:
            return None
        answer = result.stdout.strip()
        if answer == "true":
            return True
        if answer == "false":
            return False
        return None
