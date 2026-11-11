"""Resource-isolated provider preparation. The Server remains the state writer."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from pipeline.shared.core import DomainError, confined, file_hash

from .episode_validation import EpisodeValidationError
from .providers.hdf5_images import image_storage
from .providers.registry import get_provider


def execute(request):
    """Only static registered Providers; no credentials or arbitrary callables."""
    raw = Path(request["raw"])
    root = raw if raw.is_dir() else raw.parent
    for item in request["files"]:
        if file_hash(confined(root, item["path"])) != item["sha256"]:
            raise DomainError(f"原始文件摘要变化：{item['path']}")
    provider = get_provider(request["provider"])
    configure = getattr(provider, "configure", None)
    if configure:
        configure(SimpleNamespace(**request["tool_settings"]))
    kwargs = {"options": request["options"]} if request["options"] else {}
    dest = Path(request["destination"])
    with image_storage(request["image_storage"]):
        prepared = provider.prepare(raw, dest, request["profile"], **kwargs)
    path = Path(prepared.get("aligned_path", dest / "episode0"))
    quality = provider.quality(path, request["profile"], synthetic=request["synthetic"])
    return {"prepared": prepared, "quality": quality}


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    try:
        result = execute(request)
    except EpisodeValidationError as exc:
        result = {
            "error": str(exc),
            "skippable": True,
            "code": exc.reason_code,
            "details": exc.details,
        }
    except Exception as exc:
        result = {"error": str(exc), "skippable": False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
