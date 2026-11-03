#!/usr/bin/env python3
"""Preview template-bound AI with synthetic videos and an offline model fixture.

Run with the API environment (including pyarrow) and --viewer-source pointing at
an already prepared Viewer source. The mock toolchain uses no API or real key.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime/template-ai-preview"
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer-source", type=Path)
    parser.add_argument("--role", choices=["server", "viewer"])
    args = parser.parse_args()
    os.chdir(ROOT)
    from pipeline.shared.config import Settings

    config_path = RUNTIME / "server.json"
    if args.role:
        import uvicorn

        settings = Settings.load(args.role, str(config_path))
        if args.role == "server":
            from pipeline.server.app import create_app

            os.environ["PIPELINE_TEMPLATE_DEMO_KEY"] = "offline-mock-no-real-key"
            app, port = create_app(settings), 18091
            for method in ("recover", "autostart", "poll", "stop"):
                setattr(app.state.viewer, method, lambda: None)
            app.state.viewer.ensure_service = lambda: {"state": "READY"}
        else:
            from pipeline.server.viewer import create_gateway

            app, port = create_gateway(settings), 18092
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
        return
    if (
        not args.viewer_source
        or not (args.viewer_source / "node_modules/next/dist/bin/next").exists()
    ):
        parser.error("请用 --viewer-source 指向已经安装依赖的固定版本 Viewer source")
    for port in (18091, 18092, 13110):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                parser.error(f"端口 {port} 已被占用")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    source = RUNTIME / "viewer/source"
    if not source.exists():
        shutil.copytree(
            args.viewer_source,
            source,
            ignore=shutil.ignore_patterns("node_modules", ".next", ".git"),
        )
        (source / "node_modules").symlink_to(
            (args.viewer_source / "node_modules").resolve()
        )
    # This is a cached preview source with the existing annotation/batch patches.
    # Use the system font so the local demo needs no external font download.
    layout = source / "src/app/layout.tsx"
    text = layout.read_text().replace('import { Inter } from "next/font/google";', "")
    for definition in (
        'const inter = Inter({ subsets: ["latin"] });',
        'const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });',
    ):
        text = text.replace(
            definition, 'const inter = { className: "pipeline-font", variable: "" };'
        )
    layout.write_text(text)
    shutil.copyfile(
        ROOT / "pipeline/server/viewer_ui/annotation-review-client.ts",
        source / "src/utils/annotation-review-client.ts",
    )
    for name, destination in [
        ("task-review-client.ts", "src/utils/task-review-client.ts"),
        ("task-annotation-studio.tsx", "src/components/task-annotation-studio.tsx"),
    ]:
        shutil.copyfile(ROOT / "pipeline/server/viewer_ui" / name, source / destination)
    from scripts.preview_annotations import generate_dataset

    dataset = RUNTIME / "datasets/mock-template-ai"
    generate_dataset(dataset)
    mock = RUNTIME / "offline-argus"
    for name in ("review", "label"):
        (mock / name).mkdir(parents=True, exist_ok=True)
        (mock / name / "__init__.py").touch()
    shutil.copyfile(
        ROOT / "tests/fixtures/mock_argus_review.py", mock / "review/__main__.py"
    )
    for name in ("episode.py", "harness.py", "pipeline_template.py"):
        (mock / "label" / name).write_text(
            "# pipeline-argus-task-template:v1\n# pipeline-argus-template-retry:v1\n"
        )
    config = (
        json.loads(config_path.read_text())
        if config_path.exists()
        else json.loads((ROOT / "configs/server.demo.json").read_text())
    )
    config.update(
        root=str(RUNTIME / "server"),
        import_roots=[str(RUNTIME)],
        token=config.get("token") or secrets.token_urlsafe(32),
        device_token=config.get("device_token") or secrets.token_urlsafe(32),
        login_password="annotation-demo",
        auto_process=False,
        min_free_bytes=0,
        server_url="http://127.0.0.1:18091",
        viewer_origin="http://127.0.0.1:18092",
        viewer_ui_port=13110,
        viewer_ttl=7 * 86400,
        letools_bin=str(ROOT / ".runtime/letools-venv/bin/letools"),
        argus_src=str(mock),
        argus_python=sys.executable,
        argus_api_key_env="PIPELINE_TEMPLATE_DEMO_KEY",
        argus_model="mock/template-demo",
        argus_models=["mock/template-demo"],
        argus_prices={"mock/template-demo": [[0, 0, 0]]},
    )
    config_path.write_text(json.dumps(config, indent=2))
    config_path.chmod(0o600)
    from pipeline.server.app import create_app
    from pipeline.server.task_annotations import TemplateLibrary, TemplateRequest

    app = create_app(Settings.load("server", str(config_path)), background=False)
    imported = app.state.imports.import_path(str(dataset))
    app.state.viewer.registry.register(imported["job_id"], dataset)
    app.state.ingest.store.put(
        "viewer",
        "service",
        {"id": "service", "state": "READY", "mode": "offline-template-demo"},
    )
    template = {
        "id": "mock-cup-template",
        "name": "MOCK · 抓取与放置",
        "subtasks": [{"id": "grasp", "name": "抓取"}, {"id": "place", "name": "放置"}],
        "fields": [
            {
                "id": "quality",
                "label": "整体质量",
                "kind": "rating",
                "scope": "episode",
            },
            {
                "id": "result",
                "label": "执行结果",
                "kind": "single",
                "scope": "attempt",
                "options": ["失败", "成功"],
            },
            {"id": "score", "label": "执行质量", "kind": "rating", "scope": "attempt"},
            {
                "id": "comment",
                "label": "片段备注",
                "kind": "textarea",
                "scope": "attempt",
            },
            {"id": "note", "label": "异常说明", "kind": "textarea", "scope": "frame"},
        ],
    }
    library = TemplateLibrary(app.state.ingest.store)
    current = library.read()
    if not any(t["id"] == template["id"] for t in current["templates"]):
        library.save(
            TemplateRequest(
                revision=current["revision"],
                templates=[*current["templates"], template],
            )
        )
    logs = RUNTIME / "logs"
    logs.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "DATASET_URL": "http://127.0.0.1:18092/_data",
        "NEXT_TELEMETRY_DISABLED": "1",
        "NEXT_PUBLIC_LEROBOT_DOCTOR_URL": "http://127.0.0.1:18092/_data/doctor",
    }
    env.pop("NEXT_PUBLIC_ANNOTATE_BACKEND_URL", None)
    commands = [
        (
            "viewer-ui",
            [
                "node",
                "node_modules/next/dist/bin/next",
                "dev",
                "--hostname",
                "127.0.0.1",
                "--port",
                "13110",
            ],
            source,
        )
    ]
    commands += [
        (role, [sys.executable, str(Path(__file__).resolve()), "--role", role], ROOT)
        for role in ("server", "viewer")
    ]
    children, handles = [], []
    try:
        for name, command, cwd in commands:
            handle = (logs / f"{name}.log").open("ab")
            handles.append(handle)
            children.append(
                subprocess.Popen(
                    command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT
                )
            )
        (RUNTIME / "preview.json").write_text(
            json.dumps(
                {
                    "url": "http://127.0.0.1:18091",
                    "dataset_id": imported["job_id"],
                    "pids": [p.pid for p in children],
                }
            )
        )
        print(
            "模板 AI Mock：http://127.0.0.1:18091\n密码：annotation-demo\n选择「自动标注」提交模板任务；全部模型结果为离线合成，费用为 0。",
            flush=True,
        )
        for child in children:
            child.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    main()
