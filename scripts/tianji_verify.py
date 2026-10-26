"""Offline verification only: existing MCAP -> HDF5 -> LeRobot -> Doctor.

Run with Bodypipeline's API environment. Native provider and letools execute in
their configured separate environment. This script never imports ROS or Apex.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from pipeline.server.providers.tianji import TianjiProvider
from pipeline.shared.core import atomic_json, file_hash
from pipeline.shared.tianji import recording_files


def fingerprint(root):
    return {p.relative_to(root).as_posix(): file_hash(p) for p in recording_files(root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--profile", type=Path, default=Path("configs/tianji-marvin-pro-v1.json")
    )
    parser.add_argument("--letools", required=True)
    args = parser.parse_args()
    raw, output = args.raw.resolve(), args.output.resolve()
    if output == raw or output.is_relative_to(raw):
        parser.error("Output must be outside the original recording")
    output.mkdir(parents=True, exist_ok=False)
    before = fingerprint(raw)
    atomic_json(output / "raw-before.json", before)
    profile = json.loads(args.profile.read_text())
    provider = TianjiProvider()
    report = dict(raw=str(raw), robot_operation=False)
    report["synthetic_fixture"] = (raw / "SYNTHETIC.txt").is_file()
    try:
        prepared = provider.prepare(raw, output / "native/aligned", profile)
        report["alignment"] = prepared
        path = Path(prepared["aligned_path"])
        report["quality"] = provider.quality(path, profile)
        aligned = output / "aligned"
        aligned.mkdir()
        shutil.move(path, aligned / "episode0.hdf5")
        for version in ("v2.1", "v3.0"):
            dest = output / version
            command = provider.conversion_args(
                executable=args.letools,
                source=aligned,
                destination=dest,
                profile=profile,
                job=dict(instruction="offline verification", target_format=version),
            )
            for name, cmd in [
                ("convert", command),
                ("validate", [args.letools, "validate", str(dest), "--deep"]),
            ]:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=1800
                )
                (output / f"{version}-{name}.log").write_text(
                    result.stdout + result.stderr
                )
                if result.returncode:
                    raise RuntimeError(f"{version} {name} failed; see saved log")
            provider.patch_dataset_info(dest, profile)
            info_path = dest / "meta/info.json"
            info = json.loads(info_path.read_text())
            info["verification_only"] = True
            info["synthetic_fixture"] = report["synthetic_fixture"]
            atomic_json(info_path, info)
            result = subprocess.run(
                [
                    args.letools,
                    "doctor",
                    "check",
                    str(dest),
                    "--checks",
                    "metadata,temporal,actions,videos,statistics,episodes,consistency,training,anomalies,portability,per_episode",
                    "--ci",
                    "--fail-on",
                    "warn",
                ],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            (output / f"{version}-doctor.json").write_text(result.stdout)
            (output / f"{version}-doctor.log").write_text(result.stderr)
            report[version] = dict(
                converted=True, deep_validated=True, doctor_returncode=result.returncode
            )
            if result.returncode not in (0, 1):
                raise RuntimeError("Doctor invocation failed")
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        report["raw_unchanged"] = before == fingerprint(raw)
        atomic_json(output / "verification.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
