import hashlib
import json
from pathlib import Path

import httpx
import pytest
from test_client import request
from test_g1_client import episode
from test_g1_client import native as native_fixture

from pipeline.client.resources import WorkPaused
from pipeline.client.upload import UploadAgent
from pipeline.shared.core import digest

native = native_fixture


def ready(service, adapter):
    batch = service.begin_batch(request())
    packet, _ = episode(adapter)
    # Enlarge the payload to exercise more than one bounded chunk. Integrity
    # upload tests do not interpret the raw; conversion tests use valid raw.
    packet.write_bytes(packet.read_bytes() * 100)
    metadata = packet.with_name("episode.json")
    ep = json.loads(metadata.read_text())
    ep.update(
        bytes=packet.stat().st_size,
        sha256=hashlib.sha256(packet.read_bytes()).hexdigest(),
    )
    metadata.write_text(json.dumps(ep))
    service.sync_external()
    service.end_batch(batch["id"])
    service.seal_pending()
    return service.store.all("capture")[0], packet


class Remote:
    def __init__(self):
        self.manifest = None
        self.data = []
        self.puts = []
        self.on_chunk = None

    def __call__(self, req):
        if req.url.path == "/device/uploads":
            self.manifest = json.loads(req.content)["manifest"]
            if not self.data:
                self.data = [bytearray() for _ in self.manifest["files"]]
            result = {"id": "upload-1"}
        elif req.method == "GET":
            result = {
                "manifest_hash": digest(self.manifest),
                "offsets": list(map(len, self.data)),
                "chunk_bytes": 65536,
                "state": "RECEIVING",
            }
        elif req.method == "PUT":
            index = int(req.url.path.rsplit("/", 1)[-1])
            offset = int(req.url.params["offset"])
            assert offset == len(self.data[index])
            assert len(req.content) <= 65536
            assert (
                hashlib.sha256(req.content).hexdigest() == req.headers["X-Chunk-SHA256"]
            )
            self.data[index].extend(req.content)
            self.puts.append((index, offset))
            if self.on_chunk:
                self.on_chunk()
            result = {"offset": len(self.data[index])}
        else:
            for spec, content in zip(self.manifest["files"], self.data, strict=True):
                assert spec["size_bytes"] == len(content)
                assert spec["sha256"] == hashlib.sha256(content).hexdigest()
            result = {"state": "COMMITTED", "manifest_hash": digest(self.manifest)}
        return httpx.Response(200, json=result)


def transport(monkeypatch, remote):
    original = httpx.Client
    monkeypatch.setattr(
        "pipeline.client.upload.httpx.Client",
        lambda **kw: original(transport=httpx.MockTransport(remote), **kw),
    )


def test_chunk_pause_resumes_remote_offset_without_retry_or_raw_pruning(
    native, monkeypatch
):
    service, adapter = native
    row, packet = ready(service, adapter)
    remote = Remote()
    transport(monkeypatch, remote)
    remote.on_chunk = service.batch_active.set
    uploader = UploadAgent(service)
    uploader.process_once()
    paused = service.store.get("capture", row["id"])
    assert paused["upload_state"] == "PAUSED"
    assert paused.get("attempts", 0) == 0
    assert len(remote.puts) == 1
    assert paused["uploaded_bytes"] == 65536
    service.batch_active.clear()
    remote.on_chunk = None
    uploader.process_once()
    completed = service.store.get("capture", row["id"])
    assert completed["upload_state"] == "ACKED"
    assert remote.puts[1] == (0, 65536)
    assert packet.is_file()
    assert Path(row["raw_root"]).exists()


def test_source_mutation_invalidates_cached_hash(native, monkeypatch):
    service, adapter = native
    row, packet = ready(service, adapter)
    packet.write_bytes(b"corrupt")
    remote = Remote()
    transport(monkeypatch, remote)
    UploadAgent(service).process_once()
    result = service.store.get("capture", row["id"])
    assert result["upload_state"] == "RETRY"
    assert remote.puts == []


def test_hash_can_pause_and_continue_in_bounded_memory(native, monkeypatch):
    service, _ = native
    path = service.settings.root / "hash-source"
    path.write_bytes(b"a" * 200000)
    work = service.background
    work.config["chunk_bytes"] = 65536
    checked = [0]
    original = work.checkpoint

    def checkpoint():
        checked[0] += 1
        if checked[0] == 2:
            raise WorkPaused("busy")
        original()

    monkeypatch.setattr(work, "checkpoint", checkpoint)
    with pytest.raises(WorkPaused):
        work.hash_file(path)
    assert work.hashes[str(path)][2] == 65536
    monkeypatch.setattr(work, "checkpoint", original)
    assert work.hash_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not work.hashes


def test_actual_server_routes_commit_native_manifest(
    native, settings_factory, monkeypatch
):
    from fastapi.testclient import TestClient

    from pipeline.server.app import create_app
    from pipeline.server.jobs import JobRunner

    service, adapter = native
    row, packet = ready(service, adapter)
    settings = settings_factory(
        "server", profiles=service.settings.profiles, default_profile="g1"
    )
    app = create_app(settings, background=False)
    original = httpx.Client
    with TestClient(app) as server:

        def handle(req):
            response = server.request(
                req.method, str(req.url), content=req.content, headers=dict(req.headers)
            )
            return httpx.Response(
                response.status_code, content=response.content, headers=response.headers
            )

        monkeypatch.setattr(
            "pipeline.client.upload.httpx.Client",
            lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
        )
        UploadAgent(service).process_once()
        completed = service.store.get("capture", row["id"])
        assert completed["upload_state"] == "ACKED", completed
        ingest = app.state.ingest
        uploaded = ingest.store.get("upload", completed["upload_id"])
        assert uploaded["provider"] == "g1_sonic" and uploaded["capture_mode"] == "real"
        received = ingest.root(uploaded) / "data/episode0/packets.raw"
        assert received.read_bytes() == packet.read_bytes()
        runner = JobRunner(settings, ingest)
        job = runner.enqueue(uploaded["id"])
        runner.tick()
        assert ingest.store.get("job", job["id"])["state"] == "BLOCKED"
