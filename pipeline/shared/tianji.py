"""Tianji's versioned MCAP contract. No robot/ROS dependencies."""

from pathlib import Path

from .core import DomainError

TIANJI_MCAP_FORMAT = "tianji_apex_mcap_v1"
TIANJI_ALIGNED_SCHEMA = "bodypipeline.tianji.aligned_hdf5.v1"
CAMERAS = ("head_left", "head_right", "left_wrist", "right_wrist")
JOINT_NAMES = [
    name
    for side in ("left", "right")
    for name in (
        *[f"{side}_joint_{j}_rad" for j in range(1, 8)],
        f"{side}_gripper_normalized",
    )
]
JOINT_UNITS = ["rad"] * 7 + ["normalized"] + ["rad"] * 7 + ["normalized"]


def recording_files(root: Path) -> list[Path]:
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise DomainError("Tianji recording must be a real, absolute directory")
    entries = sorted(root.rglob("*"))
    if any(p.is_symlink() for p in entries):
        raise DomainError("Tianji recording contains a symbolic link")
    for part in ("data", "video"):
        directory = root / part
        if not directory.is_dir() or not list(directory.rglob("*.mcap")):
            raise DomainError(f"Tianji recording is missing {part} MCAP")
        if not list(directory.rglob("metadata.yaml")):
            raise DomainError(f"Tianji recording is missing {part} metadata.yaml")
    files = [p for p in entries if p.is_file()]
    if any(p.stat().st_size == 0 for p in files):
        raise DomainError("Tianji recording contains an empty file")
    return files
