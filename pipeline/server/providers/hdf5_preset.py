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
    extra_numeric_fields: tuple[str, ...] = (),
) -> Path:
    import h5py

    cameras = []
    with h5py.File(episode, "r") as handle:
        for camera in profile["cameras"]:
            key = f"observation.images.{camera['id']}"
            dataset = handle[key]
            cameras.append(
                {
                    "source_key": key,
                    "target_key": key,
                    "width": int(dataset.attrs["width"]),
                    "height": int(dataset.attrs["height"]),
                    "encoded_format": str(dataset.attrs.get("encoded_format", "jpeg")),
                }
            )
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
            "numeric_fields": [
                {
                    "source_key": key,
                    "target_key": key,
                    "dtype": "float32",
                    "names": (
                        list(profile["joint_names"])
                        if key in ("action", "observation.state")
                        and isinstance(profile.get("joint_names"), list)
                        else None
                    ),
                }
                for key in ("action", "observation.state", *extra_numeric_fields)
            ],
            "video_fields": cameras,
        },
    }
    path = source.parent / "letools-hdf5-preset.json"
    atomic_json(path, preset)
    return path
