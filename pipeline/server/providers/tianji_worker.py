"""Standalone read-only native worker, run in the separately installed toolchain.

Never imports ROS or calls Apex. The native provider resolves embedded schemas.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def run(request):
    import cv2
    import h5py
    import numpy as np
    import tianji_provider
    from tianji_provider.media import CAMERAS, crop, decode_quad
    from tianji_provider.provider import TianjiConfig
    from tianji_provider.source import TianjiSource

    if tianji_provider.__version__ != "0.7.0":
        raise ValueError("Expected tianji-provider 0.7.0")
    profile = request["profile"]
    source = TianjiSource(
        request["raw"],
        TianjiConfig(task="recorded teleoperation", fps=50),
        _collect_image_stats=False,
        _workers=1,
    )
    if len(source.episodes) != 1:
        raise ValueError("One raw recording must produce exactly one episode")
    episode = source.episodes[0]
    table = source.read_episode(episode)
    n = episode.length
    final = Path(request["aligned"])
    temporary = final.with_suffix(".tmp")
    if final.exists() or temporary.exists():
        raise FileExistsError("Refusing to overwrite an existing aligned episode")
    streams = {}
    with h5py.File(temporary, "x") as f:
        f.attrs.update(
            schema="bodypipeline.tianji.aligned_hdf5.v1",
            fps=50,
            frame_count=n,
            source_format="tianji_apex_mcap_v1",
        )
        for native, key in [
            ("observation.state", "observation.state"),
            ("observation.action", "action"),
            ("observation.eef", "observation.eef"),
            ("observation.wrench_windows", "observation.wrench_windows"),
            ("timestamp", "timestamp"),
        ]:
            a = np.asarray(table[native].to_pylist(), dtype=np.float32)
            if len(a) != n or not np.isfinite(a).all():
                raise ValueError("Invalid native feature: " + native)
            f.create_dataset(key, data=a, compression="gzip")
            if key != "timestamp":
                streams[key] = dict(dimensions=list(a.shape[1:]))
        audit = source.audit(episode)
        for key in (
            "absolute_time_ns",
            "video_packet_index",
            "video_time_ns",
            "wrench_time_ns",
            "wrench_sample_time_ns",
        ):
            f.create_dataset("audit/" + key, data=audit[key], compression="gzip")
        # Decode the quad once; reuse the same selected frame for all cameras.
        first = episode.videos["observation.image.head_left"]
        wanted = first.indices
        datasets = {}
        for camera in CAMERAS:
            key = f"observation.images.{camera}"
            ds = f.create_dataset(
                key, shape=(n,), dtype=h5py.vlen_dtype(np.dtype("uint8"))
            )
            ds.attrs.update(
                width=first.width, height=first.height, encoded_format="png"
            )
            datasets[camera] = ds
            streams[key] = dict(dimensions=[first.height, first.width, 3])
        cursor = 0
        # Exhaust the decoder so its packet/frame integrity check also runs.
        for index, frame in decode_quad(first.paths):
            if cursor == n or index < wanted[cursor]:
                continue
            if index != wanted[cursor]:
                raise ValueError("Selected video frame missing")
            rgb = frame.to_ndarray(format="rgb24")
            encoded = {}
            for camera in CAMERAS:
                ok, payload = cv2.imencode(
                    ".png",
                    cv2.cvtColor(crop(rgb, camera), cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_PNG_COMPRESSION, 1],
                )
                if not ok:
                    raise ValueError("PNG encoder failed")
                encoded[camera] = payload.reshape(-1)
            while cursor < n and wanted[cursor] == index:
                for camera, ds in datasets.items():
                    ds[cursor] = encoded[camera]
                cursor += 1
        if cursor != n:
            raise ValueError("Video ended before the selected final frame")
        f.attrs["native_alignment_reports"] = json.dumps(
            source.reports, ensure_ascii=False
        )
        f.attrs["native_feature_schema"] = json.dumps(
            source.metadata.features, ensure_ascii=False
        )
    os.replace(temporary, final)
    report = dict(
        frames=n,
        fps=profile["output_fps"],
        streams=streams,
        errors=[],
        aligned_path=str(final),
        aligned_kind="hdf5",
        native_alignment_reports=source.reports,
    )
    Path(request["report"]).write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
