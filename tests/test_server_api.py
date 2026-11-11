import hashlib

import pytest
from fastapi.testclient import TestClient
from test_ingest import manifest

from pipeline.server.app import create_app


def test_device_isolation_and_ownership(settings_factory, monkeypatch):
    monkeypatch.setenv("ROBOT_ONE_SECRET", "first-device-" + "a" * 32)
    monkeypatch.setenv("ROBOT_TWO_SECRET", "second-device-" + "b" * 32)
    s = settings_factory(
        "server",
        devices=[
            {
                "robot_id": "r1",
                "project_id": "p1",
                "token_env": "ROBOT_ONE_SECRET",
                "targets": ["default"],
            },
            {
                "robot_id": "r2",
                "project_id": "p2",
                "token_env": "ROBOT_TWO_SECRET",
                "targets": ["default"],
            },
        ],
    )
    first = {"Authorization": "Bearer " + "first-device-" + "a" * 32}
    second = {"Authorization": "Bearer " + "second-device-" + "b" * 32}
    human = {"Authorization": "Bearer " + s.token}
    with TestClient(create_app(s, background=False)) as http:
        assert http.get("/device/targets", headers=human).status_code == 401
        assert http.get("/api/jobs", headers=first).status_code == 401
        m, data = manifest(s)
        m.update(robot_id="r1", project_id="p1")
        result = http.post(
            "/device/uploads", headers=first, json={"manifest": m, "target": "default"}
        )
        assert result.status_code == 201, result.text
        uid = result.json()["id"]
        assert http.get("/device/uploads/" + uid, headers=second).status_code == 403
        assert (
            http.post(
                "/device/uploads/" + uid + "/complete", headers=second
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/device/uploads",
                headers=second,
                json={"manifest": m, "target": "default"},
            ).status_code
            == 403
        )
        assert (
            http.put(
                f"/device/uploads/{uid}/files/0?offset=0",
                content=data,
                headers={**first, "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
            ).status_code
            == 200
        )
        assert (
            http.post("/device/uploads/" + uid + "/complete", headers=first).status_code
            == 200
        )


def test_malformed_requests_and_login_limits(settings_factory):
    s = settings_factory("server")
    with TestClient(create_app(s, background=False)) as http:
        assert http.post("/api/login", content=b"not-json").status_code == 400
        assert (
            http.post(
                "/api/login",
                json={"token": s.token},
                headers={"Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        assert http.post("/api/login", content=b"x" * 5000).status_code == 413
        for _ in range(10):
            assert (
                http.post("/api/login", json={"token": "incorrect"}).status_code == 401
            )
        assert http.post("/api/login", json={"token": "incorrect"}).status_code == 429


def test_symlink_file_prefix_and_quota(settings_factory, tmp_path):
    from pipeline.server.ingest import IngestService
    from pipeline.shared.core import DomainError

    s = settings_factory("server", max_upload_bytes=4)
    i = IngestService(s)
    m, data = manifest(s)
    with pytest.raises(DomainError):
        i.create(m, "default", "")
    s.max_upload_bytes = 100000
    m["files"] = [
        {"path": "episode0/a", "size_bytes": 1, "sha256": "a" * 64},
        {"path": "episode0/a/b", "size_bytes": 1, "sha256": "b" * 64},
    ]
    with pytest.raises(DomainError):
        i.create(m, "default", "")
    m, data = manifest(s)
    r = i.create(m, "default", "")
    (i.root(r) / "data/episode0").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError):
        i.chunk(r["id"], 0, 0, hashlib.sha256(data).hexdigest(), data)


def test_report_page_is_public_but_data_requires_login(settings_factory):
    s = settings_factory("server")
    with TestClient(create_app(s, background=False)) as http:
        for path in ("/report", "/report/some-job-id"):
            page = http.get(path)
            assert page.status_code == 200
            assert "数据质检报告" in page.text
        assert http.get("/assets/report.js").status_code == 200
        assert http.get("/assets/report.css").status_code == 200
        assert http.get("/api/jobs").status_code == 401
        assert (
            http.get("/api/jobs", headers={"Authorization": "Bearer " + s.token})
        ).status_code == 200


def test_server_page_scripts_load_before_login(settings_factory):
    s = settings_factory("server")
    with TestClient(create_app(s, background=False)) as http:
        page = http.get("/")
        assert page.status_code == 200
        # The page's own scripts must load before login: a script that 401s is
        # never retried by boot(), so its panel would silently stay dead.
        for name in (
            "theme.js",
            "clipboard.js",
            "client-replay.js",
            "app.js",
            "annotations.js",
        ):
            assert f"/assets/{name}" in page.text
            assert http.get(f"/assets/{name}").status_code == 200, name
        assert http.get("/api/annotations").status_code == 401


def test_job_list_summary_omits_detail_reports(settings_factory):
    s = settings_factory("server")
    app = create_app(s, background=False)
    row = {
        "id": "job-summary",
        "key": "job-summary",
        "created_at": 1,
        "state": "CONVERTED",
        "robot_report": {"detail": "x" * 100},
        "alignment_report": {"frames": 1},
        "episode_diagnostics": [{"passed": True}],
        "source_manifest_hashes": {"file": "hash"},
        "selection_report": {"kept": 1},
    }
    app.state.ingest.store.put("job", row["id"], row)
    headers = {"Authorization": "Bearer " + s.token}
    with TestClient(app) as http:
        full = http.get("/api/jobs", headers=headers).json()[0]
        slim = http.get("/api/jobs?summary=true", headers=headers).json()[0]
    # Progress rendering keeps the light reports; the heavy ones stay in detail.
    assert slim["selection_report"] == {"kept": 1}
    for field in (
        "robot_report",
        "alignment_report",
        "episode_diagnostics",
        "source_manifest_hashes",
    ):
        assert field in full
        assert field not in slim


def test_report_page_is_server_only():
    from fastapi import FastAPI

    from pipeline.shared.core import DomainError
    from pipeline.shared.web import mount_ui

    app = FastAPI()
    mount_ui(app)
    with TestClient(app) as http:
        assert http.get("/report").status_code == 404
        with pytest.raises(DomainError):
            http.get("/assets/report.js")
        assert http.get("/assets/app.js").status_code == 200
