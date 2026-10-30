"""Explicit format bindings; heavy verification runs only under a work lease."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json, digest, file_hash
from pipeline.shared.hdf5 import (
    AGILEX_HDF5_FILENAME,
    AGILEX_HDF5_FORMAT,
    package_agilex_capture,
)

from .resources import file_identity


def seal_raw(row, work):
    source = Path(row["source_directory"])
    metadata = Path(row["metadata_path"])
    before_metadata = file_identity(metadata)
    ep = json.loads(metadata.read_text())
    if digest(ep) != row["source_metadata_hash"]:
        raise DomainError("原始 episode 元数据在登记后发生变化")
    packet = source / "packets.raw"
    identity = file_identity(packet)
    if (
        ep.get("state") != "saved"
        or ep.get("file") != "packets.raw"
        or ep.get("bytes") != identity[2]
    ):
        raise DomainError("原始 episode 未完整保存")
    if work.hash_file(packet) != ep.get("sha256") or file_identity(packet) != identity:
        raise DomainError("原始 raw 校验失败或校验时发生变化")
    staged = Path(row["raw_root"]) / "episode0"
    staged.mkdir(parents=True, exist_ok=True)
    sources = {"episode0/packets.raw": str(packet)}
    atomic_json(staged / "episode.json", ep)
    sources["episode0/episode.json"] = str(staged / "episode.json")
    config_path = Path(row["robot_config_path"])
    if config_path.exists():
        before = file_identity(config_path)
        cfg = json.loads(config_path.read_text())
        if (
            cfg.get("hand_type") != "brainco"
            or cfg.get("hand_num_motors") != 6
            or cfg.get("hand_units") != "normalized"
            or "sonic_v1_1" not in Path(cfg.get("model_path", "")).parts
        ):
            raise DomainError("原始 robot_config 不匹配 SONIC 1.1 / BrainCo")
        if file_identity(config_path) != before:
            raise DomainError("robot_config 在封存时发生变化")
        atomic_json(staged / "robot_config.json", cfg)
        sources["episode0/robot_config.json"] = str(staged / "robot_config.json")
    if file_identity(metadata) != before_metadata:
        raise DomainError("episode 元数据在封存时发生变化")
    files, identities = [], {}
    for name, value in sources.items():
        path = Path(value)
        identities[name] = file_identity(path)
        sha = ep["sha256"] if name.endswith("packets.raw") else work.hash_file(path)
        files.append({"path": name, "size_bytes": path.stat().st_size, "sha256": sha})
    if (
        file_identity(packet) != identity
        or identities["episode0/packets.raw"] != identity
    ):
        raise DomainError("原始 raw 在封存过程中发生变化")
    return {
        "files": files,
        "upload_sources": sources,
        "verified_files": identities,
        "package_schema": "g1_sonic_raw_v1",
        "package_stats": {"size_bytes": sum(f["size_bytes"] for f in files)},
    }


def raw_format(capabilities):
    return capabilities.get("raw_format", AGILEX_HDF5_FORMAT)


def seal_agilex(row, work, *, hasher=file_hash):
    root = Path(row["raw_root"])
    package = root.parent / AGILEX_HDF5_FILENAME
    stats = package_agilex_capture(
        root,
        package,
        row["profile"],
        metadata={
            "session_id": row["id"],
            "robot_id": row["robot_id"],
            "project_id": row["project_id"],
            "instruction": row["instruction"],
            "dataset_name": row.get("dataset_name") or row["instruction"],
            "started_at": row["started_at"],
            "stopped_at": row.get("stopped_at"),
            "capture_mode": row["mode"],
            "profile_hash": row["profile_hash"],
        },
    )
    return {
        "files": [
            {
                "path": AGILEX_HDF5_FILENAME,
                "size_bytes": stats["size_bytes"],
                "sha256": hasher(package),
            }
        ],
        "package_path": str(package),
        "package_schema": stats["schema"],
        "package_stats": stats,
    }


def seal_capture(row, capabilities, work, *, hasher=file_hash):
    binding = capabilities.get("packager", "agilex_hdf5")
    if binding == "agilex_hdf5":
        return seal_agilex(row, work, hasher=hasher)
    if binding == "g1_sonic_raw":
        return seal_raw(row, work)
    raise DomainError("未知原始数据封存实现，拒绝回退到其他机器人")
