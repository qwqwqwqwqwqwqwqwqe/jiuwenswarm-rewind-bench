# replay/sandbox/domains/rootfs.py
from __future__ import annotations

from dataclasses import dataclass

from ..docker_client import DockerClient


@dataclass(frozen=True)
class ContainerSpec:
    image: str
    name: str
    workspace_dir: str
    session_id: str
    mount_target: str = "/workspace"
    user: str | None = None
    extra_env: tuple[tuple[str, str], ...] = ()
    add_hosts: tuple[str, ...] = ()


class RootfsDomain:
    """Container rootfs state via docker commit / recreate."""

    name = "rootfs"

    def __init__(self, docker: DockerClient, spec: ContainerSpec) -> None:
        self._docker = docker
        self._spec = spec

    @property
    def spec(self) -> ContainerSpec:
        return self._spec

    def image_tag(self, checkpoint_id: str) -> str:
        return f"replay-sbx/{self._spec.session_id}:{checkpoint_id}"

    def start(self, from_image: str | None = None) -> str:
        return self._docker.run_detached(
            image=from_image or self._spec.image,
            name=self._spec.name,
            mounts=[(self._spec.workspace_dir, self._spec.mount_target)],
            labels={"replay.sandbox.session": self._spec.session_id},
            workdir=self._spec.mount_target,
            user=self._spec.user,
            env=dict(self._spec.extra_env),
            add_hosts=self._spec.add_hosts,
        )

    def checkpoint(self, checkpoint_id: str) -> str:
        tag = self.image_tag(checkpoint_id)
        self._docker.commit(self._spec.name, tag)
        return tag

    def restore(self, image_tag: str) -> None:
        self._docker.remove_container(self._spec.name)
        self.start(from_image=image_tag)

    def down(self) -> None:
        self._docker.remove_container(self._spec.name)
