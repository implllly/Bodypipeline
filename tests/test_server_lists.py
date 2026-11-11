import copy
import gzip
import hashlib

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from test_ingest import manifest

from pipeline.server.app import create_app


def test_collection_summary_preserves_selection_and_runtime_checks(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    ingest = app.state.ingest
    m, data = manifest(s)
    first = ingest.create(m, "default", "")
    ingest.chunk(first["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)
    ingest.complete(first["id"])
    second = ingest.create({**m, "session_id": "still-uploading"}, "default", "")
    # Inline manifests are used by imports and must never travel in list polls.
    inline = copy.deepcopy(m)
    inline["session_id"] = "imported-episode"
    inline["provenance"] = {"original_metadata": "x" * 100_000}
    imported = ingest.register_import(
        inline, s.root / "source/manifest.json", s.root / "source", inline=True
    )
    headers = {"Authorization": "Bearer " + s.token}
    with TestClient(app) as http:
        assert http.get("/api/collections?summary=true").status_code == 401
        assert (
            http.get(
                "/api/collections?summary=true",
                headers={"Authorization": "Bearer " + s.device_token},
            ).status_code
            == 401
        )
        full_response = http.get("/api/collections", headers=headers)
        slim_response = http.get("/api/collections?summary=true", headers=headers)
        full = full_response.json()
        slim = slim_response.json()
        assert len(slim_response.content) < len(full_response.content) / 10
        for group, summary in zip(full, slim, strict=True):
            assert {k: v for k, v in group.items() if k != "uploads"} == {
                k: v for k, v in summary.items() if k != "uploads"
            }
            for original, episode in zip(
                group["uploads"], summary["uploads"], strict=True
            ):
                for key in (
                    "id",
                    "state",
                    "robot_id",
                    "robot_type",
                    "runtime_id",
                    "total_bytes",
                    "file_count",
                    "created_at",
                    "conversion_blockers",
                ):
                    assert episode.get(key) == original.get(key)
                assert "manifest_inline" not in episode
                assert "raw_path" not in episode
        episodes = [u for group in slim for u in group["uploads"]]
        assert {u["id"] for u in episodes} == {
            first["id"],
            second["id"],
            imported["id"],
        }
        assert next(u for u in episodes if u["id"] == imported["id"])["import_path"]
        # Existing upload clients and the full collection API retain the manifest.
        uploads = http.get("/api/uploads", headers=headers).json()
        assert (
            next(u for u in uploads if u["id"] == imported["id"])["manifest_inline"]
            == inline
        )


def test_collection_summary_keeps_manifest_failures_visible(
    settings_factory, monkeypatch
):
    s = settings_factory("server")
    app = create_app(s, background=False)
    ingest = app.state.ingest
    m, _ = manifest(s)
    row = ingest.create(m, "default", "")

    def unavailable(*args, **kwargs):
        raise OSError("source unavailable")

    monkeypatch.setattr(ingest, "manifest", unavailable)
    with TestClient(app) as http:
        response = http.get(
            "/api/collections?summary=true",
            headers={"Authorization": "Bearer " + s.token},
        )
        assert response.status_code == 200
        episode = response.json()[0]["uploads"][0]
        assert episode["id"] == row["id"]
        assert episode["conversion_blockers"][0]["field"] == "manifest"


def test_job_summary_keeps_reports_in_detail(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    row = {
        "id": "large-job",
        "state": "FAILED",
        "created_at": 1,
        "profile": {"robot_type": "example", "large_metadata": "x" * 100_000},
        "doctor_report": {"checks": [{"messages": "x" * 100_000}]},
        "trim_report": {"episodes": ["x" * 100_000]},
        "upload_ids": [f"episode-{n}" for n in range(1000)],
        "selection_report": {
            "input_count": 1000,
            "accepted_count": 999,
            "skipped_count": 1,
        },
        "source_job_ids": ["source-one", "source-two"],
        "error": "conversion failed",
    }
    app.state.ingest.store.put("job", row["id"], row)
    headers = {"Authorization": "Bearer " + s.token}
    with TestClient(app) as http:
        response = http.get("/api/jobs?summary=true", headers=headers)
        summary = response.json()[0]
        assert len(response.content) < 2000
        assert summary["robot_type"] == "example"
        for field in ("selection_report", "source_job_ids", "error"):
            assert summary[field] == row[field]
        for path in ("/api/jobs", "/api/jobs/large-job"):
            full = http.get(path, headers=headers).json()
            if isinstance(full, list):
                full = full[0]
            for field in ("profile", "doctor_report", "trim_report", "upload_ids"):
                assert field not in summary
                assert full[field] == row[field]


def test_list_compression_negotiation_does_not_touch_device_or_media(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    for n in range(20):
        app.state.ingest.store.put(
            "job",
            str(n),
            {
                "id": str(n),
                "state": "FAILED",
                "created_at": n,
                "error": "reason " * 100,
            },
        )

    @app.get("/api/test-stream")
    def media():
        return StreamingResponse(
            iter([b"frame" * 1000]), media_type="multipart/x-mixed-replace"
        )

    headers = {"Authorization": "Bearer " + s.token}
    with TestClient(app) as http:
        compressed = http.get(
            "/api/jobs?summary=true", headers={**headers, "Accept-Encoding": "gzip"}
        )
        plain = http.get(
            "/api/jobs?summary=true", headers={**headers, "Accept-Encoding": "identity"}
        )
        assert compressed.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in compressed.headers["vary"].lower()
        assert "content-encoding" not in plain.headers
        assert compressed.json() == plain.json()
        with http.stream(
            "GET",
            "/api/jobs?summary=true",
            headers={**headers, "Accept-Encoding": "gzip"},
        ) as response:
            wire = b"".join(response.iter_raw())
        assert gzip.decompress(wire) == plain.content
        assert len(wire) < len(plain.content) / 5
        stream = http.get(
            "/api/test-stream", headers={**headers, "Accept-Encoding": "gzip"}
        )
        assert stream.content == b"frame" * 1000
        assert "content-encoding" not in stream.headers
        targets = http.get(
            "/device/targets",
            headers={
                "Authorization": "Bearer " + s.device_token,
                "Accept-Encoding": "gzip",
            },
        )
        assert targets.status_code == 200
        assert "content-encoding" not in targets.headers
