from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class CaptureAdapter(Protocol):
    # Optional external-trigger extension: begin/end/observe/recover. Legacy
    # adapters remain command-triggered; capabilities drive the generic UI.
    capabilities: dict

    def health(self) -> dict: ...
    def start(self, session_id: str, raw_root: Path, instruction: str) -> dict: ...
    def stop(self, session_id: str) -> dict: ...
    def status(self) -> dict: ...
    def frame(self, camera_id: str) -> bytes | None: ...
    def close(self): ...


class ExternalCaptureAdapter(CaptureAdapter, Protocol):
    """External triggers remain adapter-owned; observations are durable snapshots.

    observe(): key/index, Client state, final, started_at/stopped_at (Unix seconds),
    count/count_unit, size_bytes, metadata_hash and packager source paths.
    status(): normalized Client state, episode_index, frame_count, optional error.
    end() must prove recorder exit; release() only drops ownership after final sync.
    launch_attempted=False certifies no child was launched by the last begin().
    """

    launch_attempted: bool

    def begin(self, batch: dict, persist) -> dict: ...
    def recover(self, batch: dict): ...
    def observe(self) -> list[dict]: ...
    def end(self): ...
    def interrupt(self): ...
    def release(self): ...


@dataclass
class PreviewStream:
    content_type: str
    chunks: AsyncIterator[bytes]


class StreamingPreviewAdapter(CaptureAdapter, Protocol):
    """Optional MJPEG preview with Content-Length on each JPEG part.

    The source drives delivery; implementations must release upstream resources
    when the context closes. No collection or motion process may be started.
    """

    def open_preview(
        self, camera_id: str
    ) -> AbstractAsyncContextManager[PreviewStream]: ...


class TelemetryAdapter(CaptureAdapter, Protocol):
    """Optional read-only numeric telemetry exposed by a capture Adapter."""

    def telemetry_signals(self) -> dict: ...
    def telemetry_history(self, signals: list[str]) -> dict: ...


class ManagedCaptureAdapter(ExternalCaptureAdapter, Protocol):
    """Optional service control, advertised through service_control capabilities.

    Frontend options and commands are adapter-declared. end() returns False while
    asynchronous shutdown is pending; status.service_stopped proves completion.
    No startup is implied by health polling, recovery, or Client shutdown.
    """

    session: dict | None

    def prepare_service(self) -> dict: ...
    def configure_service(self, options: dict) -> dict: ...
    def refresh_service(self) -> dict: ...
    def service_status(self) -> dict: ...
    def background_blocker(self) -> str: ...
    def command(self, action: str, request_id: str) -> dict: ...
    def end(self, force: bool = False) -> bool: ...
