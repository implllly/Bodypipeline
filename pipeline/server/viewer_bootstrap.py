"""Run the pinned Visualizer once, independent of any single dataset.

``letools visualizer serve`` binds the application to one dataset and its own
data server. BodyPipeline instead keeps a single Visualizer alive and serves
every dataset through the gateway, so this bootstrap only prepares the pinned,
patched source and starts the Next.js application with the gateway as its data
origin.

The pinned Visualizer reads ``process.env.DATASET_URL`` in client code, but its
Next config omits this variable from the browser bundle. Exposing ONLY this
public origin prevents local datasets from being requested from the Hub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

UPSTREAM = "dc59887796fd41f37040c0df6b10e6f6a30a1854"
MARKER = "// embodied-pipeline: expose only the public dataset origin"
BUILD_STAMP = ".pipeline-build.json"
CHANNEL_PATCH = Path(__file__).with_name("viewer_channels.patch")
PRESENTATION_PATCH = Path(__file__).with_name("viewer_presentation.patch")
CAMERA_ORDER_PATCH = Path(__file__).with_name("viewer_camera_order.patch")
SCORE_FILTER_PATCH = Path(__file__).with_name("viewer_score_filter.patch")
UI_ASSETS = Path(__file__).with_name("viewer_ui")
THEME_SCRIPT = Path(__file__).parents[1] / "web/theme.js"


def patch_cache(cache: Path):
    metadata = json.loads((cache / ".letools-source.json").read_text())
    if metadata.get("upstream_commit") != UPSTREAM:
        raise RuntimeError(
            "Visualizer upstream changed: integration patch must be reviewed again"
        )
    path = cache / "source/next.config.ts"
    text = path.read_text()
    if MARKER not in text:
        anchor = "const nextConfig: NextConfig = {\n"
        if text.count(anchor) != 1 or "env:" in text:
            raise RuntimeError(
                "Unexpected Next config; refusing to apply integration patch"
            )
        text = text.replace(
            anchor,
            anchor
            + "  "
            + MARKER
            + '\n  env: { DATASET_URL: process.env.DATASET_URL || "" },\n',
        )
        path.write_text(text)
    (cache / ".pipeline-integration.json").write_text(
        json.dumps(
            {
                "upstream": UPSTREAM,
                "patch": "public-dataset-origin-v1",
                "next_config_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            indent=2,
        )
    )


def patch_channel_charts(cache: Path):
    """Apply the single-channel layout after letools prepares upstream patches."""

    source = cache / "source"
    patch = CHANNEL_PATCH.read_text()
    chart = (source / "src/components/data-recharts.tsx").read_text()
    if "// bodypipeline: signal explorer v1" in chart:
        # The presentation replaces the stock chart after this base patch.
        # Still check/apply channel grouping on every startup.
        patch = (
            "diff --git a/src/utils/dataProcessing.ts"
            + patch.split("diff --git a/src/utils/dataProcessing.ts", 1)[1]
        )
    # The optional Chinese theme translates the button this patch removes.
    if '{expanded ? "分图显示" : "合并显示"}' in chart:
        patch = patch.replace(
            '{expanded ? "Split charts" : "Combine all"}',
            '{expanded ? "分图显示" : "合并显示"}',
        )
    command = ["git", "apply", "--unsafe-paths", "--directory", str(source)]
    for flags in (["--check"], ["--reverse", "--check"]):
        result = subprocess.run(
            command + flags,
            input=patch,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            if "--reverse" not in flags:
                subprocess.run(command, input=patch, text=True, check=True)
            return
    raise RuntimeError(
        "Unexpected Visualizer source; cannot apply the single-channel chart layout"
    )


def patch_presentation(cache: Path):
    """Install the signal explorer, camera layout and shared theme assets."""

    source = cache / "source"
    patch = PRESENTATION_PATCH.read_text()
    layout = (source / "src/app/layout.tsx").read_text()
    if 'lang="en"' in layout:
        # Context lines differ when the optional Chinese base theme is absent.
        for chinese, upstream in {
            'lang="zh-CN"': 'lang="en"',
            '{renderTab("episodes", "Episode")}': '{renderTab("episodes", "Episodes")}',
            "数据集质量诊断（由 lerobot-doctor 提供）": "Dataset quality diagnostics (powered by lerobot-doctor)",
            "语言指令": "Language Instruction",
            "后退 5 秒": "Jump backward 5 seconds",
            "className={`${inter.variable} pipeline-font`}": "className={inter.className}",
        }.items():
            patch = patch.replace(chinese, upstream)
    command = ["git", "apply", "--unsafe-paths", "--directory", str(source)]
    for flags in (["--check"], ["--reverse", "--check"]):
        result = subprocess.run(
            command + flags, input=patch, text=True, capture_output=True
        )
        if result.returncode == 0:
            if "--reverse" not in flags:
                subprocess.run(command, input=patch, text=True, check=True)
            break
    else:
        raise RuntimeError("Unexpected Visualizer source; cannot apply presentation")
    camera_patch = CAMERA_ORDER_PATCH.read_text()
    for flags in (["--check"], ["--reverse", "--check"]):
        result = subprocess.run(
            command + flags, input=camera_patch, text=True, capture_output=True
        )
        if result.returncode == 0:
            if "--reverse" not in flags:
                subprocess.run(command, input=camera_patch, text=True, check=True)
            break
    else:
        raise RuntimeError("Unexpected Visualizer source; cannot apply camera order")
    patch_score_filter(cache)
    patch_subtask_review(cache)
    destinations = {
        "data-recharts.tsx": "src/components/data-recharts.tsx",
        "theme-toggle.tsx": "src/components/theme-toggle.tsx",
        "chart-model.ts": "src/utils/chart-model.ts",
        "viewer-ui.css": "src/app/viewer-ui.css",
        "side-nav.tsx": "src/components/side-nav.tsx",
        "episode-scores.tsx": "src/components/episode-scores.tsx",
        "episode-score-filter.tsx": "src/components/episode-score-filter.tsx",
        "episode-score-filter-model.ts": "src/utils/episode-score-filter-model.ts",
        "subtask-review-model.ts": "src/utils/subtask-review-model.ts",
        "annotation-review-client.ts": "src/utils/annotation-review-client.ts",
        "use-annotation-review.ts": "src/utils/use-annotation-review.ts",
    }
    for name, target in destinations.items():
        (source / target).write_bytes((UI_ASSETS / name).read_bytes())
    (source / "src/utils/pipeline-theme.ts").write_text(
        "export const themeScript = " + json.dumps(THEME_SCRIPT.read_text()) + ";\n"
    )
    # Dataset payloads can now change after a permanent cleanup. The stock
    # five-minute cache would pair new shard offsets with stale episode counts.
    versions = source / "src/utils/versionUtils.ts"
    text = versions.read_text()
    anchor = "const CACHE_TTL_MS = 5 * 60 * 1000;"
    replacement = "const CACHE_TTL_MS = 0; // bodypipeline: mutable datasets"
    if anchor not in text and replacement not in text:
        raise RuntimeError("Unexpected Visualizer dataset cache configuration")
    versions.write_text(text.replace(anchor, replacement))
    parquet = source / "src/utils/parquetUtils.ts"
    text = parquet.read_text()
    marker = "// bodypipeline: invalidate replaced shared shards"
    if marker not in text:
        text += (
            "\n" + marker + "\n"
            "export function invalidateParquetCache(repoId: string) {\n"
            "  for (const key of parquetFileCache.keys()) {\n"
            "    if (key.includes('/' + repoId + '/')) parquetFileCache.delete(key);\n"
            "  }\n}\n"
        )
        parquet.write_text(text)
    text = versions.read_text()
    if marker not in text:
        text = text.replace(
            'import { authHeaders } from "./auth";',
            'import { authHeaders } from "./auth";\n'
            'import { invalidateParquetCache } from "./parquetUtils";\n'
            + marker
            + "\nconst payloadRevisions = new Map<string, string>();",
        )
        anchor = "    datasetInfoCache.set(repoId, {"
        if text.count(anchor) != 1:
            raise RuntimeError("Unexpected Visualizer dataset cache setter")
        text = text.replace(
            anchor,
            '    const revision = String(data.bodypipeline_revision || "original");\n'
            "    if (payloadRevisions.get(repoId) !== revision) invalidateParquetCache(repoId);\n"
            "    payloadRevisions.set(repoId, revision);\n" + anchor,
        )
        versions.write_text(text)

    from pipeline.server.viewer_task_annotations import patch_task_annotations

    patch_task_annotations(cache)

    from pipeline.server.viewer_batch_preview import patch_batch_preview

    patch_batch_preview(cache)


def patch_score_filter(cache: Path):
    """Extend the existing filtering tab without replacing upstream filters."""
    command = ["git", "apply", "--unsafe-paths", "--directory", str(cache / "source")]
    patch = SCORE_FILTER_PATCH.read_text()
    for flags in (["--check"], ["--reverse", "--check"]):
        result = subprocess.run(
            command + flags, input=patch, text=True, capture_output=True
        )
        if result.returncode == 0:
            if "--reverse" not in flags:
                subprocess.run(command, input=patch, text=True, check=True)
            return
    raise RuntimeError("Unexpected Visualizer source; cannot apply score filtering")


def patch_subtask_review(cache: Path):
    from .viewer_annotations import patch_annotations

    patch_annotations(cache)


def _fingerprint(cache: Path, data_origin: str) -> str:
    """Identify the build inputs; the origin is baked into the browser bundle."""

    source = json.loads((cache / ".letools-source.json").read_text())
    return hashlib.sha256(
        json.dumps(
            {
                "source": source.get("fingerprint"),
                "origin": data_origin,
                "channel_charts": hashlib.sha256(
                    CHANNEL_PATCH.read_bytes()
                ).hexdigest(),
                "presentation": hashlib.sha256(
                    PRESENTATION_PATCH.read_bytes()
                    + CAMERA_ORDER_PATCH.read_bytes()
                    + SCORE_FILTER_PATCH.read_bytes()
                    + THEME_SCRIPT.read_bytes()
                    + Path(__file__).read_bytes()
                    + Path(__file__).with_name("viewer_annotations.py").read_bytes()
                    + Path(__file__).with_name("viewer_task_annotations.py").read_bytes()
                    + Path(__file__).with_name("viewer_batch_preview.py").read_bytes()
                    + b"".join(
                        path.name.encode() + path.read_bytes()
                        for path in sorted(UI_ASSETS.iterdir())
                        if path.is_file()
                    )
                ).hexdigest(),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def build_if_needed(cache: Path, bun: str, env: dict, data_origin: str):
    stamp = cache / BUILD_STAMP
    wanted = _fingerprint(cache, data_origin)
    source = cache / "source"
    try:
        current = json.loads(stamp.read_text()).get("fingerprint")
    except (OSError, ValueError):
        current = None
    if current == wanted and (source / ".next").is_dir():
        return
    subprocess.run([bun, "run", "build"], cwd=source, env=env, check=True)
    stamp.write_text(json.dumps({"fingerprint": wanted}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--data-origin", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bun")
    args = parser.parse_args()
    cache = Path(args.cache)

    setup = [args.executable, "visualizer", "setup", "--cache-dir", str(cache)]
    if args.bun:
        setup += ["--bun", args.bun]
    subprocess.run(setup, check=True)
    patch_cache(cache)
    patch_channel_charts(cache)
    patch_presentation(cache)

    bun = args.bun or "bun"
    origin = args.data_origin.rstrip("/")
    env = os.environ.copy()
    env["DATASET_URL"] = origin
    env["NEXT_PUBLIC_LEROBOT_DOCTOR_URL"] = f"{origin}/doctor"
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    env.pop("NEXT_PUBLIC_ANNOTATE_BACKEND_URL", None)

    build_if_needed(cache, bun, env, origin)
    source = cache / "source"
    print(
        json.dumps({"url": f"http://{args.host}:{args.port}/", "origin": origin}),
        flush=True,
    )
    os.chdir(source)
    os.execvpe(
        bun,
        [bun, "run", "start", "--hostname", args.host, "--port", str(args.port)],
        env,
    )


if __name__ == "__main__":
    main()
