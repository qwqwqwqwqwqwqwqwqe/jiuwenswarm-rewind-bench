from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field


class SandboxError(Exception):
    """Raised for sandbox subsystem failures."""


@dataclass(frozen=True)
class WorldCheckpoint:
    checkpoint_id: str
    parent_id: str | None
    handles: dict[str, str]
    trigger: str
    label: str | None
    created_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "WorldCheckpoint":
        payload = json.loads(line)
        return cls(**payload)


@dataclass
class RestoreReport:
    checkpoint_id: str
    domain_results: dict[str, str] = field(default_factory=dict)
    reason: str | None = None
    direction: str | None = None
    replatform_layers: int | None = None
    replatform_checkpoint_id: str | None = None
    rewound_over: list[str] = field(default_factory=list)
    files_diff: list[str] = field(default_factory=list)
    processes_restarted: list[str] = field(default_factory=list)
    processes_lost: list[str] = field(default_factory=list)
    ledger_uncovered: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [f"Rewound world to {self.checkpoint_id}."]
        if self.reason:
            lines.append(f"Reason recorded: {self.reason}")
        if self.direction:
            lines.append(f"Direction: {self.direction}")
        if self.replatform_checkpoint_id:
            lines.append(
                f"base re-platformed: {self.replatform_layers} layers -> 1, "
                f"recorded as {self.replatform_checkpoint_id}"
            )
        for domain, outcome in self.domain_results.items():
            lines.append(f"- {domain}: {outcome}")
        if self.rewound_over:
            lines.append(
                f"Rolled back over {len(self.rewound_over)} checkpoint(s): "
                + ", ".join(self.rewound_over)
            )
        if self.files_diff:
            lines.append("Workspace changes undone by this rewind:")
            lines.extend(f"  {entry}" for entry in self.files_diff)
        if self.processes_restarted:
            lines.append("Services restarted from the checkpoint manifest:")
            lines.extend(f"  {entry}" for entry in self.processes_restarted)
        if self.processes_lost:
            lines.append("Processes NOT restarted (state lost):")
            lines.extend(f"  {entry}" for entry in self.processes_lost)
        if self.ledger_uncovered:
            lines.append(
                "External side effects crossed by this rewind that CANNOT be undone:"
            )
            lines.extend(f"  {entry}" for entry in self.ledger_uncovered)
        for note in self.notes:
            lines.append(f"NOTE: {note}")
        return "\n".join(lines)
