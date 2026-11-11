"""Read-only LeRobot v3 delivery validator, run in the LeTools Python env."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path, PurePosixPath


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(batch: Path, delivery: dict):
    import av
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    root = batch / "lerobot"

    def read_json(name):
        require("lerobot/" + name in declared, f"必需元数据未列入清单：{name}")
        value = json.loads((root / name).read_text())
        require(isinstance(value, dict), f"{name} 必须为对象")
        return value

    declared = {item["path"] for item in delivery["files"]}

    def locate(template, **values):
        require(isinstance(template, str), "缺少路径模板")
        name = template.format(**values)
        require(
            not PurePosixPath(name).is_absolute()
            and "\\" not in name
            and all(p not in ("", ".", "..") for p in name.split("/"))
            and "lerobot/" + name in declared,
            f"路径不在交付清单中：{name}",
        )
        return root / name

    def integer(value, label, minimum=0):
        require(
            type(value) is int and value >= minimum, f"{label} 必须为整数且 ≥ {minimum}"
        )
        return value

    info, platform, stats = (
        read_json("meta/info.json"),
        read_json("meta/platform.json"),
        read_json("meta/stats.json"),
    )
    for key in (
        "robot_type",
        "total_episodes",
        "total_frames",
        "fps",
        "codebase_version",
    ):
        require(info.get(key) == delivery[key], f"info.json 的 {key} 与交付不一致")
    require(
        platform.get("robot_type") == delivery["robot_type"],
        "platform.robot_type 不匹配",
    )
    require(
        platform.get("feature_schema_id") == delivery["feature_schema_id"],
        "platform.feature_schema_id 不匹配",
    )
    require(bool(stats), "stats.json 不能为空")
    frames, episodes, fps = (
        delivery["total_frames"],
        delivery["total_episodes"],
        delivery["fps"],
    )
    features = info.get("features")
    require(isinstance(features, dict), "缺少 features")
    required = {
        "index",
        "frame_index",
        "episode_index",
        "timestamp",
        "task_index",
        "observation.state",
        "action",
    }
    require(required <= features.keys(), "缺少基础状态/动作/索引字段")
    for key in required - {"observation.state", "action", "timestamp"}:
        require(
            features[key].get("dtype") == "int64" and features[key].get("shape") == [1],
            f"{key} 需声明 int64 [1]",
        )
    require(
        features["timestamp"].get("dtype") == "float32"
        and features["timestamp"].get("shape") == [1],
        "timestamp 需声明 float32 [1]",
    )
    cameras, numeric = {}, {}
    for key, feature in features.items():
        require(isinstance(feature, dict), f"字段定义无效：{key}")
        shape = feature.get("shape")
        require(
            isinstance(shape, list)
            and shape
            and all(type(n) is int and n > 0 for n in shape),
            f"字段形状无效：{key}",
        )
        if feature.get("dtype") == "video":
            require(
                key.startswith("observation.images.")
                and len(shape) == 3
                and shape[2] == 3,
                f"需要 RGB 视频：{key}",
            )
            cameras[key] = feature
        elif feature.get("dtype") in {"float32", "float64", "int64", "int32", "bool"}:
            require(len(shape) == 1, f"数值字段仅支持标量/固定向量：{key}")
            if shape[0] > 1:
                names = feature.get("names")
                require(
                    isinstance(names, list)
                    and len(names) == shape[0]
                    and all(isinstance(n, str) and n for n in names)
                    and len(set(names)) == len(names),
                    f"缺少逐维名称：{key}",
                )
            numeric[key] = feature
    for key in ("observation.state", "action"):
        require(
            features[key].get("dtype") == "float32" and key in numeric,
            f"{key} 必须为 float32 固定向量",
        )
        require(key in stats and isinstance(stats[key], dict), f"stats 缺少 {key}")
        require(stats[key].get("count") == [frames], f"stats 的 {key} count 不匹配")
        for statistic in ("min", "max", "mean", "std"):
            values = stats[key].get(statistic)
            require(
                isinstance(values, list)
                and len(values) == features[key]["shape"][0]
                and bool(np.isfinite(np.asarray(values, dtype=float)).all()),
                f"stats 的 {key}/{statistic} 无效",
            )
        names = features[key].get("names")
        require(
            isinstance(names, list) and len(names) == features[key]["shape"][0],
            f"{key} 缺少逐维名称",
        )
    require(cameras, "至少需要一路 RGB 视频")
    if "observation.wrist_wrench" in features:
        require(
            features["observation.wrist_wrench"].get("shape") == [6]
            and features["observation.wrist_wrench"].get("names")
            == ["Fx_N", "Fy_N", "Fz_N", "Tx_Nm", "Ty_Nm", "Tz_Nm"],
            "六维力维度或分量名称不符合契约",
        )
        require("observation.wrist_wrench_valid" in features, "六维力缺少有效标志")
    splits = info.get("splits")
    require(isinstance(splits, dict) and splits, "缺少 splits")
    for split in splits.values():
        require(
            isinstance(split, str) and split.count(":") == 1, "split 必须为 start:end"
        )
        start, end = map(int, split.split(":"))
        require(0 <= start < end <= episodes, "split episode 区间越界")

    require("lerobot/meta/tasks.parquet" in declared, "缺少 tasks.parquet 清单")
    task_rows = pq.read_table(root / "meta/tasks.parquet").to_pylist()
    tasks = {}
    for task in task_rows:
        index = integer(task.get("task_index"), "task_index")
        text = task.get("task", task.get("__index_level_0__"))
        require(
            index not in tasks and isinstance(text, str) and text.strip(),
            "任务索引重复或文本为空",
        )
        tasks[index] = text
    require(
        len(tasks) == integer(info.get("total_tasks"), "total_tasks", 1),
        "任务数量不匹配",
    )
    episode_files = sorted((root / "meta/episodes").glob("chunk-*/*.parquet"))
    require(episode_files, "缺少 v3 episode 元数据")
    rows = [row for path in episode_files for row in pq.read_table(path).to_pylist()]
    require(len(rows) == episodes, "episode 元数据数量不匹配")
    rows.sort(key=lambda row: integer(row.get("episode_index"), "episode_index"))
    data_files, video_ranges, offset = {}, {}, 0
    for index, row in enumerate(rows):
        require(row["episode_index"] == index, "episode_index 不连续或重复")
        length = integer(row.get("length"), "length", 1)
        start, end = row.get("dataset_from_index"), row.get("dataset_to_index")
        require(
            type(start) is int
            and type(end) is int
            and start == offset
            and end - start == length,
            "episode 行区间不连续或 length 不匹配",
        )
        offset = end
        path = locate(
            info.get("data_path"),
            chunk_index=integer(row.get("data/chunk_index"), "data/chunk_index"),
            file_index=integer(row.get("data/file_index"), "data/file_index"),
        )
        data_files.setdefault(path, []).append(row)
        require(
            isinstance(row.get("tasks"), list)
            and row["tasks"]
            and all(t in tasks.values() for t in row["tasks"]),
            "episode tasks 无法解析",
        )
        for key in cameras:
            prefix = "videos/" + key + "/"
            video = locate(
                info.get("video_path"),
                video_key=key,
                chunk_index=integer(
                    row.get(prefix + "chunk_index"), "video chunk_index"
                ),
                file_index=integer(row.get(prefix + "file_index"), "video file_index"),
            )
            a, b = row.get(prefix + "from_timestamp"), row.get(prefix + "to_timestamp")
            require(
                type(a) in (int, float)
                and type(b) in (int, float)
                and math.isfinite(a)
                and math.isfinite(b)
                and 0 <= a < b,
                "视频时间区间无效",
            )
            require(abs((b - a) * fps - length) <= 0.1, "视频区间时长与帧数不匹配")
            video_ranges.setdefault((video, key), []).append((a, b, length))
    require(offset == frames, "episode length 总和不等于 total_frames")
    require(
        set(root.glob("data/chunk-*/*.parquet")) == set(data_files),
        "存在未索引的数据分片",
    )
    seen = 0
    dtype_map = {
        "float32": pa.float32(),
        "float64": pa.float64(),
        "int64": pa.int64(),
        "int32": pa.int32(),
        "bool": pa.bool_(),
    }
    for path, segments in data_files.items():
        parquet = pq.ParquetFile(path)
        require(
            parquet.metadata.num_rows == sum(r["length"] for r in segments),
            "数据分片行数不匹配",
        )
        for key, feature in numeric.items():
            require(key in parquet.schema_arrow.names, f"Parquet 缺少字段：{key}")
            dtype = parquet.schema_arrow.field(key).type
            vector = pa.types.is_list(dtype) or pa.types.is_fixed_size_list(dtype)
            if key in (
                "index",
                "frame_index",
                "episode_index",
                "task_index",
                "timestamp",
            ):
                require(not vector, f"{key} 必须为标量")
            if key in ("observation.state", "action"):
                require(vector, f"{key} 必须为向量")
            if pa.types.is_list(dtype) or pa.types.is_fixed_size_list(dtype):
                dtype = dtype.value_type
            require(dtype == dtype_map[feature["dtype"]], f"Parquet dtype 不符：{key}")
        segment_index = 0
        for group in parquet.iter_batches(batch_size=4096, columns=list(numeric)):
            for row in group.to_pylist():
                while seen >= segments[segment_index]["dataset_to_index"]:
                    segment_index += 1
                ep = segments[segment_index]
                require(
                    type(row["index"]) is int and row["index"] == seen,
                    "全局 index 不连续",
                )
                require(
                    row["episode_index"] == ep["episode_index"]
                    and row["frame_index"] == seen - ep["dataset_from_index"],
                    "帧/episode 索引不匹配",
                )
                require(
                    row["task_index"] in tasks
                    and tasks[row["task_index"]] in ep["tasks"],
                    "逐帧 task_index 无法解析",
                )
                require(
                    abs(row["timestamp"] - row["frame_index"] / fps)
                    <= max(
                        1e-4,
                        abs(float(np.spacing(np.float32(row["frame_index"] / fps))))
                        * 2,
                    ),
                    "timestamp 与固定采样时钟不匹配",
                )
                for key, feature in numeric.items():
                    value = row[key]
                    vector = isinstance(value, list)
                    require(
                        value is not None
                        and (
                            len(value) == feature["shape"][0]
                            if vector
                            else feature["shape"] == [1]
                        ),
                        f"数值为空或维度不符：{key}",
                    )
                    require(
                        not vector or all(v is not None for v in value),
                        f"向量含空值：{key}",
                    )
                    require(
                        bool(np.isfinite(np.asarray(value, dtype=float)).all()),
                        f"数值包含 NaN/Inf/空值：{key}",
                    )
                if "observation.wrist_wrench_valid" in row:
                    valid = row["observation.wrist_wrench_valid"]
                    require(valid in (0, 1), "六维力有效标志必须为 0/1")
                seen += 1
    require(seen == frames, "实际数据总帧数不匹配")
    require(
        set(root.glob("videos/*/chunk-*/*.mp4")) == {p for p, _ in video_ranges},
        "存在未索引的视频文件",
    )
    for (path, key), ranges in video_ranges.items():
        shape = cameras[key]["shape"]
        ranges.sort()
        cursor = 0.0
        for start, end, _ in ranges:
            require(abs(start - cursor) * fps <= 0.1, "同文件视频区间重叠或有空洞")
            cursor = end
        with av.open(str(path)) as container:
            require(
                len(container.streams.video) == 1 and not container.streams.audio,
                "视频需单视频流且无音频",
            )
            stream = container.streams.video[0]
            require(
                stream.codec_context.name == "h264"
                and stream.codec_context.format.name == "yuv420p",
                "需要 H.264 yuv420p 视频",
            )
            require(
                stream.width == shape[1] and stream.height == shape[0],
                "视频分辨率与 features 不符",
            )
            require(
                stream.average_rate and abs(float(stream.average_rate) - fps) < 1e-4,
                "视频 fps 不匹配",
            )
            count = 0
            for frame in container.decode(stream):
                require(
                    frame.pts is not None
                    and abs(float(frame.pts * frame.time_base) * fps - count) <= 0.1,
                    "视频帧时间戳不连续",
                )
                count += 1
            require(count == sum(n for _, _, n in ranges), "解码视频帧数不匹配")
    return {"episodes": episodes, "frames": frames, "video_files": len(video_ranges)}


if __name__ == "__main__":
    try:
        print(json.dumps(validate(Path(sys.argv[1]), json.load(sys.stdin))))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
