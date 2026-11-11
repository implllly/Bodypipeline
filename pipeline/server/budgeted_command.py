"""Reconcile requested CPU parallelism with the actual Slurm execution step."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pipeline.shared.core import atomic_json
from pipeline.shared.execution_budget import available_cpus


def bounded_arguments(args, cpus):
    args = list(args)
    limits = {}
    for flag in ("--workers", "--video-workers"):
        if flag in args:
            index = args.index(flag) + 1
            limits[flag] = min(cpus, max(1, int(args[index])))
            args[index] = str(limits[flag])
    if "--video-codec-threads" in args:
        index = args.index("--video-codec-threads") + 1
        workers = limits.get("--video-workers", limits.get("--workers", 1))
        args[index] = str(min(max(1, cpus // workers), max(1, int(args[index]))))
    return args


def main():
    path, requested, *args = sys.argv[1:]
    if not args or args.pop(0) != "--" or not args:
        raise ValueError("missing command")
    cpus = min(int(requested), available_cpus())
    inherited = os.environ.get("SLURM_CPUS_PER_TASK")
    if inherited:
        cpus = min(cpus, int(inherited))
    if cpus < 1:
        raise ValueError("invalid execution CPU budget")
    bounded = bounded_arguments(args, cpus)
    atomic_json(
        Path(path),
        {
            "requested_cpus": int(requested),
            "effective_cpus": cpus,
            "parallelism": {
                flag: int(bounded[bounded.index(flag) + 1])
                for flag in ("--workers", "--video-workers", "--video-codec-threads")
                if flag in bounded
            },
        },
    )
    # Keep stdout/stderr exclusively for the tool's report/log protocol.
    os.execvpe(bounded[0], bounded, os.environ.copy())


if __name__ == "__main__":
    main()
