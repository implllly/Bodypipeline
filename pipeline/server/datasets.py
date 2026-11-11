"""Resolve inspectable datasets from server-owned job records."""

from pathlib import Path

from pipeline.shared.core import DomainError, confined

from .storage import storage_path

TARGET_FORMATS = ("v3.0", "v2.1")
DATA_READY_STATES = {"CONVERTED", "SYNTHETIC_READY", "QUARANTINED", "COMPLETED"}


def require_mutable_dataset(settings, path, job=None, *, store=None, jobs=None):
    """Do not mutate published deliveries, including aliases/manual imports."""
    resolved = Path(path).resolve()
    protected = (job or {}).get("immutable_delivery") or any(
        resolved == Path(s["root"]).resolve()
        or resolved.is_relative_to(Path(s["root"]).resolve())
        or Path(s["root"]).resolve().is_relative_to(resolved)
        for s in settings.delivery_sources
    )
    if not protected and (store is not None or jobs is not None):
        for row in jobs if jobs is not None else store.all("job"):
            if row.get("immutable_delivery") and row.get("dataset_path"):
                source = Path(row["dataset_path"]).resolve()
                if resolved == source or resolved.is_relative_to(source) or source.is_relative_to(resolved):
                    protected = True
                    break
    if protected:
        raise DomainError("外部交付数据集为只读，不允许原地修改或删除；请生成副本", 403)


def _has_metadata(path: Path) -> bool:
    info = path / "meta/info.json"
    return (
        path.is_dir()
        and not path.is_symlink()
        and info.is_file()
        and not info.is_symlink()
    )


def _dataset_roots(settings):
    roots = [settings.root]
    if settings.data_root is not None:
        roots.append(settings.data_root)
    roots.extend(settings.import_roots)
    return roots


def _recorded_dataset(settings, job):
    """Read an existing result path recorded by an older/imported job, without moving data."""
    recorded = job.get("dataset_path")
    if not isinstance(recorded, str):
        return None
    candidate = Path(recorded)
    if not candidate.is_absolute():
        return None
    resolved = candidate.resolve()
    for root in _dataset_roots(settings):
        root = root.resolve()
        try:
            path = (
                resolved
                if resolved == root
                else confined(root, resolved.relative_to(root).as_posix())
            )
        except (ValueError, DomainError):
            continue
        if _has_metadata(path):
            return path
    return None


def dataset_for_job(settings, job):
    state = job["state"]
    if state in {"DELETING", "DELETE_FAILED", "DELETED"}:
        raise DomainError("数据集正在删除或已删除，不能继续使用", 409)
    if state not in DATA_READY_STATES and job.get("dataset_ready") is not True:
        raise DomainError("尚无完成转换和结构验证的数据集；请先完成转换")
    data = storage_path(settings, job, "work", job["id"]) / "dataset"
    if _has_metadata(data):
        return data
    recorded = _recorded_dataset(settings, job)
    if recorded is not None:
        return recorded
    raise DomainError("转换结果不存在或存储不可用，请检查数据目录")
