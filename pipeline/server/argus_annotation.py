"""Argus ``review`` execution and output adaptation for automatic annotation.

Argus is a separate Python 3.11 toolchain.  BodyPipeline only builds its
command line, supervises the process group, and reads the files it writes.  No
Argus module is imported into the Server API environment: the two toolchains
have different dependencies and are deliberately isolated.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from pipeline.shared.core import DomainError, executable_path

STAGE_TITLES = {
    "download": "下载数据",
    "convert": "准备 Episode",
    "dictionary": "解释字段",
    "checks": "确定性检查",
    "checks_jumps": "确定性检查",
    "checks_grippers": "确定性检查",
    "checks_capture": "采集质量检查",
    "checks_sensors": "传感器检查",
    "clips": "生成预览视频",
    "dry_run": "构造模型请求",
    "label": "大模型标注",
    "board": "拼接标注结果",
}

STAGE_ORDER = (
    "download",
    "convert",
    "dictionary",
    "checks",
    "clips",
    "dry_run",
    "label",
    "board",
)

_MARKER = re.compile(r"^== ([A-Za-z0-9_]+)\b", re.MULTILINE)
_EPISODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CAMERA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Argus names the fixed camera ``exo`` and the mounted ones ``left``/``right``;
# clips land at CLIPS/<episode>.mp4 for the fixed one and in a folder per camera
# otherwise (board/clips.py clip_path).  The page shows these labels; any other
# camera keeps the folder's own name so a new rig needs no page change.
CAMERA_LABELS = {"exo": "主相机", "left": "左腕", "right": "右腕"}
_WRIST_FOLDERS = {"wrist_left": "left", "wrist_right": "right"}


def _python(settings) -> str | None:
    """The configured interpreter, never symlink-resolved.

    Resolving a virtualenv's ``bin/python`` drops its site-packages, which
    surfaces as a ModuleNotFoundError on the first Argus import.
    """

    candidate = settings.argus_python or str(
        Path(settings.argus_src).expanduser() / ".venv/bin/python"
    )
    path = Path(candidate).expanduser()
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return executable_path(candidate)


def available(settings) -> tuple[bool, str]:
    if not settings.argus_src:
        return False, "未配置 argus_src"
    source = Path(settings.argus_src).expanduser()
    if not (source / "review").is_dir() or not (source / "label").is_dir():
        return False, f"Argus review 源码不存在：{source}"
    if _python(settings) is None:
        return False, "未找到 Argus Python 解释器"
    return True, ""


def source_path(settings) -> Path:
    return Path(settings.argus_src).expanduser()


def template_available(settings) -> bool:
    root = source_path(settings)
    try:
        marker = "pipeline-argus-task-template:v1"
        return (
            all(
                marker in (root / name).read_text()
                for name in (
                    "label/episode.py",
                    "label/harness.py",
                    "label/pipeline_template.py",
                )
            )
            and "pipeline-argus-template-retry:v1"
            in (root / "label/harness.py").read_text()
        )
    except OSError:
        return False


# ---------------------------------------------------------------- price table
# Argus reserves the worst case of a model call before dispatching it, and
# refuses a capped run whose model it cannot price.  A gateway publishes model
# names Argus never listed, so the operator can declare their rates here, in USD
# per token, exactly in Argus's shape: [input, cached input, output] for the
# standard context tier, optionally followed by the long-context tier.
#
# This mirrors the table pinned Argus ships (label/harness.py OPENAI_PRICES at
# 383ca11); it only answers "can a cap bound this model", while the actual bound
# is computed inside Argus from PIPELINE_ARGUS_PRICES and its own table.
ARGUS_LIST_PRICES = {
    "gpt-6-astra": [[1e-5, 1e-6, 5e-5], [2e-5, 2e-6, 7.5e-5]],
    "gpt-6-sol": [[2e-6, 2e-7, 1e-5], [4e-6, 4e-7, 1.5e-5]],
    "gpt-6.1-sol": [[2e-6, 1e-7, 1e-5], [4e-6, 2e-7, 1.5e-5]],
}
MAX_PRICE_MODELS = 200


def normalize_prices(prices) -> dict:
    """Validate an operator-declared rate table for one call.

    ``Settings.load`` validates the same table at startup; this keeps an
    in-process Settings (tests, reloads) honest and reports the reason as a
    request error instead of a refused paid run.
    """

    from pipeline.shared.config import validate_argus_prices

    if prices is None or prices == "":
        return {}
    try:
        table = validate_argus_prices(prices)
    except ValueError as exc:
        raise DomainError(f"模型价目表无效：{exc}", 400) from exc
    if len(table) > MAX_PRICE_MODELS:
        raise DomainError("模型价目表条目过多", 400)
    return table


def configured_prices(settings) -> dict:
    """The Server's declared rates, validated once per call."""

    return normalize_prices(getattr(settings, "argus_prices", None))


