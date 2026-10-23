"""Apex recording only. This adapter has no motion, mode, enable or replay API."""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from pipeline.shared.core import DomainError, atomic_json
from pipeline.shared.tianji import CAMERAS, recording_files


class ApexAdapter:
    def __init__(self, settings, *, http=None):
        if settings.profile().get("robot_type") != "tianji_marvin_pro":
            raise ValueError("Apex capture requires tianji_marvin_pro Profile")
        self.options = settings.capture_options
        base = self.options.get("url", "http://127.0.0.1:8080")
        parsed = urlparse(base)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("Apex recording API must be loopback HTTP")
        self.http = http or httpx.Client(base_url=base, timeout=10, trust_env=False)
        self.lock = threading.RLock()
        self.journal = settings.root / "apex-capture.json"
        self.row = json.loads(self.journal.read_text()) if self.journal.exists() else {}

    def _save(self, **fields):
        self.row.update(fields)
        atomic_json(self.journal, self.row)

    def _call(self, method, route, **kwargs):
        # Keep this allowlist small even if new Apex endpoints are added later.
        allowed = {
            ("GET", "status"),
            ("GET", "projects"),
            ("POST", "start"),
            ("POST", "stop"),
        }
        if (method, route) not in allowed:
            raise DomainError("Unsupported recording operation")
        try:
            response = self.http.request(method, "/api/v1/record/" + route, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if payload.get("ok") is not True or not isinstance(
                payload.get("data"), dict
            ):
                raise DomainError("Apex recorder returned an invalid response")
            data = payload["data"]
            if data.get("status") == "error":
                raise DomainError(data.get("error_message") or "Apex recorder error")
            return data
        except httpx.HTTPError as exc:
            raise DomainError(
                "Apex recording result unknown; not automatically retried", 503
            ) from exc

    def health(self):
        status = self._call("GET", "status")
        projects = self._call("GET", "projects").get("projects", [])
        project = next(
            (p for p in projects if p["id"] == self.options.get("project_id")), {}
        )
        reasons = []
        if self.options.get("allow_recording") is not True:
            reasons.append("Recording disabled in deployment configuration")
        if not project or project.get("available") is not True:
            reasons.append("Dedicated Apex project/storage not available")
        elif (
            Path(project.get("storage_dir", "")).resolve()
            != Path(self.options["storage_root"]).resolve()
        ):
            reasons.append(
                "Dedicated Apex project path differs from configured storage root"
            )
        if status.get("phase") not in {"idle", "saved", "cancelled"}:
            reasons.append("Apex recorder is occupied")
        return dict(
            ready=not reasons,
            reasons=reasons,
            mode="ros2",
            recorder=status,
            motion_control=False,
        )

    def status(self):
        status = self._call("GET", "status")
        owned = bool(
            self.row.get("apex_session_id") == status.get("session_id")
            and self.row.get("session_id")
        )
        return dict(
            phase=status.get("phase"),
            session_id=self.row.get("session_id") if owned else None,
            fail=owned and status.get("phase") in {"error", "cancelled", "failed"},
            recorder=status,
        )

    def _wait(self, predicate):
        deadline = time.monotonic() + self.options.get("timeout_s", 30)
        while True:
            status = self._call("GET", "status")
            if status.get("session_id") != self.row.get("apex_session_id"):
                raise DomainError("Apex recording ownership changed")
            if predicate(status):
                return status
            if status.get("phase") in {"error", "failed", "cancelled"}:
                raise DomainError(
                    "Apex recording failed: " + str(status.get("error_message"))
                )
            if time.monotonic() >= deadline:
                raise DomainError("Apex writer state unknown; manual review required")
            time.sleep(0.1)

    def start(self, session_id, raw_root, instruction):
        with self.lock:
            if self.row.get("session_id") == session_id:
                status = self._call("GET", "status")
                if (
                    status.get("request_id") == session_id
                    and status.get("session_id")
                    and status.get("phase") == "recording"
                    and status.get("data_state") == "recording"
                    and status.get("video_state") == "recording"
                ):
                    self._save(apex_session_id=status["session_id"], phase="RECORDING")
                    return dict(started=True, session_id=session_id)
                raise DomainError("Existing start outcome unknown; do not resend")
            if self.row.get("phase") in {"STARTING", "RECORDING", "STOPPING"}:
                raise DomainError("Previous recording must be resolved first")
            checked = self.health()
            if not checked["ready"]:
                raise DomainError("; ".join(checked["reasons"]))
            self._save(
                session_id=session_id,
                phase="STARTING",
                raw_root=str(raw_root),
                apex_session_id=None,
                instruction=instruction,
            )
            result = self._call(
                "POST",
                "start",
                json=dict(request_id=session_id, project_id=self.options["project_id"]),
            )
            if not result.get("session_id") or result.get("request_id") != session_id:
                raise DomainError("Apex did not acknowledge this recording request")
            self._save(apex_session_id=result["session_id"])
            self._wait(
                lambda s: (
                    s.get("phase") == "recording"
                    and s.get("data_state") == "recording"
                    and s.get("video_state") == "recording"
                )
            )
            self._save(phase="RECORDING")
            return dict(started=True, session_id=session_id)

    def stop(self, session_id):
        with self.lock:
            if self.row.get("session_id") != session_id:
                raise DomainError("This Client does not own that recording")
            if self.row.get("phase") == "STOPPED":
                return dict(stopped=True, session_id=session_id)
            status = self._call("GET", "status")
            if (
                not self.row.get("apex_session_id")
                and status.get("request_id") == session_id
            ):
                self._save(apex_session_id=status.get("session_id"))
            if (
                not self.row.get("apex_session_id")
                or status.get("session_id") != self.row["apex_session_id"]
            ):
                raise DomainError("Refusing to stop another Apex recording")
            self._save(phase="STOPPING")
            if status.get("phase") not in {"saved", "saving"}:
                self._call(
                    "POST", "stop", json=dict(session_id=self.row["apex_session_id"])
                )
            status = self._wait(
                lambda s: (
                    s.get("phase") == "saved"
                    and s.get("data_state") == "closed"
                    and s.get("video_state") == "closed"
                )
            )
            source = Path(status["path"])
            storage = Path(self.options["storage_root"]).resolve()
            if (
                source.resolve() != source
                or not source.is_relative_to(storage)
                or source == storage
            ):
                raise DomainError("Apex output is outside the dedicated storage root")
            recording_files(source)
            # Apex owns its original. Only our managed copy can be discarded/uploaded.
            target = Path(self.row["raw_root"]) / "episode0"
            temporary = target.with_name(".copying")
            if target.exists() or temporary.exists():
                raise DomainError("Existing copy requires review; refusing overwrite")
            shutil.copytree(source, temporary)
            recording_files(temporary)
            atomic_json(
                temporary / "bodypipeline-source.json",
                dict(
                    source_path=str(source),
                    apex_session_id=self.row["apex_session_id"],
                    session_id=session_id,
                    writer_status=status,
                ),
            )
            temporary.rename(target)
            self._save(phase="STOPPED", source_path=str(source))
            return dict(stopped=True, session_id=session_id, writer_closed=True)

    def frame(self, camera_id):
        if camera_id not in CAMERAS or not self.options.get("preview_root"):
            return None
        root = Path(self.options["preview_root"])
        try:
            meta = json.loads((root / "latest.json").read_text())
            now = time.time()
            if (
                not 0 <= now - meta["source_time"] <= 0.75
                or not 0 <= now - meta["received_time"] <= 0.5
            ):
                return None
            name = meta["files"][camera_id]
            if Path(name).name != name:
                return None
            return (root / name).read_bytes()
        except (OSError, KeyError, ValueError):
            return None

    def close(self):
        self.http.close()  # Never implicitly stop recording or alter the robot.
