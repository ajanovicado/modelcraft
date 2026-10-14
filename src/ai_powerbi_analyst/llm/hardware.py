"""Pure profile selection: memory is reported, never a reason to refuse the model."""

from __future__ import annotations

from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class Hardware:
    total_bytes: int
    available_bytes: int
    physical_cores: int

    @classmethod
    def detect(cls) -> Hardware:
        memory = psutil.virtual_memory()
        return cls(memory.total, memory.available, psutil.cpu_count(logical=False) or 1)


@dataclass(frozen=True)
class Candidate:
    key: str
    name: str
    installed: bool
    qualified: bool


@dataclass(frozen=True)
class Selection:
    candidate: Candidate | None
    reason: str


def select_profile(candidates: list[Candidate]) -> Selection:
    """Return the first installed, qualified profile; this never starts a model.

    Qwen 4B is the only profile and runs whatever the free or total RAM, so
    Hardware only feeds health and evidence. Pins are verified again at startup.
    """
    for candidate in candidates:
        if candidate.installed and candidate.qualified:
            return Selection(candidate, "The pinned local model is installed.")
    return Selection(None, "Qwen 4B is not installed yet. Install it from Settings.")