def price_table(settings) -> dict:
    """Rates for the page: Argus's list prices plus the operator's overrides."""

    table = {
        name: [list(tier) for tier in tiers]
        for name, tiers in ARGUS_LIST_PRICES.items()
    }
    table.update(configured_prices(settings))
    return table


def pricing_gap(settings, model: str) -> str:
    """Why a spend-capped run would refuse this model, or an empty string.

    Argus reserves the worst case of the final call before it dispatches, and a
    cap it cannot bound is refused -- but only after prepare, checks and clips
    have run.  Checking here fails in the submit request instead, naming the
    models that would work.
    """

    name = str(model or "").split("/")[-1]
    if not name:
        return ""
    priced = price_table(settings)
    if name in priced:
        return ""
    known = ", ".join(sorted(priced)) or "（无）"
    return (
        f"模型 {model} 没有已知单价，费用上限无法保证；"
        f"请在服务器 argus_prices 中补充该模型单价，或改用：{known}"
    )


MAX_BASE_URL = 2048
MAX_API_KEY = 4096
MAX_MODELS_BYTES = 2_000_000
MAX_MODELS = 2000
MODEL_LIST_TIMEOUT = 20
# Argus can also read its upstream key variables directly.  They survive
# tool_environment() because it only filters PIPELINE_* names.
NATIVE_KEY_ENVS = ("OPENROUTER_API_KEYS", "OPENAI_API_KEY")


def normalize_base_url(value) -> str:
    """Validate a per-task gateway URL without ever echoing the secret fields."""

    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_BASE_URL:
        raise DomainError("模型 baseURL 过长", 400)
    if any(char.isspace() or ord(char) < 32 for char in text):
        raise DomainError("模型 baseURL 不能包含空白或控制字符", 400)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        raise DomainError("模型 baseURL 必须使用 http:// 或 https://", 400)
    if not parsed.hostname:
        raise DomainError("模型 baseURL 缺少主机名", 400)
    if parsed.username or parsed.password:
        raise DomainError("模型 baseURL 不能包含用户名或密码", 400)
    if parsed.query or parsed.fragment:
        raise DomainError("模型 baseURL 不能包含查询参数或片段", 400)
    return text.rstrip("/")


