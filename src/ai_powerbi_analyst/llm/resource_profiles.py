"""The single pinned local model profile and its evidence-only resource sampler."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import psutil
from powerbi_rag_runtime import guards
from powerbi_rag_runtime.guards import Guard


@dataclass(frozen=True)
class ResourceProfile:
    key: str
    label: str
    tag: str
    digest: str
    process_gib: float
    cache_mib: int
    idle_seconds: int


# Qwen3.5 4B is the only local model; runtime/catalog.json records retired profiles.
PROFILES = {
    "standard": ResourceProfile(
        "standard",
        "Standard · Qwen 4B · text only",
        "qwen3.5:4b-q4_K_M",
        "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd",
        6,
        128,
        60,
    ),
}


def configured_profile(cfg: Mapping[str, object]) -> ResourceProfile | None:
    key = cfg.get("resource_profile")
    if key is None:
        return None
    if not isinstance(key, str) or key not in PROFILES:
        raise ValueError("Unknown resource profile")
    profile = PROFILES[key]
    if cfg.get("tag") != profile.tag or cfg.get("package_sha256") != profile.digest:
        raise ValueError("Resource profile does not match the pinned model package")
    if cfg.get("prompt_cache_mib") != profile.cache_mib:
        raise ValueError("Resource profile cache budget differs")
    if key == "standard" and cfg.get("backend") != "managed-llama-text":
        raise ValueError("Standard memory budget requires the text-only backend")
    return profile


class ProfileGuard(Guard):
    """Record resources as evidence; only the user's Stop request ends a session.

    Free RAM, disk, AC power, paging and session length never refuse or stop Qwen 4B
    (it peaks near 3.5 GiB). The kernel job limit and the owned server's 6 GiB runaway
    cap still apply to the model process itself.
    """

    root: Path

    def __init__(self, root: Path, profile: ResourceProfile) -> None:
        super().__init__(root)
        self.profile = profile

    def check(self, startup: bool = False) -> dict[str, object]:
        # Startup and running checks are identical; `startup` keeps the Guard interface.
        if (self.root / "STOP_REQUESTED").exists():
            raise RuntimeError("Stop requested")
        try:
            return guards.resources(self.root)
        except Exception as error:  # noqa: BLE001 - telemetry is evidence, never a gate
            # Any sampler failure (PDH or power status, psutil's swap counters, disk) is
            # recorded only: the watchdog stops the model on whatever check() raises.
            sample: dict[str, object] = {"telemetry_error": f"{type(error).__name__}: {error}"}
            with contextlib.suppress(Exception):
                sample["available"] = psutil.virtual_memory().available
            return sample
