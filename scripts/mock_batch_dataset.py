"""Generate 40 distinct, variable-length episodes for local video-wall checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def generate_batch_dataset(dataset: Path):
    if (dataset / "meta/info.json").exists():
        return
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from PIL import Image, ImageDraw

    from scripts.preview_annotations import FPS, generate_dataset

    base = dataset.with_name(dataset.name + "-base")
    generate_dataset(base)
    (dataset / "meta").mkdir(parents=True, exist_ok=True)
    shutil.copy2(base / "meta/tasks.jsonl", dataset / "meta/tasks.jsonl")
    info = json.loads((base / "meta/info.json").read_text())
    cameras = [
        key for key, value in info["features"].items() if value["dtype"] == "video"
    ]
    table = pq.read_table(base / "data/chunk-000/episode_000000.parquet")
    data_folder = dataset / "data/chunk-000"
    data_folder.mkdir(parents=True, exist_ok=True)
    lengths, per_episode, all_values = [], [], {}
    offset = 0
    overlay_path = dataset / ".mock-overlay.png"
    try:
        for episode in range(40):
            frames = [240, 180, 120][episode % 3]
            lengths.append(frames)
            sliced = table.slice(0, frames)
            for key, values in {
                "episode_index": [episode] * frames,
                "index": range(offset, offset + frames),
            }.items():
                sliced = sliced.set_column(
                    sliced.schema.get_field_index(key),
                    key,
                    pa.array(values, type=pa.int64()),
                )
            pq.write_table(
                sliced,
                data_folder / f"episode_{episode:06d}.parquet",
                compression="NONE",
            )
            stats = {}
            for key, feature in info["features"].items():
                if feature["dtype"] == "video":
                    continue
                values = np.asarray(sliced[key].to_pylist(), dtype=np.float64).reshape(
                    frames, -1
                )
                all_values.setdefault(key, []).append(values)
                stats[key] = _stats(values)
            per_episode.append({"episode_index": episode, "stats": stats})
            offset += frames
            mode = (
                "FROZEN"
                if episode % 10 == 3
                else "DIM VIEW"
                if episode % 10 == 7
                else "NORMAL"
            )
            for camera in cameras:
                folder = dataset / "videos/chunk-000" / camera
                folder.mkdir(parents=True, exist_ok=True)
                overlay = Image.new("RGBA", (480, 300))
                draw = ImageDraw.Draw(overlay)
                draw.rectangle((0, 0, 480, 34), fill=(18, 30, 46, 255))
                draw.text(
                    (12, 11),
                    f"MOCK / EP {episode:02d} / {camera.split('.')[-1].upper()} / {mode}",
                    fill=(255, 193, 105, 255)
                    if mode != "NORMAL"
                    else (133, 218, 193, 255),
                )
                overlay.save(overlay_path)
                scene = (
                    "trim=end_frame=1,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=12"
                    if mode == "FROZEN"
                    else "eq=brightness=-0.25:saturation=0.2"
                    if mode == "DIM VIEW"
                    else "null"
                )
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-loglevel",
                        "error",
                        "-threads",
                        "1",
                        "-i",
                        str(base / "videos/chunk-000" / camera / "episode_000000.mp4"),
                        "-loop",
                        "1",
                        "-i",
                        str(overlay_path),
                        "-filter_complex",
                        f"[0:v]{scene}[scene];[scene][1:v]overlay=shortest=1",
                        "-t",
                        str(frames / FPS),
                        "-r",
                        str(FPS),
                        "-an",
                        "-c:v",
                        "libx264",
                        "-threads",
                        "1",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "27",
                        "-pix_fmt",
                        "yuv420p",
                        "-movflags",
                        "+faststart",
                        str(folder / f"episode_{episode:06d}.mp4"),
                    ],
                    check=True,
                )
            print(
                f"Mock Episode {episode + 1}/40 ({frames / FPS:.0f}s, {mode})",
                flush=True,
            )
    finally:
        overlay_path.unlink(missing_ok=True)
    stats = {
        key: _stats(np.concatenate(values, axis=0))
        for key, values in all_values.items()
    }
    (dataset / "meta/stats.json").write_text(json.dumps(stats))
    (dataset / "meta/episodes_stats.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in per_episode)
    )
    (dataset / "meta/episodes.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "episode_index": i,
                    "length": frames,
                    "tasks": ["将蓝色方块放入托盘（Mock 演示）"],
                },
                ensure_ascii=False,
            )
            + "\n"
            for i, frames in enumerate(lengths)
        )
    )
    info.update(
        total_episodes=40,
        total_frames=offset,
        total_videos=40 * len(cameras),
        splits={"train": "0:40"},
    )
    # Write the completion marker last, so an interrupted mock can be regenerated.
    (dataset / "meta/info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2)
    )


def _stats(values):
    return {
        "min": values.min(axis=0).tolist(),
        "max": values.max(axis=0).tolist(),
        "mean": values.mean(axis=0).tolist(),
        "std": values.std(axis=0).tolist(),
        "count": [len(values)],
    }
