"""Register existing on-disk captures and LeRobot datasets without copying them."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from pipeline.shared.collections import dataset_name
from pipeline.shared.core import DomainError, confined, digest, file_hash, now
from pipeline.shared.hdf5 import AGILEX_HDF5_FORMAT, ALOHA_HDF5_FORMAT, is_hdf5_path

from .providers import RuntimeCatalog
from .providers.arx_direct_teaching import RAW_FORMAT as ARX_DIRECT_HDF5_FORMAT
from .storage import robot_type

LEROBOT_VERSIONS = {"v2.1", "v3.0"}


def _json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DomainError(f"{label} 无法读取：{path}", 400) from exc
    if not isinstance(value, dict):
        raise DomainError(f"{label} 必须是 JSON 对象：{path}", 400)
    return value


def _within(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    return any(
        resolved == root.resolve() or resolved.is_relative_to(root.resolve())
        for root in roots
    )


class ImportService:
    """Admin-only import registry for historical raw captures and LeRobot datasets."""

    def __init__(self, settings, ingest, runtimes=None):
        self.settings, self.ingest, self.store = settings, ingest, ingest.store
        self.runtimes = runtimes or RuntimeCatalog(settings)
        self.lock = threading.RLock()

    def allowed_roots(self) -> list[Path]:
        roots = list(self.settings.import_roots)
        if self.settings.data_root is not None:
            roots.append(self.settings.data_root)
        unique = []
        seen = set()
        for root in roots:
            resolved = root.expanduser().resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

    def resolve(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise DomainError("请填写 Server 上的数据目录", 400)
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise DomainError("导入路径必须是 Server 上的绝对路径", 400)
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise DomainError(f"导入路径不存在或不可访问：{candidate}", 404) from exc
        roots = self.allowed_roots()
        if not roots:
            raise DomainError("Server 未配置 import_roots，无法导入外部目录", 503)
        if not _within(candidate, roots):
            display = "、".join(str(root) for root in roots)
            raise DomainError(f"导入路径不在允许范围内：{display}", 403)
        if not candidate.is_dir() or candidate.is_symlink():
            raise DomainError("导入路径必须是普通目录，不能是文件或符号链接", 400)
        return candidate

    def import_path(
        self,
        raw_path: str,
        robot_type_override: str | None = None,
        *,
        runtime_id: str | None = None,
    ):
        path = self.resolve(raw_path)
        if any(path.is_relative_to(Path(s["root"])) for s in self.settings.delivery_sources):
            raise DomainError("交付目录必须通过 READY 扫描校验登记，不能手动绕过", 403)
        override = None
        if robot_type_override is not None and robot_type_override.strip():
            override = robot_type(robot_type_override.strip())
        runtime = self.runtimes.get(runtime_id) if runtime_id else None
        if (path / "meta/info.json").is_file():
            return self._import_lerobot(path, override, runtime)
        if not (path / "manifest.json").is_file() and self._episode_files(path):
            return self._import_hdf5_episodes(path, override, runtime)
        if runtime is not None:
            runtime = self.runtimes.get(runtime.id)
        return self._import_raw(path, override, runtime)

    @staticmethod
    def _episode_files(path: Path) -> list[Path]:
        """Upstream ALOHA-style captures: one HDF5 file per episode, no manifest."""

        return sorted(
            item
            for item in path.iterdir()
            if item.is_file() and not item.is_symlink() and is_hdf5_path(item)
        )

    def _known_runtime(self, robot_type_value: str | None):
        if not robot_type_value or robot_type_value == "unknown":
            return None
        candidates = [
            r for r in self.runtimes.all() if r.robot_type == robot_type_value
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _import_lerobot(
        self, path: Path, override: str | None, runtime=None, *, delivery=None
    ):
        info_path = path / "meta/info.json"
        if info_path.is_symlink():
            raise DomainError("LeRobot meta/info.json 不能是符号链接", 400)
        info = _json_object(info_path, "LeRobot meta/info.json")
        version = info.get("codebase_version")
        if version not in LEROBOT_VERSIONS:
            raise DomainError(f"仅支持 LeRobot v2.1 / v3.0，当前为：{version!r}", 400)
        total_episodes, total_frames = (
            info.get("total_episodes"),
            info.get("total_frames"),
        )
        if (
            type(total_episodes) is not int
            or total_episodes < 1
            or type(total_frames) is not int
            or total_frames < 1
        ):
            raise DomainError(
                "LeRobot 元数据缺少有效的 total_episodes / total_frames", 400
            )
        detected = info.get("robot_type")
        if detected is not None and not isinstance(detected, str):
            raise DomainError("LeRobot 元数据中的 robot_type 无效", 400)
        if override and detected and override != detected:
            raise DomainError(
                f"机器人类型冲突：目录声明 {detected}，导入请求为 {override}", 400
            )
        detected = override or (robot_type(detected) if detected else "unknown")
        # A finished delivery is import-only, not an implicit conversion binding.
        if delivery is None:
            runtime = runtime or self._known_runtime(detected)
        if runtime is not None and detected != runtime.robot_type:
            raise DomainError(
                f"LeRobot robot_type={detected!r} 与 Runtime {runtime.id!r} 不匹配", 400
            )
        name = dataset_name(path.name)
        key = digest(
            {
                "kind": "lerobot-import",
                "path": str(path),
                "version": version,
                "runtime_id": runtime.id if runtime else None,
                "robot_type": detected,
                "total_episodes": total_episodes,
                "total_frames": total_frames,
            }
        )[:32]
        if delivery is not None:
            name = dataset_name(delivery["dataset_id"])
            key = digest({"kind": "lerobot-delivery", "delivery_id": delivery["delivery_id"]})[:32]
        with self.lock:
            try:
                row = self.store.get("job", key)
            except DomainError as exc:
                if exc.code != 404:
                    raise
            else:
                if row["state"] in {"DELETING", "DELETE_FAILED", "DELETED"}:
                    raise DomainError("该数据集已删除或正在删除", 409)
                return self._lerobot_response(row)
            row = {
                "id": key,
                "key": "import:" + key,
                "source_kind": "lerobot_import",
                "state": "COMPLETED",
                "stage": "历史 LeRobot 数据集已登记；可预览或运行 Doctor",
                "created_at": now(),
                "updated_at": now(),
                "finished_at": now(),
                "dataset_name": name,
                "instruction": "",
                "source_count": total_episodes,
                "episode_count": total_episodes,
                "frame_count": total_frames,
                "target_format": version,
                "dataset_ready": True,
                "dataset_path": str(path),
                "dataset_available": True,
                "robot_type": detected,
                **(
                    {"runtime_id": runtime.id, "provider": runtime.provider}
                    if runtime is not None
                    else {}
                ),
                "capture_mode": "imported",
                "processing_mode": self.settings.processing_mode,
                "run_doctor": False,
                "error": None,
                "import_metadata": {
                    "path": str(path),
                    "fps": info.get("fps"),
                    "codebase_version": version,
                    "robot_type": detected,
                },
            }
            if delivery is not None:
                row.update(
                    immutable_delivery=True,
                    delivery_id=delivery["delivery_id"],
                    logical_dataset_id=delivery["dataset_id"],
                    feature_schema_id=delivery["feature_schema_id"],
                    delivery_manifest_sha256=delivery["manifest_sha256"],
                    stage="外部 LeRobot v3 交付已校验登记（只读）",
                )
            self.store.put("job", key, row)
            return self._lerobot_response(row)

    def _lerobot_response(self, row):
        return {
            "kind": "lerobot",
            "job_id": row["id"],
            "dataset_name": row["dataset_name"],
            "robot_type": row["robot_type"],
            "runtime_id": row.get("runtime_id"),
            "provider": row.get("provider"),
            "target_format": row["target_format"],
            "episode_count": row["episode_count"],
            "dataset_path": row["dataset_path"],
        }

    def _import_hdf5_episodes(self, path: Path, override: str | None, runtime=None):
        """Register a directory of upstream HDF5 episodes, one upload per file.

        These captures carry no manifest, so the import synthesizes one per
        episode from the trusted Runtime profile. The source directory is only
        read: the manifest lives in the Server store, not beside the data.
        """

        if runtime is None:
            runtime = self._known_runtime(override) if override else None
        if runtime is None:
            raise DomainError(
                "该目录是 HDF5 episode 集合，未声明机器人类型；"
                "请填写机器人类型或指定 Runtime 后重新导入",
                400,
            )
        provider = self.runtimes.provider(runtime)
        formats = [
            value
            for value in (ALOHA_HDF5_FORMAT, ARX_DIRECT_HDF5_FORMAT)
            if value in runtime.raw_formats
        ]
        if not formats:
            raise DomainError(
                f"Runtime {runtime.id!r} 不支持可导入的 HDF5 episode 格式", 400
            )
        raw_format = formats[0]
        inspect = getattr(provider, "episode_metadata", None)
        if inspect is None:
            raise DomainError(
                f"Provider {runtime.provider!r} 不支持导入 HDF5 episode", 503
            )
        files = self._episode_files(path)
        if len(files) > self.settings.max_files:
            raise DomainError("HDF5 episode 数量超过 Server 限制", 413)
        dataset = dataset_name(path.name)
        rows, total = [], 0
        for episode in files:
            try:
                inspect(episode, runtime.profile)
            except DomainError as exc:
                raise DomainError(
                    f"{episode.name} 不符合 {runtime.id} 的 HDF5 布局：{exc}", 422
                ) from exc
            size = episode.stat().st_size
            manifest = {
                "manifest_version": 1,
                "complete": True,
                "session_id": "imp-" + digest(str(episode))[:16],
                "robot_id": self.settings.robot_id,
                "project_id": self.settings.project_id,
                "control_mode": "teleoperation",
                "capture_mode": "ros2",
                "instruction": dataset,
                "dataset_name": dataset,
                "profile": runtime.profile,
                "profile_hash": runtime.profile_hash,
                "raw_format": raw_format,
                "files": [
                    {
                        "path": episode.name,
                        "size_bytes": size,
                        "sha256": file_hash(episode),
                    }
                ],
            }
            rows.append(
                self.ingest.register_import(
                    manifest, episode, episode, runtime=runtime, inline=True
                )
            )
            total += size
        return {
            "kind": "hdf5",
            "collection_id": rows[0]["collection_id"],
            "dataset_name": dataset,
            "robot_type": runtime.robot_type,
            "runtime_id": runtime.id,
            "provider": runtime.provider,
            "episode_count": len(rows),
            "total_bytes": total,
        }

    def _import_raw(self, path: Path, override: str | None, runtime=None):
        manifest_path = path / "manifest.json"
        raw_path = path
        if not manifest_path.is_file() and path.name in {"data", "raw"}:
            manifest_path = path.parent / "manifest.json"
            raw_path = path
        if not manifest_path.is_file():
            raise DomainError(
                "无法识别导入目录：需要包含 meta/info.json 的 LeRobot 数据集、"
                "包含 manifest.json 的原始采集目录，"
                "或包含 episode HDF5 文件的目录",
                400,
            )
        if manifest_path.is_symlink():
            raise DomainError("manifest.json 不能是符号链接", 400)
        manifest = _json_object(manifest_path, "采集 manifest.json")
        if manifest.get("raw_format") != AGILEX_HDF5_FORMAT:
            for child in ("data", "raw"):
                if (path / child / "episode0").is_dir():
                    raw_path = path / child
                    break
            if not (raw_path / "episode0").is_dir():
                raise DomainError("原始采集目录缺少 episode0", 400)
        profile = manifest.get("profile")
        detected = profile.get("robot_type") if isinstance(profile, dict) else None
        if detected is not None and not isinstance(detected, str):
            raise DomainError("采集 Manifest 中的 robot_type 无效", 400)
        if override and detected and override != detected:
            raise DomainError(
                f"机器人类型冲突：Manifest 声明 {detected}，导入请求为 {override}", 400
            )
        runtime = runtime or self.runtimes.for_manifest(manifest)
        self.runtimes.validate_capture(runtime, manifest)
        files = manifest.get("files")
        if not isinstance(files, list):
            raise DomainError("采集 Manifest 缺少文件清单", 400)
        for spec in files:
            if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
                raise DomainError("采集 Manifest 文件清单格式无效", 400)
            source = confined(raw_path, spec["path"])
            if not source.is_file() or source.stat().st_size != spec.get("size_bytes"):
                raise DomainError(f"原始文件缺失或大小不符：{spec['path']}", 422)
        row = self.ingest.register_import(
            manifest, manifest_path, raw_path, runtime=runtime
        )
        return {
            "kind": "raw",
            "upload_id": row["id"],
            "collection_id": row["collection_id"],
            "dataset_name": row["dataset_name"],
            "robot_type": row["robot_type"],
            "runtime_id": row.get("runtime_id"),
            "provider": row.get("provider"),
            "file_count": row["file_count"],
            "total_bytes": row["total_bytes"],
        }
