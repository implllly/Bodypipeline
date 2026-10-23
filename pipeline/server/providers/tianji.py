"""Trusted MCAP -> aligned HDF5 boundary for the installed Tianji provider."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json
from pipeline.shared.tianji import (
    CAMERAS,
    JOINT_NAMES,
    JOINT_UNITS,
    TIANJI_ALIGNED_SCHEMA,
    TIANJI_MCAP_FORMAT,
    recording_files,
)

from ..quality import robot_profile_blockers, validate_robot_policy
from .aloha_hdf5 import AlohaHDF5Provider
from .hdf5_preset import write_hdf5_preset


class TianjiProvider(AlohaHDF5Provider):
    id = "tianji"
    source_format = TIANJI_MCAP_FORMAT
    raw_formats = (TIANJI_MCAP_FORMAT,)

    def validate_layout(self, profile):
        if profile.get("robot_type") != "tianji_marvin_pro":
            raise DomainError("Tianji provider requires tianji_marvin_pro")
        if (
            profile.get("joint_names") != JOINT_NAMES
            or profile.get("joint_units") != JOINT_UNITS
        ):
            raise DomainError(
                "Tianji requires the verified left7/gripper/right7/gripper schema"
            )
        if [c.get("id") for c in profile.get("cameras", [])] != list(CAMERAS):
            raise DomainError(
                "Tianji requires all four quad cameras in their recorded order"
            )
        if (
            profile.get("output_fps") != 50
            or profile.get("native_provider_version") != "0.7.0"
        ):
            raise DomainError(
                "This Tianji profile requires 50 Hz and tianji-provider 0.7.0"
            )

    def validate_profile(self, profile):
        validate_robot_policy(profile, expected_dimensions=16)
        self.validate_layout(profile)

    def profile_blockers(self, profile):
        blockers = robot_profile_blockers(profile, expected_dimensions=16)
        try:
            self.validate_layout(profile)
        except (DomainError, TypeError, ValueError) as exc:
            blockers.append(dict(field="provider_profile", message=str(exc)))
        return blockers

    def prepare(self, raw: Path, dest: Path, profile: dict) -> dict:
        self.validate_profile(profile)
        root = raw if (raw / "data").is_dir() else raw / "episode0"
        recording_files(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        request = dest.with_suffix(".request.json")
        report = dest.with_suffix(".report.json")
        aligned = dest.with_suffix(".hdf5")
        atomic_json(
            request,
            dict(
                raw=str(root), aligned=str(aligned), report=str(report), profile=profile
            ),
        )
        # Executable is a deployment choice, never an upload/Manifest field.
        executable = os.environ.get("PIPELINE_TIANJI_PYTHON", sys.executable)
        worker = Path(__file__).with_name("tianji_worker.py")
        try:
            result = subprocess.run(
                [executable, "-B", str(worker), str(request)],
                capture_output=True,
                text=True,
                timeout=7200,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DomainError(
                "Tianji worker unavailable or timed out; raw recording preserved"
            ) from exc
        atomic_json(
            dest.with_suffix(".worker-log.json"),
            dict(
                returncode=result.returncode,
                stdout=result.stdout[-16000:],
                stderr=result.stderr[-16000:],
            ),
        )
        if result.returncode != 0:
            raise DomainError("Tianji alignment failed: " + result.stderr[-1500:])
        return json.loads(report.read_text())

    def quality(self, aligned, profile, synthetic=False):
        import io

        import h5py
        import numpy as np
        from PIL import Image

        from ..quality import robot_quality

        self.validate_profile(profile)
        with h5py.File(aligned, "r") as f:
            if f.attrs.get("schema") != TIANJI_ALIGNED_SCHEMA:
                raise DomainError("Invalid Tianji aligned schema")
            n = int(f.attrs["frame_count"])
            if n < profile.get("minimum_frames", 2):
                raise DomainError("Tianji episode too short")
            shapes = {
                "action": (16,),
                "observation.state": (16,),
                "observation.eef": (14,),
                "observation.wrench_windows": (4, 12),
                "timestamp": (),
            }
            for key, shape in shapes.items():
                a = f[key][:]
                if a.shape != (n, *shape) or not np.isfinite(a).all():
                    raise DomainError("Invalid Tianji numeric feature: " + key)
            if not np.allclose(f["timestamp"][:], np.arange(n) / 50, atol=1e-5):
                raise DomainError("Tianji time grid mismatch")
            for camera in CAMERAS:
                ds = f[f"observation.images.{camera}"]
                if len(ds) != n:
                    raise DomainError("Incomplete camera: " + camera)
                for payload in ds:
                    with Image.open(io.BytesIO(payload.tobytes())) as image:
                        image.load()
                        if image.size != (ds.attrs["width"], ds.attrs["height"]):
                            raise DomainError("Camera dimensions changed: " + camera)
        return robot_quality(aligned, profile, synthetic, expected_dimensions=16)

    def conversion_args(self, *, executable, source, destination, profile, job):
        args = super().conversion_args(
            executable=executable,
            source=source,
            destination=destination,
            profile=profile,
            job=job,
        )
        preset = write_hdf5_preset(
            source,
            profile,
            job,
            sorted(source.glob("*.hdf5"))[0],
            name="bodypipeline_tianji_v1",
            extra_numeric_fields=("observation.eef", "observation.wrench_windows"),
        )
        args[args.index("--preset") + 1] = str(preset)
        return args

    def patch_dataset_info(self, dataset, profile, *, synthetic=False):
        super().patch_dataset_info(dataset, profile, synthetic=synthetic)
        if synthetic:
            return
        path = dataset / "meta/info.json"
        info = json.loads(path.read_text())
        info["tianji"] = self.lineage_metadata(profile)
        for key, metadata in profile.get("auxiliary_feature_metadata", {}).items():
            if key not in info["features"]:
                raise DomainError("Converted auxiliary feature missing: " + key)
            info["features"][key].update(metadata)
        atomic_json(path, info)

    def lineage_metadata(self, profile):
        return {
            **super().lineage_metadata(profile),
            "native_provider_version": "0.7.0",
            "action_source": "recorded joint_cmd_A/B and gripperValueL/R",
            "action_feedback_substitution": False,
            "gripper_units": "normalized",
            "camera_transport": "lossless PNG quadrants; no resize/crop beyond tile split",
            "alignment": "tianji-provider defaults, stationary-edge trim; strict command freshness",
            "field_mapping": {
                "observation.action": "action",
                "observation.image.*": "observation.images.*",
            },
        }