def normalize_api_key(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > MAX_API_KEY:
        raise DomainError("模型 API Key 过长", 400)
    if any(
        char.isspace()
        or not char.isascii()
        or ord(char) < 32
        or ord(char) == 127
        for char in text
    ):
        raise DomainError("模型 API Key 不能包含空白、控制字符或非 ASCII 字符", 400)
    return text


def server_key_configured(settings) -> bool:
    if os.environ.get(settings.argus_api_key_env, "").strip():
        return True
    return any(os.environ.get(name, "").strip() for name in NATIVE_KEY_ENVS)


def resolve_credentials(settings, base_url=None, api_key=None) -> dict:
    """Credentials for one task: the page input wins over server defaults.

    The key is returned to the caller only.  It must never be copied into the
    annotation record, the work context, a log or an export.  When Argus reads
    its own upstream key variables, ``api_key`` stays empty and those variables
    are left untouched in the subprocess environment.
    """

    task_base_url = normalize_base_url(base_url)
    server_base_url = normalize_base_url(settings.argus_base_url)
    key = normalize_api_key(api_key) or normalize_api_key(
        os.environ.get(settings.argus_api_key_env, "")
    )
    if not key and not server_key_configured(settings):
        raise DomainError(
            "未提供模型 API Key，且服务器未配置默认密钥；请在页面填写本次任务的 API Key",
            400,
        )
    return {
        "base_url": task_base_url or server_base_url,
        "api_key": key,
        "task_base_url": task_base_url,
    }


def redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


def _model_ids(payload) -> list[str]:
    """Read the common ``/models`` shapes without assuming one gateway."""

    items = payload
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models")
        if isinstance(items, dict):
            items = items.get("data") or list(items.values())
    if not isinstance(items, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("id") or item.get("model") or item.get("name")
        else:
            continue
        if not isinstance(name, str):
            continue
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= MAX_MODELS:
            break
    return names


def list_models(settings, base_url=None, api_key=None) -> dict:
    """List models offered by the configured Chat Completions gateway.

    The key is only sent as a request header and is removed from any error
    detail before it can reach the page.
    """

    credentials = resolve_credentials(settings, base_url, api_key)
    endpoint = credentials["base_url"]
    if not endpoint:
        raise DomainError("请填写模型 baseURL 后再获取模型列表", 400)
    key = credentials["api_key"]
    headers = {
        "Accept": "application/json",
        "User-Agent": "BodyPipeline annotation model discovery",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(
        endpoint + "/models", headers=headers, method="GET"
    )
    try:
        with urllib.request.urlopen(
            request, timeout=MODEL_LIST_TIMEOUT
        ) as response:
            raw = response.read(MAX_MODELS_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2048).decode(errors="replace").strip()
        except OSError:
            pass
        detail = redact(detail, key)[:400]
        suffix = f" · {detail}" if detail else ""
        raise DomainError(
            f"模型列表获取失败：HTTP {exc.code}{suffix}", 502
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        message = redact(str(reason), key)[:500]
        raise DomainError(f"模型列表获取失败：{message}", 502) from exc
    if len(raw) > MAX_MODELS_BYTES:
        raise DomainError("模型列表响应过大", 502)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise DomainError("模型列表不是有效 JSON", 502) from exc
    return {"models": _model_ids(payload), "base_url": endpoint}


def environment(
    settings,
    annotation: dict,
    provider_context: dict,
    credentials: dict | None = None,
) -> dict:
    """Environment for the Argus subprocess.

    ``tool_environment`` strips every ``PIPELINE_*`` variable, which is the
    right default for letools.  Argus needs the gateway/key/camera variables
    back, and they are added explicitly here rather than by loosening it.
    """

    source = source_path(settings)
    env = settings.tool_environment()
    # Only this task's frozen snapshot may shape the request, never an inherited
    # environment variable from another run or a live edit of the template library.
    env.pop("PIPELINE_ARGUS_TASK_TEMPLATE", None)
    if annotation.get("task_template"):
        from .template_annotation import SPEC_NAME

        env["PIPELINE_ARGUS_TASK_TEMPLATE"] = str(
            Path(annotation["work_path"]) / SPEC_NAME
        )
    env["PYTHONPATH"] = str(source) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["PATH"] = os.pathsep.join([str(source / "tools/bin"), env.get("PATH", "")])
    env.setdefault("MALLOC_ARENA_MAX", "2")

    cameras = provider_context.get("cameras") or {}
    if cameras:
        env["PIPELINE_ARGUS_CAMERA_MOUNTING"] = json.dumps(cameras, ensure_ascii=False)
    if settings.argus_base_url:
        env["PIPELINE_ARGUS_BASE_URL"] = settings.argus_base_url
        env["PIPELINE_ARGUS_GENERIC_GATEWAY"] = "1"
    key = os.environ.get(settings.argus_api_key_env, "")
    if key:
        env["PIPELINE_ARGUS_API_KEY"] = key
    if credentials:
        base_url = str(credentials.get("base_url") or "")
        task_key = str(credentials.get("api_key") or "")
        if base_url:
            env["PIPELINE_ARGUS_BASE_URL"] = base_url
            env["PIPELINE_ARGUS_GENERIC_GATEWAY"] = "1"
        if task_key:
            env["PIPELINE_ARGUS_API_KEY"] = task_key
    if env.get("PIPELINE_ARGUS_GENERIC_GATEWAY") == "1":
        # Argus's routing question otherwise goes to its OpenRouter-only
        # ``openai/gpt-6-sol``.  A gateway publishes its own catalogue, so route
        # with the model this task already chose (and just listed).
        route_model = str(annotation.get("model") or "").strip()
        if route_model:
            env["PIPELINE_ARGUS_ROUTE_MODEL"] = route_model
    prices = configured_prices(settings)
    if prices:
        # Without this a capped run refuses a gateway model Argus never listed:
        # it cannot reserve the worst case of a call it cannot price.
        env["PIPELINE_ARGUS_PRICES"] = json.dumps(prices, ensure_ascii=False)
    jobs = max(1, int(annotation.get("concurrency") or 2))
    env["RDA_DECODE_CONCURRENCY"] = str(max(2, 2 * jobs))
    return env


def command(settings, annotation: dict, data: Path, work: Path) -> list[str]:
    python = _python(settings)
    if python is None:
        raise DomainError("未找到 Argus Python 解释器", 503)
    args = [
        "-m",
        "review",
        "--data",
        str(data),
        "--rig",
        str(annotation.get("rig") or "teleop_arms"),
        "--out",
        str(work / "argus"),
        "--dataset",
        str(annotation.get("dataset_name") or annotation.get("job_id") or "dataset"),
        "--cap",
        f"{float(annotation.get('cap_usd') or 0.0):.4f}",
        "--concurrency",
        str(max(1, int(annotation.get("concurrency") or 8))),
    ]
    note = str(annotation.get("note") or "").strip()
    if note:
        args += ["--dataset-note", note]
    model = str(annotation.get("model") or "").strip()
    if model:
        args += ["--model", model]
    reasoning = str(annotation.get("reasoning") or "").strip()
    if reasoning:
        args += ["--reasoning", reasoning]
    sample_fps = annotation.get("sample_fps")
    if sample_fps:
        args += ["--sample-fps", f"{float(sample_fps):.4f}"]
    inspection = annotation.get("inspection")
    if inspection in {"auto", "all", "off"}:
        args += ["--inspection", inspection]
    if annotation.get("field_dictionary") is True:
        args += ["--field-dictionary", "on"]
    elif annotation.get("field_dictionary") is False:
        args += ["--field-dictionary", "off"]
    max_tokens = annotation.get("max_tokens")
    if max_tokens:
        args += ["--max-tokens", str(int(max_tokens))]
    if annotation.get("task_template") and int(annotation.get("attempts") or 0) > 1:
        from .template_annotation import invalid_received_units

        for name in invalid_received_units(work):
            args += ["--retry-received", name]
    return [python, *args]


def stage_from_log(text: str, current: str = "") -> str:
    """The latest stage Argus announced, or the current title when none."""

    stage = current
    for match in _MARKER.finditer(text):
        marker = match.group(1)
        key = marker if marker in STAGE_TITLES else next(
            (prefix for prefix in STAGE_ORDER if marker.startswith(prefix)), ""
        )
        if key:
            stage = STAGE_TITLES[key]
    if "review finished" in text or "== board" in text:
        stage = stage or STAGE_TITLES["board"]
    return stage


def read_run(work: Path) -> dict:
    argus = Path(work) / "argus"
    for relative in ("run_final/run.json", "run/run.json"):
        path = argus / relative
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def read_report(work: Path) -> dict:
    try:
        value = json.loads((Path(work) / "argus/report.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _qa_files(argus: Path) -> list[Path]:
    qa = argus / "qa"
    if not qa.is_dir():
        return []
    return sorted(qa.glob("*.json"))


def _raw_files(argus: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for directory in (argus / "run/out", argus / "run_final/out"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("episode_*.json")):
            files.setdefault(path.stem, path)
        for path in sorted(directory.glob("failed_episode_*.json")):
            files.setdefault(path.stem.removeprefix("failed_"), path)
        for path in sorted(directory.glob("noreply_episode_*.json")):
            files.setdefault(path.stem.removeprefix("noreply_"), path)
    return files


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _summary(name: str, data: dict, source: str) -> dict:
    labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
    completion = data.get("completion")
    if not isinstance(completion, dict):
        completion = labels.get("completion") if isinstance(labels, dict) else {}
    completion = completion if isinstance(completion, dict) else {}
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    failed = _label_failure(data)
    timeline = (
        data.get("event_labels", [])
        if "task_document" in data
        else data.get("event_labels") or labels.get("timeline") or []
    )
    key_events = data.get("key_events") or labels.get("key_events") or []
    prompt = data.get("episode_prompt") or labels.get("task_summary") or ""
    duration = data.get("duration_s")
    if duration is None and isinstance(data.get("config"), dict):
        times = data["config"].get("timesteps_s") or []
        if times:
            try:
                duration = round(float(times[-1]) - float(times[0]), 3)
            except (TypeError, ValueError):
                duration = None
    returns = data.get("state_changes") or labels.get("state_changes") or []
    outcome = completion.get("task_completed")
    if isinstance(outcome, bool):
        # Older adapters wrote a boolean; Argus itself uses the word list.
        outcome = "success" if outcome else "failure"
    return {
        "id": name,
        "episode_id": meta.get("episode_id") or name,
        "run_episode": meta.get("run_episode") or name,
        "prompt": prompt,
        "duration_s": duration,
        "completed": outcome == "success",
        "outcome": outcome,
        "failure_kind": completion.get("failure_kind"),
        "completed_at_s": completion.get("completed_at_s"),
        "events": len(timeline) if isinstance(timeline, list) else 0,
        "key_events": len(key_events) if isinstance(key_events, list) else 0,
        "state_changes": len(returns) if isinstance(returns, list) else 0,
        "labels_available": not bool(failed),
        "label_failure": failed,
        "model": meta.get("model") or data.get("model"),
        "usage": data.get("_usage") or data.get("usage"),
        "source": source,
        "template_name": data.get("template_name"),
        "template_warnings": data.get("template_warnings", []),
    }


def outputs(work: Path) -> list[dict]:
    """Every episode the run produced, newest output layout first."""

    from .template_annotation import SPEC_NAME

    if (Path(work) / SPEC_NAME).exists():
        return [
            _summary(name, value, "template")
            for name, value in episode_results(work).items()
        ]
    argus = Path(work) / "argus"
    rows: list[dict] = []
    seen: set[str] = set()
    for path in _qa_files(argus):
        data = _read_json(path)
        if not data:
            continue
        name = path.stem
        rows.append(_summary(name, data, "board"))
        seen.add(name)
    for name, path in _raw_files(argus).items():
        if name in seen:
            continue
        data = _read_json(path)
        if not data or data.get("dry_run"):
            continue
        episode = Path(str(data.get("episode_dir") or name)).name or name
        rows.append(_summary(episode, data, "run"))
    rows.sort(key=lambda row: row["id"])
    return rows


def labels_available(data: dict) -> bool:
    """Whether one episode result carries usable model labels.

    Kept next to :func:`_summary` so the queue, the export and the page agree on
    what counts as a labelled episode.
    """

    if not isinstance(data, dict) or data.get("dry_run"):
        return False
    return not _label_failure(data)


def _label_failure(data: dict) -> dict | None:
    failed = data.get("_label_failed")
    if isinstance(failed, dict) and failed:
        return failed
    if failed:
        return {"status": "failed", "why": "模型调用失败"}
    if data.get("parse_ok") is False:
        return {
            "status": "failed",
            "why": data.get("no_reply") or data.get("unreadable") or "模型未返回可用标注",
        }
    return None


def episode_results(work: Path) -> dict[str, dict]:
    """Episode directory name -> full result, including failed labels.

    Both the board output (``qa/*.json``) and a raw model reply share the
    episode directory name the prepared context uses, so the caller can join
    them on that name.  Failed and unparsable replies are kept so an export can
    report why an episode was skipped instead of silently omitting it.
    """

    argus = Path(work) / "argus"
    rows: dict[str, dict] = {}
    for path in _qa_files(argus):
        data = _read_json(path)
        if not data or data.get("dry_run"):
            continue
        rows.setdefault(path.stem, data)
    for name, path in _raw_files(argus).items():
        if name in rows:
            continue
        data = _read_json(path)
        if not data or data.get("dry_run"):
            continue
        episode = Path(str(data.get("episode_dir") or name)).name or name
        rows.setdefault(episode, data)
    from .template_annotation import assemble_results

    return assemble_results(Path(work), rows)


def episode(work: Path, episode_id: str) -> dict | None:
    if not isinstance(episode_id, str) or not _EPISODE_ID.fullmatch(episode_id):
        return None
    from .template_annotation import SPEC_NAME

    if (Path(work) / SPEC_NAME).exists():
        return episode_results(work).get(episode_id)
    argus = Path(work) / "argus"
    for path in _qa_files(argus):
        data = _read_json(path)
        if not data:
            continue
        meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
        ids = {
            path.stem,
            str(meta.get("episode_id") or ""),
            str(meta.get("run_episode") or ""),
        }
        if episode_id in ids:
            return {"id": path.stem, "source": "board", **data}
    path = _raw_files(argus).get(episode_id)
    if path is None:
        for name, candidate in _raw_files(argus).items():
            data = _read_json(candidate)
            if Path(str(data.get("episode_dir") or "")).name == episode_id:
                path = candidate
                break
    if path is not None:
        data = _read_json(path)
        if data:
            return {
                "id": Path(str(data.get("episode_dir") or path.stem)).name,
                "source": "run",
                **data,
            }
    return None


def _clip_label(camera: str) -> str:
    depth = camera.startswith("depth_")
    base = camera.removeprefix("depth_")
    label = CAMERA_LABELS.get(base, base)
    return f"{label} 深度" if depth else label


def _clip_rows(clips: Path, episode_id: str) -> list[tuple[str, Path]]:
    """Every playable clip of one episode as ``(camera, path)``, main camera first.

    The main camera's clip sits at the top of ``clips`` and every other camera
    has a folder named after it; ``wrist_left``/``wrist_right`` are the mounted
    cameras.  Depth clips (``depth_<camera>``) stay separate from their colour
    camera and come last.
    """

    if not clips.is_dir():
        return []
    candidates: list[tuple[str, Path]] = [("exo", clips / f"{episode_id}.mp4")]
    for folder in sorted(clips.iterdir()):
        if not folder.is_dir():
            continue
        camera = _WRIST_FOLDERS.get(folder.name, folder.name)
        candidates.append((camera, folder / f"{episode_id}.mp4"))
    rows: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for camera, path in candidates:
        if camera in seen or not path.is_file():
            continue
        seen.add(camera)
        rows.append((camera, path))
    order = {"exo": 0, "left": 1, "right": 2}
    rows.sort(key=lambda row: (
        row[0].startswith("depth_"),
        order.get(row[0], len(order)),
        row[0],
    ))
    return rows


def clips(work: Path, episode_id: str) -> list[dict]:
    """The episode's playable preview clips, or [] when the run cut none."""

    if not isinstance(episode_id, str) or not _EPISODE_ID.fullmatch(episode_id):
        return []
    rows: list[dict] = []
    for camera, path in _clip_rows(Path(work) / "argus" / "clips", episode_id):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append(
            {"camera": camera, "label": _clip_label(camera), "size": size}
        )
    return rows


def clip(work: Path, episode_id: str, camera: str) -> Path | None:
    """The path of one discovered clip; never a client-supplied path."""

    if not isinstance(episode_id, str) or not _EPISODE_ID.fullmatch(episode_id):
        return None
    if not isinstance(camera, str) or not _CAMERA_ID.fullmatch(camera):
        return None
    for name, path in _clip_rows(Path(work) / "argus" / "clips", episode_id):
        if name == camera:
            return path
    return None


def cost(work: Path) -> dict:
    info = read_run(work)
    value = {
        "status": info.get("status"),
        "cost_usd": info.get("cost_usd"),
        "reserved_cost_usd": info.get("reserved_cost_usd"),
        "cost_complete": info.get("cost_complete"),
        "cap_usd": info.get("cap_usd"),
    }
    if value["cost_usd"] is None:
        billed = 0.0
        reserved = 0.0
        complete = True
        for path in _raw_files(Path(work) / "argus").values():
            data = _read_json(path)
            if data.get("dry_run"):
                continue
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            estimate = usage.get("est_cost_usd")
            if isinstance(estimate, (int, float)) and not isinstance(estimate, bool):
                billed += float(estimate)
            else:
                complete = False
            bound = data.get("final_reserved_usd")
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                reserved += float(bound)
        value["cost_usd"] = round(billed, 6) if complete else None
        value["reserved_cost_usd"] = round(reserved, 6)
        value["cost_complete"] = complete
    return value


def progress(work: Path) -> dict:
    argus = Path(work) / "argus"
    from .template_annotation import SPEC_NAME

    if (Path(work) / SPEC_NAME).exists():
        rows = outputs(work)
        return {
            "units": len(list((argus / "units").glob("episode_*"))),
            "labelled": sum(r["labels_available"] for r in rows),
            "episodes": len(rows),
        }
    units = (
        len(list((argus / "units").glob("episode_*")))
        if (argus / "units").is_dir()
        else 0
    )
    # A resumed run may label into run_final while run/out is being rebuilt.
    labelled: set[str] = set()
    for directory in (argus / "run/out", argus / "run_final/out"):
        if directory.is_dir():
            labelled.update(path.stem for path in directory.glob("episode_*.json"))
    return {
        "units": units,
        "labelled": len(labelled),
        "episodes": max(len(labelled), len(_raw_files(argus))),
    }


def log_names(work: Path) -> list[str]:
    names = ["run"]
    logs = Path(work) / "argus/logs"
    if logs.is_dir():
        names.extend(
            sorted(path.stem for path in logs.glob("*.log") if path.is_file())
        )
    return names


def read_log(work: Path, name: str) -> str:
    if name not in log_names(work):
        raise DomainError("日志不存在", 404)
    if name == "run":
        path = Path(work) / "logs/run.log"
    else:
        path = Path(work) / "argus/logs" / f"{name}.log"
    if not path.is_file() or path.is_symlink():
        raise DomainError("日志不存在", 404)
    return _tail_text(path, 64000)


def _tail_text(path: Path, limit: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            return handle.read().decode(errors="replace")
    except OSError:
        return ""


__all__ = [
    "STAGE_ORDER",
    "STAGE_TITLES",
    "available",
    "command",
    "cost",
    "environment",
    "episode",
    "labels_available",
    "episode_results",
    "log_names",
    "outputs",
    "progress",
    "read_log",
    "read_report",
    "read_run",
    "source_path",
    "stage_from_log",
]
