from __future__ import annotations

from pathlib import Path

from .types import SandboxError, WorldCheckpoint


class CheckpointRegistry:
    """Append-only JSONL registry of WorldCheckpoints for one session."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, checkpoint: WorldCheckpoint) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(checkpoint.to_json() + "\n")
            handle.flush()

    def load(self) -> list[WorldCheckpoint]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return [WorldCheckpoint.from_json(line) for line in lines if line.strip()]

    def get(self, checkpoint_id: str) -> WorldCheckpoint:
        for checkpoint in self.load():
            if checkpoint.checkpoint_id == checkpoint_id:
                return checkpoint
        raise SandboxError(f"Unknown checkpoint: {checkpoint_id}")

    def next_checkpoint_id(self) -> str:
        return f"ckpt_{len(self.load()):06d}"
