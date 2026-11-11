"""Explicit CPU accounting, shared by configuration and command launchers."""

from __future__ import annotations

import os
from dataclasses import dataclass


def available_cpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def positive_int(value, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


@dataclass(frozen=True)
class ExecutionBudget:
    cpus: int
    workers: int
    codec_threads: int
    basis: str

    @classmethod
    def resolve(cls, config: dict) -> ExecutionBudget:
        if not isinstance(config, dict):
            raise ValueError("slurm 必须是配置对象")
        cpus = available_cpus()
        basis = "process_affinity"
        if config.get("enabled"):
            # This method runs in the Server, NOT in the remote allocation.
            # Its affinity is not evidence of the remote node's CPU budget.
            cpus = positive_int(config.get("cpus_per_task", 16), "cpus_per_task")
            basis = "slurm_request"
            if os.environ.get("SLURM_JOB_ID"):
                inherited = os.environ.get("SLURM_CPUS_PER_TASK")
                if not inherited or not inherited.isdecimal() or int(inherited) < 1:
                    raise ValueError(
                        "复用 Slurm allocation 需要有效的 SLURM_CPUS_PER_TASK"
                    )
                cpus = min(cpus, int(inherited), available_cpus())
                basis = "inherited_slurm"
        requested = config.get("conversion_workers", min(cpus, 8))
        workers = min(cpus, positive_int(requested, "conversion_workers"))
        requested_threads = config.get("video_codec_threads")
        threads = max(1, cpus // workers)
        if requested_threads is not None:
            threads = min(
                threads, positive_int(requested_threads, "video_codec_threads")
            )
        return cls(cpus, workers, threads, basis)

    def parallelism(self) -> dict:
        return {
            "convert_workers": self.workers,
            "video_workers": self.workers,
            "video_codec_threads": self.codec_threads,
        }

    def public(self) -> dict:
        return {"cpus": self.cpus, "basis": self.basis, **self.parallelism()}
