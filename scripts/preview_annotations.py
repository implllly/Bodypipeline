#!/usr/bin/env python3
"""Run the real Server/Viewer with synthetic data and a server-owned task draft.

The pinned Viewer must already be prepared at --viewer-source (normal LeTools
setup, or a local cached copy). LeTools reads and exports the synthetic dataset;
no robot or paid AI request is used.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME = ROOT / ".runtime/annotation-preview"
FPS, FRAMES = 20, 240


def generate_dataset(dataset: Path):
    if (dataset / "meta/info.json").exists():
        return
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from PIL import Image, ImageDraw

    if not shutil.which("ffmpeg"):
        raise RuntimeError("Mock 视频生成需要 ffmpeg")
    (dataset / "meta").mkdir(parents=True, exist_ok=True)
    cameras = ["overhead", "side", "detail"]
    for camera_index, camera in enumerate(cameras):
        folder = dataset / "videos/chunk-000" / f"observation.images.{camera}"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "episode_000000.mp4"
        encoder = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                "480x300",
                "-r",
                str(FPS),
                "-i",
                "-",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ],
            stdin=subprocess.PIPE,
        )
        try:
            for frame in range(FRAMES):
                image = Image.new("RGB", (480, 300), (25 + camera_index * 5, 34, 46))
                draw = ImageDraw.Draw(image)
                draw.rounded_rectangle(
                    (18, 40, 462, 280),
                    radius=12,
                    fill=(49, 60, 72),
                    outline=(76, 90, 105),
                    width=2,
                )
                for x in range(35, 460, 25):
                    draw.line((x, 48, x, 272), fill=(55, 68, 82))
                for y in range(50, 280, 25):
                    draw.line((24, y, 456, y), fill=(55, 68, 82))
                draw.rounded_rectangle(
                    (320, 115, 433, 235),
                    radius=12,
                    fill=(30, 42, 54),
                    outline=(119, 148, 169),
                    width=3,
                )
                draw.text((343, 242), "TARGET TRAY", fill=(173, 192, 207))
                progress = max(0, min(1, (frame - 112) / 80))
                x = 155 + 215 * progress
                y = 175 - 17 * math.sin(progress * math.pi) + camera_index * 5
                if frame < 80:
                    x = 155 + (8 * math.sin(frame / 6) if frame > 40 else 0)
                draw.rounded_rectangle(
                    (x - 19, y - 18, x + 19, y + 18),
                    radius=3,
                    fill=(70, 151, 228),
                    outline=(145, 204, 255),
                    width=2,
                )
                grip_x = 90 + min(frame / 40, 1) * 65 if frame < 40 else x
                grip_y = 90 + min(frame / 40, 1) * 60
                if 45 < frame < 75:
                    grip_y -= (frame - 45) * 1.3
                draw.line(
                    (40, 55, grip_x - 43, grip_y - 45, grip_x, grip_y),
                    fill=(133, 146, 158),
                    width=20,
                )
                for xx, yy in [(40, 55), (grip_x - 43, grip_y - 45), (grip_x, grip_y)]:
                    draw.ellipse(
                        (xx - 10, yy - 10, xx + 10, yy + 10),
                        fill=(50, 62, 75),
                        outline=(180, 194, 207),
                        width=3,
                    )
                gap = 26 if frame < 38 or 55 < frame < 82 or frame > 218 else 21
                draw.line(
                    (grip_x - gap, grip_y, grip_x - gap, y + 15),
                    fill=(205, 215, 223),
                    width=5,
                )
                draw.line(
                    (grip_x + gap, grip_y, grip_x + gap, y + 15),
                    fill=(205, 215, 223),
                    width=5,
                )
                draw.text(
                    (18, 12),
                    f"MOCK DATA / {camera.upper()} / FRAME {frame:03d}",
                    fill=(158, 188, 218),
                )
                phase = (
                    "APPROACH"
                    if frame < 35
                    else "GRASP ATTEMPT 1"
                    if frame < 70
                    else "RETRY GRASP"
                    if frame < 115
                    else "MOVE"
                    if frame < 180
                    else "PLACE"
                )
                draw.text((300, 12), phase, fill=(128, 218, 190))
                assert encoder.stdin is not None
                encoder.stdin.write(image.tobytes())
        finally:
            if encoder.stdin:
                encoder.stdin.close()
            if encoder.wait() != 0:
                raise RuntimeError("Mock 视频编码失败")
        shutil.copy2(target, folder / "episode_000001.mp4")
    names = [f"axis_{i}" for i in range(6)]
    features = {
        "timestamp": {"dtype": "float32", "shape": [1], "names": None},
        "action": {"dtype": "float32", "shape": [6], "names": names},
        "observation.state": {"dtype": "float32", "shape": [6], "names": names},
        "frame_index": {"dtype": "int64", "shape": [1], "names": None},
        "episode_index": {"dtype": "int64", "shape": [1], "names": None},
        "task_index": {"dtype": "int64", "shape": [1], "names": None},
        "index": {"dtype": "int64", "shape": [1], "names": None},
    }
    for camera in cameras:
        features[f"observation.images.{camera}"] = {
            "dtype": "video",
            "shape": [300, 480, 3],
            "names": ["height", "width", "channel"],
            "info": {
                "video.fps": FPS,
                "video.codec": "h264",
                "video.pix_fmt": "yuv420p",
                "video.is_depth_map": False,
                "has_audio": False,
            },
        }
    folder = dataset / "data/chunk-000"
    folder.mkdir(parents=True, exist_ok=True)
    episode_stats, numeric_values = [], {}
    for episode in range(2):
        table = pa.table(
            {
                "timestamp": pa.array(np.arange(FRAMES) / FPS, type=pa.float32()),
                "frame_index": pa.array(range(FRAMES), type=pa.int64()),
                "episode_index": pa.array([episode] * FRAMES, type=pa.int64()),
                "task_index": pa.array([0] * FRAMES, type=pa.int64()),
                "index": pa.array(
                    range(episode * FRAMES, (episode + 1) * FRAMES), type=pa.int64()
                ),
                "action": pa.array(
                    [
                        [math.sin(frame / 26 + j) * 0.6 for j in range(6)]
                        for frame in range(FRAMES)
                    ],
                    type=pa.list_(pa.float32(), 6),
                ),
                "observation.state": pa.array(
                    [
                        [math.sin((frame - 2) / 26 + j) * 0.58 for j in range(6)]
                        for frame in range(FRAMES)
                    ],
                    type=pa.list_(pa.float32(), 6),
                ),
                "task": pa.array(["将蓝色方块放入托盘（Mock 演示）"] * FRAMES),
            }
        )
        pq.write_table(
            table, folder / f"episode_{episode:06d}.parquet", compression="NONE"
        )
        stats = {}
        for key, feature in features.items():
            if feature["dtype"] == "video":
                continue
            values = np.asarray(table[key].to_pylist(), dtype=np.float64).reshape(
                FRAMES, -1
            )
            numeric_values.setdefault(key, []).append(values)
            stats[key] = {
                "min": values.min(axis=0).tolist(),
                "max": values.max(axis=0).tolist(),
                "mean": values.mean(axis=0).tolist(),
                "std": values.std(axis=0).tolist(),
                "count": [FRAMES],
            }
        episode_stats.append({"episode_index": episode, "stats": stats})
    (dataset / "meta/episodes_stats.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in episode_stats)
    )
    stats = {}
    for key, chunks in numeric_values.items():
        values = np.concatenate(chunks, axis=0)
        stats[key] = {
            "min": values.min(axis=0).tolist(),
            "max": values.max(axis=0).tolist(),
            "mean": values.mean(axis=0).tolist(),
            "std": values.std(axis=0).tolist(),
            "count": [len(values)],
        }
    (dataset / "meta/stats.json").write_text(json.dumps(stats))
    info = {
        "codebase_version": "v2.1",
        "robot_type": "mock_demo",
        "total_episodes": 2,
        "total_frames": FRAMES * 2,
        "total_tasks": 1,
        "total_videos": 6,
        "total_chunks": 1,
        "chunks_size": 1000,
        "fps": FPS,
        "splits": {"train": "0:2"},
        "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
        "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
        "features": features,
    }
    (dataset / "meta/info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2)
    )
    (dataset / "meta/tasks.jsonl").write_text(
        json.dumps(
            {"task_index": 0, "task": "将蓝色方块放入托盘（Mock 演示）"},
            ensure_ascii=False,
        )
        + "\n"
    )
    (dataset / "meta/episodes.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "episode_index": i,
                    "tasks": ["将蓝色方块放入托盘（Mock 演示）"],
                    "length": FRAMES,
                },
                ensure_ascii=False,
            )
            + "\n"
            for i in range(2)
        )
    )


def demo_bootstrap(source: Path, dataset_id: str):
    """Seed only this explicitly synthetic dataset; never overwrite browser edits."""
    template = {
        "id": "mock-cube-task",
        "name": "方块搬运 · 示例任务",
        "subtasks": [
            {"id": f"step-{i + 1}", "name": name}
            for i, name in enumerate(["接近方块", "抓取方块", "移动到托盘", "放下方块"])
        ],
        "fields": [
            {
                "id": "episode-quality",
                "label": "任务质量",
                "scope": "episode",
                "kind": "rating",
            },
            {
                "id": "episode-comment",
                "label": "整体评价",
                "scope": "episode",
                "kind": "textarea",
            },
            {
                "id": "attempt-outcome",
                "label": "执行结果",
                "scope": "attempt",
                "kind": "single",
                "options": ["成功", "失败", "无法判断"],
            },
            {
                "id": "attempt-quality",
                "label": "执行质量",
                "scope": "attempt",
                "kind": "rating",
            },
            {
                "id": "attempt-comment",
                "label": "片段备注",
                "scope": "attempt",
                "kind": "textarea",
            },
            {
                "id": "frame-comment",
                "label": "异常说明",
                "scope": "frame",
                "kind": "textarea",
            },
        ],
    }
    doc = {
        "version": 1,
        "templateId": template["id"],
        "templateSnapshot": template,
        "values": {
            "episode-quality": 4,
            "episode-comment": "示例：任务最终完成，抓取时有一次失败，重试后成功。",
        },
        "attempts": [],
        "frames": [
            {
                "id": "demo-frame",
                "frame": 55,
                "values": {"frame-comment": "示例：第一次抓取时，方块从夹爪中滑落。"},
            }
        ],
    }
    for i, (step, start, end, success) in enumerate(
        [
            (0, 0, 35, True),
            (1, 35, 70, False),
            (1, 80, 115, True),
            (2, 115, 180, True),
            (3, 180, 240, True),
        ]
    ):
        doc["attempts"].append(
            {
                "id": f"demo-attempt-{i}",
                "subtaskId": template["subtasks"][step]["id"],
                "subtaskName": template["subtasks"][step]["name"],
                "start": start,
                "end": end,
                "values": {
                    "attempt-outcome": "成功" if success else "失败",
                    "attempt-quality": 4 if success else 2,
                    "attempt-comment": "示例：夹爪未夹稳，需要再次尝试。"
                    if not success
                    else "",
                },
            }
        )
    payload = json.dumps(
        {"template": template, "doc": doc, "dataset": f"bp/{dataset_id}"},
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>打开 Mock 标注预览</title><body><p>正在打开 Mock 标注预览…</p><script>
const fixture = FIXTURE;
try {
  const key = 'bodypipeline:task-templates:v1';
  const templates = JSON.parse(localStorage.getItem(key) || '[]');
  if (!templates.some(t => t.id === fixture.template.id)) { templates.push(fixture.template); localStorage.setItem(key, JSON.stringify(templates)); }
  const binding = 'bodypipeline:task-binding:v1:' + encodeURIComponent(fixture.dataset);
  if (!localStorage.getItem(binding)) localStorage.setItem(binding, fixture.template.id);
  const docKey = 'bodypipeline:task-annotations:v1:' + encodeURIComponent(fixture.dataset) + ':0:' + encodeURIComponent(fixture.template.id);
  if (!localStorage.getItem(docKey)) localStorage.setItem(docKey, JSON.stringify(fixture.doc));
  sessionStorage.setItem('activeTab', 'annotations');
  location.replace('/' + fixture.dataset + '/episode_0');
} catch (e) { document.querySelector('p').textContent = '浏览器存储不可用：' + e.message; }
</script></body></html>""".replace("FIXTURE", payload)
    (source / "public").mkdir(exist_ok=True)
    (source / "public/annotation-demo.html").write_text(html)
    return template, doc


