"""Mapping preset for converting aligned HDF5 with ``letools convert``.

Every HDF5-backed provider writes its aligned file to the same field layout
(``action``, ``observation.state``, ``observation.images.<id>``), so the preset
that tells letools how to read it is produced here once instead of per robot.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.shared.core import atomic_json


def write_hdf5_preset(
    source: Path,
    profile: dict,
    job: dict,
    episode: Path,
    *,
    name: str,
    extra_numeric_fields: list[dict] | None = None,
) -> Path:
    import h5py

    cameras = []
    with h5py.File(episode, "r") as handle:
        for camera in profile["cameras"]:
            key = f"observation.images.{camera['id']}"
            dataset = handle[key]
            encoded_format = str(dataset.attrs.get("encoded_format", "jpeg")).lower()
            if encoded_format == "jpg":
                encoded_format = "jpeg"
            if encoded_format not in {"jpeg", "png"}:
                raise ValueError(
                    f"{key} 的 encoded_format={encoded_format!r} 不受支持"
                )
            cameras.append(
                {
                    "source_key": key,
                    "target_key": key,
                    "width": int(dataset.attrs["width"]),
                    "height": int(dataset.attrs["height"]),
                    "encoded_format": encoded_format,
                }
            )
    numeric_fields = [
        {
            "source_key": key,
            "target_key": key,
            "dtype": "float32",
            "names": (
                list(profile["joint_names"])
                if isinstance(profile.get("joint_names"), list)
                else None
            ),
        }
        for key in ("action", "observation.state")
    ]
    for field in extra_numeric_fields or []:
        if not isinstance(field, dict):
            raise ValueError("extra_numeric_fields 必须是对象列表")
        copied = dict(field)
        if not all(
            isinstance(copied.get(key), str) and copied[key]
            for key in ("source_key", "target_key", "dtype")
        ):
            raise ValueError("extra_numeric_fields 缺少 source_key/target_key/dtype")
        numeric_fields.append(copied)
    preset = {
        "schema_version": 1,
        "name": name,
        "description": "Generated from the trusted Runtime profile",
        "mapping": {
            "fps": profile["output_fps"],
            "episode_glob": "*.hdf5",
            "robot_type": profile["robot_type"],
            "task_key": None,
            "default_task": job["instruction"],
            "numeric_fields": numeric_fields,
            "video_fields": cameras,
        },
    }
    path = source.parent / "letools-hdf5-preset.json"
    atomic_json(path, preset)
    return path
