"""Trusted capture-format selection; session/upload logic is robot independent."""

from pathlib import Path

from pipeline.shared.core import DomainError, file_hash
from pipeline.shared.hdf5 import AGILEX_HDF5_FORMAT, package_agilex_capture
from pipeline.shared.tianji import TIANJI_MCAP_FORMAT, recording_files


class AgileXPackager:
    raw_format = AGILEX_HDF5_FORMAT

    def seal(self, root, profile, metadata):
        package = root.parent / "episode.hdf5"
        stats = package_agilex_capture(root, package, profile, metadata=metadata)
        return dict(
            package_path=str(package),
            package_schema=stats["schema"],
            package_stats=stats,
            files=[
                dict(
                    path=package.name,
                    size_bytes=package.stat().st_size,
                    sha256=file_hash(package),
                )
            ],
        )


class TianjiMCAPPackager:
    raw_format = TIANJI_MCAP_FORMAT

    def seal(self, root, profile, metadata):
        if profile.get("robot_type") != "tianji_marvin_pro":
            raise DomainError("Tianji MCAP packager requires tianji_marvin_pro")
        # MCAP already contains compressed images and schemas in a few large files.
        # Preserve these bytes; no JPEG expansion, resampling, or feedback fallback.
        files = [
            dict(
                path=p.relative_to(root).as_posix(),
                size_bytes=p.stat().st_size,
                sha256=file_hash(p),
            )
            for p in recording_files(Path(root) / "episode0")
        ]
        return dict(
            package_schema=TIANJI_MCAP_FORMAT,
            package_stats=dict(files=len(files), metadata=metadata),
            files=files,
        )


def create_packager(name):
    factories = {"agilex": AgileXPackager, "tianji_mcap": TianjiMCAPPackager}
    if name not in factories:
        raise DomainError(f"Unknown capture packager: {name!r}")
    return factories[name]()