def run_role(role: str):
    import uvicorn

    from pipeline.shared.config import Settings

    settings = Settings.load(role, str(RUNTIME / "server.json"))
    if role == "viewer":
        from pipeline.server.viewer import create_gateway

        app, port = create_gateway(settings), 18082
    elif role == "server":
        from pipeline.server.app import create_app

        app, port = create_app(settings), 18081
        # This local supervisor owns Next; do not start a second Viewer or invoke
        # production toolchain recovery. All normal Server routes stay intact.
        for method in ("recover", "autostart", "poll", "stop"):
            setattr(app.state.viewer, method, lambda: None)
    else:
        from pipeline.client.app import create_app

        settings = Settings.load("client", str(RUNTIME / "client.json"))
        app, port = create_app(settings), 18080
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["server", "viewer", "client"])
    parser.add_argument("--viewer-source", type=Path, default=RUNTIME / "viewer/source")
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.role:
        run_role(args.role)
        return
    source = args.viewer_source.resolve()
    if not (source / "node_modules/next/dist/bin/next").exists():
        parser.error("请先准备固定版本 Viewer 源码与依赖，然后传入 --viewer-source")
    for port in (18080, 18081, 18082, 13100):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                parser.error(f"端口 {port} 已被占用，请先停止先前的本地预览")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    config_path = RUNTIME / "server.json"
    if not config_path.exists():
        config = json.loads((ROOT / "configs/server.demo.json").read_text())
        config.update(
            root=str(RUNTIME / "server"),
            import_roots=[str(RUNTIME)],
            token=secrets.token_urlsafe(32),
            device_token=secrets.token_urlsafe(32),
            login_password="annotation-demo",
            viewer_origin="http://127.0.0.1:18082",
            viewer_ui_port=13100,
            viewer_ttl=7 * 24 * 3600,
            auto_process=False,
        )
        descriptor = os.open(config_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(config, handle, indent=2)
    config = json.loads(config_path.read_text())
    tool = ROOT / ".runtime/letools-venv/bin/letools"
    if tool.is_file():
        config["letools_bin"] = str(tool)
        config_path.write_text(json.dumps(config, indent=2))
    client = json.loads((ROOT / "configs/client.mock.json").read_text())
    client.update(
        root=str(RUNTIME / "client"),
        server_url="http://127.0.0.1:18081",
        token=config["token"],
        device_token=config["device_token"],
        login_password=config["login_password"],
    )
    client_path = RUNTIME / "client.json"
    client_path.write_text(json.dumps(client, indent=2))
    client_path.chmod(0o600)
    dataset = RUNTIME / "datasets/mock-annotation-demo-v2"
    generate_dataset(dataset)
    from pipeline.server.app import create_app
    from pipeline.server.subtask_review import DraftRequest, SubtaskReview
    from pipeline.server.task_annotations import (
        TemplateLibrary,
        TemplateRequest,
        document_segments,
    )
    from pipeline.server.viewer_bootstrap import (
        patch_cache,
        patch_channel_charts,
        patch_presentation,
    )
    from pipeline.shared.config import Settings

    # Restore this demo's font substitution before checking production patches.
    layout = source / "src/app/layout.tsx"
    text = layout.read_text()
    standin = 'const inter = { className: "pipeline-font", variable: "" };'
    if standin in text:
        definition = (
            'const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });'
            if "inter.variable" in text
            else 'const inter = Inter({ subsets: ["latin"] });'
        )
        text = text.replace(standin, definition)
        if 'import { Inter } from "next/font/google";' not in text:
            text = text.replace(
                'import type { Metadata } from "next";',
                'import type { Metadata } from "next";\nimport { Inter } from "next/font/google";',
            )
        layout.write_text(text)
    patch_cache(source.parent)
    patch_channel_charts(source.parent)
    patch_presentation(source.parent)
    settings = Settings.load("server", str(config_path))
    app = create_app(settings)
    imported = app.state.imports.import_path(str(dataset))
    dataset_id = imported["job_id"]
    app.state.viewer.registry.register(dataset_id, dataset)
    app.state.ingest.store.put(
        "viewer",
        "service",
        {"id": "service", "state": "READY", "mode": "local-mock-preview"},
    )
    template, document = demo_bootstrap(source, dataset_id)
    review = SubtaskReview(settings, app.state.ingest.store)
    initial = review.episode(dataset_id, 0)
    if not initial["has_draft"]:
        review.save(
            dataset_id,
            0,
            DraftRequest.model_validate(
                {
                    "revision": initial["revision"],
                    "source_signature": initial["source_signature"],
                    "segments": document_segments(document),
                    "task_document": document,
                }
            ),
        )
    library = TemplateLibrary(app.state.ingest.store)
    catalog = library.read()
    if not any(t["id"] == template["id"] for t in catalog["templates"]):
        library.save(
            TemplateRequest.model_validate(
                {
                    "revision": catalog["revision"],
                    "templates": [*catalog["templates"], template],
                }
            )
        )
    env = {
        **os.environ,
        "DATASET_URL": "http://127.0.0.1:18082/_data",
        "NEXT_TELEMETRY_DISABLED": "1",
        "NEXT_PUBLIC_LEROBOT_DOCTOR_URL": "http://127.0.0.1:18082/_data/doctor",
    }
    env.pop("NEXT_PUBLIC_ANNOTATE_BACKEND_URL", None)
    # Development preview uses the OS font stack; no font downloads required.
    layout = source / "src/app/layout.tsx"
    text = layout.read_text().replace('import { Inter } from "next/font/google";', "")
    text = text.replace(
        'const inter = Inter({ subsets: ["latin"] });',
        'const inter = { className: "pipeline-font", variable: "" };',
    )
    text = text.replace(
        'const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });',
        'const inter = { className: "pipeline-font", variable: "" };',
    )
    layout.write_text(text)
    logs = RUNTIME / "logs"
    logs.mkdir(exist_ok=True)
    children, handles = [], []
    try:
        commands = [
            (
                "viewer-ui",
                [
                    args.node,
                    "node_modules/next/dist/bin/next",
                    "dev",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    "13100",
                ],
                source,
            )
        ]
        commands += [
            (
                role,
                [sys.executable, str(Path(__file__).resolve()), "--role", role],
                ROOT,
            )
            for role in ("server", "viewer", "client")
        ]
        for name, command, cwd in commands:
            handle = (logs / f"{name}.log").open("ab")
            handles.append(handle)
            children.append(
                subprocess.Popen(
                    command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT
                )
            )
        print(
            "Mock Client: http://127.0.0.1:18080\nServer: http://127.0.0.1:18081\n标注预览: http://127.0.0.1:18082/annotation-demo.html\n本地演示登录密码: annotation-demo\nCtrl-C 停止本次预览。",
            flush=True,
        )
        (RUNTIME / "preview.json").write_text(
            json.dumps(
                {
                    "url": "http://127.0.0.1:18082/annotation-demo.html",
                    "dataset_id": dataset_id,
                    "viewer_url": f"http://127.0.0.1:18082/bp/{dataset_id}/episode_0",
                    "pids": [p.pid for p in children],
                },
                indent=2,
            )
        )
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
        raise RuntimeError(f"预览进程退出，请查看 {logs}")
    except KeyboardInterrupt:
        pass
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
