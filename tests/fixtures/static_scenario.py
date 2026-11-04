"""Exercise whole-recording static-interval trimming in the letools runtime.

Builds a LeRobot v2.1 or v3.0 dataset whose first episode has static frames at
the head, in the middle and (too briefly to remove) at the tail, plus a fully
static second episode. Detection must split the first episode into two output
episodes and keep the second one unchanged and marked.

Run as a subprocess so the API test environment does not need av / letools.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from letools._stats import flatten_stats
from letools.plugins import open_dataset
from letools.validation import validate_dataset

from pipeline.server.static_intervals import normalize_policy, resolve_policy
from pipeline.server.trim_worker import _scalar_stats, run

FPS = 6
EPISODES = 2

STATIC = [0.0, 0.0]
MOTION = [
    [0.0, 0.0],
    [0.0, 0.0],
    [0.0, 0.0],
    [0.0, 0.0],
    [0.0, 0.0],
    [0.0, 0.0],
    [0.5, 0.5],
    [1.0, 1.0],
    [1.5, 1.5],
    [1.5, 1.5],
    [1.5, 1.5],
    [1.5, 1.5],
    [1.5, 1.5],
    [1.5, 1.5],
    [1.5, 1.5],
    [2.0, 2.0],
    [2.5, 2.5],
    [3.0, 3.0],
    [3.0, 3.0],
    [3.0, 3.0],
    [3.0, 3.0],
]
STATES = [MOTION, [STATIC] * len(MOTION)]
FRAMES = len(MOTION)
EXPECTED_SPLIT_LENGTHS = [3, 6]
EXPECTED_STATIC_LENGTH = len(MOTION)

FEATURES = {
    name: {"dtype": "int64", "shape": [1], "names": None}
    for name in ["episode_index", "index", "frame_index", "task_index"]
}
FEATURES["timestamp"] = {"dtype": "float32", "shape": [1], "names": None}
FEATURES["observation.state"] = {
    "dtype": "float32",
    "shape": [2],
    "names": ["joint0", "joint1"],
    "units": ["rad", "rad"],
}
FEATURES["action"] = {
    "dtype": "float32",
    "shape": [2],
    "names": ["joint0", "joint1"],
    "units": ["rad", "rad"],
}
FEATURES["observation.images.head"] = {
    "dtype": "video",
    "shape": [32, 32, 3],
    "names": ["height", "width", "channels"],
    "info": {"video.fps": FPS, "video.codec": "h264", "video.pix_fmt": "yuv420p"},
}


def _video(path: Path, episodes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=FPS)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        stream.options = {
            "crf": "18",
            "bf": "0",
            "g": str(FPS),
            "keyint_min": str(FPS),
            "sc_threshold": "0",
        }
        for i in range(episodes * FRAMES):
            pixels = np.full((32, 32, 3), [30, 100, 200][i // FRAMES], dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def _episode_table(episode: int) -> pa.Table:
    return pa.Table.from_pylist(
        [
            {
                "episode_index": episode,
                "index": episode * FRAMES + frame,
                "frame_index": frame,
                "timestamp": frame / FPS,
                "task_index": 0,
                "observation.state": list(STATES[episode][frame]),
                "action": [value + episode * 10 for value in STATES[episode][frame]],
            }
            for frame in range(FRAMES)
        ]
    )


def _episode_stats(table: pa.Table) -> dict:
    return {
        name: _scalar_stats(table[name].to_pylist())
        for name in (
            "episode_index",
            "index",
            "frame_index",
            "timestamp",
            "action",
            "observation.state",
        )
    }


def fixture(root: Path, version: str = "v3.0") -> None:
    if version == "v2.1":
        _fixture_v21(root)
    elif version == "v3.0":
        _fixture_v30(root)
    else:
        raise ValueError(f"不支持的 LeRobot 版本：{version}")
    assert validate_dataset(root, deep=True).valid, validate_dataset(root, deep=True)


def _fixture_v30(root: Path) -> None:
    (root / "meta/episodes/chunk-000").mkdir(parents=True)
    (root / "data/chunk-000").mkdir(parents=True)
    _video(root / "videos/observation.images.head/chunk-000/file-000.mp4", EPISODES)
    pq.write_table(
        pa.concat_tables([_episode_table(episode) for episode in range(EPISODES)]),
        root / "data/chunk-000/file-000.parquet",
    )
    info = {
        "codebase_version": "v3.0",
        "robot_type": "static_fixture",
        "total_episodes": EPISODES,
        "total_frames": EPISODES * FRAMES,
        "total_tasks": 1,
        "fps": FPS,
        "chunks_size": 1000,
        "splits": {"train": f"0:{EPISODES}"},
        "features": FEATURES,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/stats.json").write_text("{}\n")
    pq.write_table(
        pa.Table.from_pylist([{"task_index": 0, "task": "Test."}]),
        root / "meta/tasks.parquet",
    )
    episodes = []
    for episode in range(EPISODES):
        prefix = "videos/observation.images.head"
        episodes.append(
            {
                "episode_index": episode,
                "length": FRAMES,
                "tasks": ["Test."],
                "data/chunk_index": 0,
                "data/file_index": 0,
                "dataset_from_index": episode * FRAMES,
                "dataset_to_index": (episode + 1) * FRAMES,
                "meta/episodes/chunk_index": 0,
                "meta/episodes/file_index": 0,
                f"{prefix}/chunk_index": 0,
                f"{prefix}/file_index": 0,
                f"{prefix}/from_timestamp": episode * FRAMES / FPS,
                f"{prefix}/to_timestamp": (episode + 1) * FRAMES / FPS,
                **flatten_stats(_episode_stats(_episode_table(episode))),
            }
        )
    pq.write_table(
        pa.Table.from_pylist(episodes),
        root / "meta/episodes/chunk-000/file-000.parquet",
    )


def _fixture_v21(root: Path) -> None:
    (root / "meta").mkdir(parents=True)
    for episode in range(EPISODES):
        table = _episode_table(episode)
        path = root / f"data/chunk-000/episode_{episode:06d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path)
        _video(
            root
            / f"videos/observation.images.head/chunk-000/episode_{episode:06d}.mp4",
            1,
        )
    info = {
        "codebase_version": "v2.1",
        "robot_type": "static_fixture",
        "total_episodes": EPISODES,
        "total_frames": EPISODES * FRAMES,
        "total_tasks": 1,
        "total_chunks": 1,
        "total_videos": EPISODES,
        "fps": FPS,
        "chunks_size": 1000,
        "splits": {"train": f"0:{EPISODES}"},
        "features": FEATURES,
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/{video_key}/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.mp4",
    }
    (root / "meta/info.json").write_text(json.dumps(info))
    (root / "meta/tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "Test."}) + "\n"
    )
    (root / "meta/episodes.jsonl").write_text(
        "".join(
            json.dumps({"episode_index": episode, "length": FRAMES, "tasks": ["Test."]})
            + "\n"
            for episode in range(EPISODES)
        )
    )
    (root / "meta/episodes_stats.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "episode_index": episode,
                    "stats": _episode_stats(_episode_table(episode)),
                }
            )
            + "\n"
            for episode in range(EPISODES)
        )
    )


def inspect(root: Path) -> dict:
    source = open_dataset(root)
    episodes = []
    for episode in source.episodes:
        table = source.read_episode(episode)
        episodes.append(
            {
                "index": episode.index,
                "length": episode.length,
                "frame_index": table["frame_index"].to_pylist(),
                "index_column": table["index"].to_pylist(),
                "timestamp": table["timestamp"].to_pylist(),
                "state": table["observation.state"].to_pylist(),
                "episode_index": table["episode_index"].to_pylist(),
            }
        )
    videos = {}
    for path in sorted((root / "videos").rglob("*.mp4")):
        with av.open(str(path)) as video:
            videos[str(path.relative_to(root))] = sum(1 for _ in video.decode(video=0))
    info = json.loads((root / "meta/info.json").read_text())
    return {
        "episodes": episodes,
        "videos": videos,
        "total_episodes": info["total_episodes"],
        "total_frames": info["total_frames"],
        "valid": validate_dataset(root, deep=True).valid,
    }


def scenario(base: Path, version: str = "v3.0") -> dict:
    source = base / "source"
    fixture(source, version)
    info = json.loads((source / "meta/info.json").read_text())
    policy = normalize_policy(
        {
            "feature": "observation.state",
            "min_static_seconds": 1.0,
            "min_motion_seconds": 0.2,
            "jitter_amplitude_seconds": 0.0,
            "padding_seconds": 0.0,
            "min_output_seconds": 0.0,
        }
    )
    resolved = resolve_policy(policy, info["features"], info["fps"])
    detect_spec = base / "detect.json"
    detect_spec.write_text(
        json.dumps({"mode": "detect-static", "source": str(source), "policy": resolved})
    )
    plan = run(str(detect_spec))
    summary = plan["summary"]
    assert summary["source_episodes"] == 2, summary
    assert summary["output_episodes"] == 3, summary
    assert summary["frames"] == EPISODES * FRAMES
    assert summary["frames_removed"] == 12, summary
    assert summary["frames_kept"] == EPISODES * FRAMES - 12
    assert plan["episodes"][0]["keep_ranges"] == [[6, 9], [15, 21]]
    assert plan["episodes"][0]["status"] == "split"
    assert plan["episodes"][1]["status"] == "static"
    assert plan["episodes"][1]["keep_ranges"] == [[0, FRAMES]]

    trim_spec = base / "trim.json"
    trim_spec.write_text(
        json.dumps(
            {
                "mode": "trim",
                "source": str(source),
                "destination": str(base / "output"),
                "fps": info["fps"],
                "policy": resolved,
                "episodes": plan["episodes"],
            }
        )
    )
    result = run(str(trim_spec))
    output = base / "output"
    assert result["validated"] is True
    assert result["episodes"] == 3
    assert result["frames"] == EPISODES * FRAMES - 12
    trimmed = inspect(output)
    assert trimmed["valid"] is True
    assert trimmed["total_episodes"] == 3
    assert [row["length"] for row in trimmed["episodes"]] == [
        *EXPECTED_SPLIT_LENGTHS,
        EXPECTED_STATIC_LENGTH,
    ]
    assert trimmed["episodes"][0]["state"] == MOTION[6:9]
    assert trimmed["episodes"][1]["state"] == MOTION[15:21]
    assert all(row == STATIC for row in trimmed["episodes"][2]["state"])
    assert sum(trimmed["videos"].values()) == sum(
        row["length"] for row in trimmed["episodes"]
    )
    original = inspect(source)
    assert original["total_frames"] == EPISODES * FRAMES
    assert [row["length"] for row in original["episodes"]] == [FRAMES, FRAMES]
    return {
        "detect": summary,
        "trim": result,
        "inspected": trimmed,
        "source": original,
    }


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    if mode == "create":
        base = Path(sys.argv[2])
        version = sys.argv[3] if len(sys.argv) > 3 else "v3.0"
        base.mkdir(parents=True, exist_ok=True)
        fixture(base / "source", version)
        print(json.dumps({"source": str(base / "source")}))
    elif mode == "inspect":
        print(json.dumps(inspect(Path(sys.argv[2])), ensure_ascii=False, default=list))
    else:
        base = (
            Path(mode)
            if mode
            else Path(tempfile.mkdtemp(prefix="pipeline-static-test-"))
        )
        version = sys.argv[2] if len(sys.argv) > 2 else "v3.0"
        base.mkdir(parents=True, exist_ok=True)
        print(json.dumps(scenario(base, version), ensure_ascii=False, default=list))
