import json
import time

import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.viewer import ViewerManager, create_gateway
from pipeline.server.viewer_bootstrap import UPSTREAM, patch_cache
from pipeline.server.viewer_data import PreviewRegistry, repo_id_for
from pipeline.shared.core import DomainError, Store


def make_dataset(root, name="ds"):
    data = root / name
    (data / "meta").mkdir(parents=True)
    (data / "meta/info.json").write_text(json.dumps({"fps": 30}))
    (data / "videos").mkdir()
    (data / "videos/a.mp4").write_bytes(b"0123456789")
    return data


def test_registry_registers_expires_and_removes(settings_factory, tmp_path):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    registry = PreviewRegistry(store, ttl=60)
    data = make_dataset(tmp_path)
    row = registry.register("job-1", data)
    assert row["repo_id"] == repo_id_for("job-1") == "bp/job-1"
    assert registry.lookup("bp/job-1")["dataset_path"] == str(data)
    assert registry.lookup("bp/other") is None
    assert registry.lookup("evil/job-1") is None
    assert registry.lookup("bp/../job-1") is None
    again = registry.register("job-1", data)
    assert again["created_at"] == row["created_at"]
    registry.remove("job-1")
    assert registry.lookup("bp/job-1") is None
    registry.remove("job-1")

    registry.register("job-2", data)
    stale = store.get("preview", "job-2")
    stale["expires_at"] = time.time() - 1
    store.put("preview", "job-2", stale)
    assert registry.lookup("bp/job-2") is None
    assert registry.prune() == 1
    assert registry.active() == []


def test_many_datasets_are_served_side_by_side(settings_factory, tmp_path):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    registry = PreviewRegistry(store, ttl=300)
    first = make_dataset(tmp_path, "one")
    second = make_dataset(tmp_path, "two")
    (second / "videos/a.mp4").write_bytes(b"ABCDEFGHIJ")
    registry.register("job-a", first)
    registry.register("job-b", second)
    store.put("viewer", "service", {"state": "READY"})
    with TestClient(create_gateway(s)) as browser:
        browser.post("/api/login", json={"token": s.token})
        a = browser.get("/_data/bp/job-a/resolve/main/videos/a.mp4")
        b = browser.get("/_data/bp/job-b/resolve/main/videos/a.mp4")
        assert a.content == b"0123456789" and b.content == b"ABCDEFGHIJ"
        ranged = browser.get(
            "/_data/bp/job-a/resolve/main/videos/a.mp4",
            headers={"Range": "bytes=2-5"},
        )
        assert ranged.status_code == 206
        assert ranged.content == b"2345"
        assert ranged.headers["content-range"] == "bytes 2-5/10"
        suffix = browser.get(
            "/_data/bp/job-a/resolve/main/videos/a.mp4",
            headers={"Range": "bytes=-3"},
        )
        assert suffix.status_code == 206 and suffix.content == b"789"
        bad = browser.get(
            "/_data/bp/job-a/resolve/main/videos/a.mp4",
            headers={"Range": "bytes=50-60"},
        )
        assert bad.status_code == 416
        multi = browser.get(
            "/_data/bp/job-a/resolve/main/videos/a.mp4",
            headers={"Range": "bytes=0-1,3-4"},
        )
        assert multi.status_code == 416
        head = browser.head("/_data/bp/job-a/resolve/main/videos/a.mp4")
        assert head.status_code == 200


def test_data_service_confines_paths_and_unknown_repos(settings_factory, tmp_path):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    registry = PreviewRegistry(store, ttl=300)
    data = make_dataset(tmp_path)
    (tmp_path / "secret.txt").write_text("secret")
    (data / "link").symlink_to(tmp_path / "secret.txt")
    registry.register("job-a", data)
    store.put("viewer", "service", {"state": "READY"})
    with TestClient(create_gateway(s)) as browser:
        browser.post("/api/login", json={"token": s.token})
        for path in (
            "/_data/bp/job-a/resolve/main/../secret.txt",
            "/_data/bp/job-a/resolve/main/%2e%2e/secret.txt",
            "/_data/bp/job-a/resolve/main/link",
            "/_data/bp/job-a/resolve/main/missing.bin",
            "/_data/bp/job-a/resolve/dev/videos/a.mp4",
            "/_data/bp/job-a/videos/a.mp4",
        ):
            assert browser.get(path).status_code == 404, path
        gone = browser.get("/_data/bp/nope/resolve/main/meta/info.json")
        assert gone.status_code == 404
        assert "预览已过期" in gone.json()["detail"]
        # Cookie-authenticated writes require a same-origin Origin header.
        assert browser.post("/_data/bp/job-a/resolve/main/x").status_code == 403
        assert (
            browser.post(
                "/_data/bp/job-a/resolve/main/x",
                headers={"Origin": "http://testserver"},
            ).status_code
            == 405
        )


def test_gateway_proxies_ui_when_service_ready(settings_factory, monkeypatch):
    import httpx

    s = settings_factory("viewer")
    store = Store(s.root / "state")
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"<html>ui</html>"))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "pipeline.server.viewer.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    with TestClient(create_gateway(s)) as browser:
        browser.post("/api/login", json={"token": s.token})
        waiting = browser.get("/bp/job-a/episode_0", headers={"Accept": "text/html"})
        assert waiting.status_code == 200
        assert 'http-equiv="refresh"' in waiting.text
        assert "正在启动 LeRobot Visualizer" in waiting.text
        assert browser.get("/bp/job-a/episode_0").status_code == 503

        store.put("viewer", "service", {"state": "READY"})
        page = browser.get(
            "/bp/job-a/episode_0",
            headers={"Authorization": "Bearer " + s.token},
        )
        assert page.status_code == 200 and page.content == b"<html>ui</html>"
        assert (
            str(seen[-1].url)
            == f"http://127.0.0.1:{s.viewer_ui_port}/bp/job-a/episode_0"
        )
        assert "cookie" not in seen[-1].headers
        assert "authorization" not in seen[-1].headers


def test_root_redirects_to_latest_preview(settings_factory, tmp_path):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    registry = PreviewRegistry(store, ttl=300)
    store.put("viewer", "service", {"state": "READY"})
    with TestClient(create_gateway(s)) as browser:
        browser.post("/api/login", json={"token": s.token})
        empty = browser.get("/", follow_redirects=False)
        assert empty.status_code == 503
        assert "预览尚未就绪" in empty.json()["detail"]
        data = make_dataset(tmp_path)
        registry.register("job-a", data)
        row = store.get("preview", "job-a")
        row["created_at"] = time.time() - 100
        store.put("preview", "job-a", row)
        registry.register("job-b", data)
        root = browser.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/bp/job-b/episode_0"


def test_doctor_is_computed_per_dataset_and_rendered_light(
    settings_factory, tmp_path, monkeypatch
):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    registry = PreviewRegistry(store, ttl=300)
    data = make_dataset(tmp_path)
    registry.register("job-a", data)
    registry.register("job-b", make_dataset(tmp_path, "other"))
    calls = []

    class Result:
        returncode = 1
        stderr = ""

        def __init__(self, path):
            self.stdout = json.dumps(
                {
                    "overall_severity": "WARN",
                    "total_episodes": 2,
                    "total_frames": 10,
                    "checks": [
                        {
                            "name": "Action Quality",
                            "severity": "WARN",
                            "messages": [
                                {"severity": "WARN", "message": f"<b>{path}</b>"}
                            ],
                        }
                    ],
                }
            )

    def fake_run(command, **kwargs):
        calls.append(command)
        return Result(command[3])

    monkeypatch.setattr("pipeline.server.viewer.subprocess.run", fake_run)
    monkeypatch.setattr(
        "pipeline.server.viewer.executable_path", lambda value: "/bin/letools"
    )
    with TestClient(create_gateway(s)) as browser:
        browser.post("/api/login", json={"token": s.token})
        page = browser.get("/_data/doctor/?dataset=bp/job-a")
        assert page.status_code == 200
        assert "color-scheme: light" in page.text
        assert "&lt;b&gt;" in page.text and "<b>" not in page.text
        assert str(data) in page.text
        again = browser.get("/_data/doctor?dataset=bp/job-a")
        assert again.status_code == 200 and len(calls) == 1
        other = browser.get("/_data/doctor/report.json?dataset=bp/job-b")
        assert other.json()["overall_severity"] == "WARN" and len(calls) == 2
        assert browser.get("/_data/doctor/?dataset=bp/none").status_code == 404
        assert browser.get("/_data/doctor/").status_code == 404


def test_open_registers_without_starting_a_process_per_dataset(
    settings_factory, tmp_path, monkeypatch
):
    s = settings_factory("server")
    store = Store(s.root / "state")
    manager = ViewerManager(s, store)
    data = make_dataset(tmp_path)
    started = []
    monkeypatch.setattr(
        "pipeline.server.viewer.dataset_for_job", lambda settings, job: data
    )
    monkeypatch.setattr(manager, "ensure_service", lambda: started.append(1) or {})
    first = manager.open({"id": "job-1"})
    second = manager.open({"id": "job-2"})
    assert started == [1, 1]
    assert first["url"].endswith("/bp/job-1/episode_0")
    assert second["url"].endswith("/bp/job-2/episode_0")
    status = manager.status()
    assert {p["job_id"] for p in status["previews"]} == {"job-1", "job-2"}
    assert manager.close("job-1")["state"] == "CLOSED"
    assert {p["job_id"] for p in manager.status()["previews"]} == {"job-2"}


def test_service_is_idempotent_and_reports_unavailable(settings_factory, monkeypatch):
    s = settings_factory("server")
    manager = ViewerManager(s, Store(s.root / "state"))
    monkeypatch.setattr("pipeline.server.viewer.executable_path", lambda value: None)
    manager.autostart()
    status = manager.status()
    assert status["state"] == "UNAVAILABLE"
    assert "letools" in status["error"]
    with pytest.raises(DomainError):
        manager.ensure_service()


def test_poll_marks_ready_restarts_and_prunes(settings_factory, monkeypatch):
    import httpx

    s = settings_factory("server")
    store = Store(s.root / "state")
    manager = ViewerManager(s, store)

    class Alive:
        pid = 4_000_000
        returncode = None

        def poll(self):
            return None

    manager.process = Alive()
    store.put("viewer", "service", {"state": "STARTING", "pid": 4_000_000})
    monkeypatch.setattr(
        "pipeline.server.viewer.httpx.get",
        lambda *a, **k: httpx.Response(200),
    )
    manager.poll()
    assert manager.status()["state"] == "READY"

    class Dead(Alive):
        returncode = 3

        def poll(self):
            return 3

    manager.process = Dead()
    manager.poll()
    status = manager.status()
    assert status["state"] == "FAILED" and "退出：3" in status["error"]
    assert manager.process is None

    restarted = []
    monkeypatch.setattr(manager, "autostart", lambda: restarted.append(1))
    row = store.get("viewer", "service")
    row["retry_at"] = time.time() - 1
    store.put("viewer", "service", row)
    manager.poll()
    assert restarted == [1]


def test_recover_reaps_recorded_orphan(settings_factory, monkeypatch):
    s = settings_factory("server")
    store = Store(s.root / "state")
    store.put("viewer", "service", {"state": "READY", "pid": 4_000_000})
    manager = ViewerManager(s, store)
    killed = []
    monkeypatch.setattr(manager, "_kill_group", lambda pid: killed.append(pid))
    monkeypatch.setattr(manager, "_ports_busy", lambda: False)
    manager.recover()
    assert killed == [4_000_000]
    row = store.get("viewer", "service")
    assert row["state"] == "INTERRUPTED" and row["pid"] is None


def test_kill_group_ignores_unrelated_pid(settings_factory):
    s = settings_factory("server")
    manager = ViewerManager(s, Store(s.root / "state"))
    # PID 1 belongs to init and None/garbage must never be signalled.
    manager._kill_group(1)
    manager._kill_group(None)
    manager._kill_group("1234")


def test_server_api_opens_lists_and_closes_previews(
    settings_factory, tmp_path, monkeypatch
):
    s = settings_factory("server")
    app = create_app(s, background=False)
    data = make_dataset(tmp_path)
    monkeypatch.setattr(
        "pipeline.server.viewer.dataset_for_job", lambda settings, job: data
    )
    monkeypatch.setattr(app.state.viewer, "ensure_service", lambda: {"state": "READY"})
    headers = {"Authorization": "Bearer " + s.token}
    store = app.state.ingest.store
    store.put("job", "job-9", {"id": "job-9", "state": "CONVERTED"})
    with TestClient(app) as http:
        opened = http.post("/api/jobs/job-9/viewer", headers=headers)
        assert opened.status_code == 200
        assert opened.json()["url"].endswith("/bp/job-9/episode_0")
        status = http.get("/api/viewer", headers=headers).json()
        assert [p["job_id"] for p in status["previews"]] == ["job-9"]
        closed = http.delete("/api/jobs/job-9/viewer", headers=headers)
        assert closed.json()["state"] == "CLOSED"
        assert http.get("/api/viewer", headers=headers).json()["previews"] == []
        assert (
            http.delete("/api/jobs/missing/viewer", headers=headers).status_code == 404
        )


def test_viewer_patch_is_version_checked_and_idempotent(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / ".letools-source.json").write_text(
        json.dumps({"upstream_commit": UPSTREAM})
    )
    config = tmp_path / "source/next.config.ts"
    config.write_text("const nextConfig: NextConfig = {\n};\n")
    patch_cache(tmp_path)
    once = config.read_text()
    patch_cache(tmp_path)
    assert config.read_text() == once
    assert "env: { DATASET_URL:" in once
    (tmp_path / ".letools-source.json").write_text(
        json.dumps({"upstream_commit": "unexpected"})
    )
    with pytest.raises(RuntimeError):
        patch_cache(tmp_path)


def test_build_is_skipped_until_inputs_change(tmp_path, monkeypatch):
    from pipeline.server import viewer_bootstrap as bootstrap

    channel_patch = tmp_path / "channels.patch"
    channel_patch.write_text("layout-v1")
    monkeypatch.setattr(bootstrap, "CHANNEL_PATCH", channel_patch)
    score_filter_patch = tmp_path / "score-filter.patch"
    score_filter_patch.write_text("score-filter-v1")
    monkeypatch.setattr(bootstrap, "SCORE_FILTER_PATCH", score_filter_patch)
    episode_lengths_patch = tmp_path / "episode-lengths.patch"
    episode_lengths_patch.write_text("episode-lengths-v1")
    monkeypatch.setattr(bootstrap, "EPISODE_LENGTHS_PATCH", episode_lengths_patch)
    (tmp_path / "source/.next").mkdir(parents=True)
    (tmp_path / ".letools-source.json").write_text(json.dumps({"fingerprint": "f1"}))
    builds = []
    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda *a, **k: builds.append(a[0])
    )
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://x/_data")
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://x/_data")
    assert len(builds) == 1
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://y/_data")
    assert len(builds) == 2
    (tmp_path / ".letools-source.json").write_text(json.dumps({"fingerprint": "f2"}))
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://y/_data")
    assert len(builds) == 3
    channel_patch.write_text("layout-v2")
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://y/_data")
    assert len(builds) == 4
    assets = tmp_path / "ui"
    assets.mkdir()
    (assets / "chart.tsx").write_text("new chart presentation")
    monkeypatch.setattr(bootstrap, "UI_ASSETS", assets)
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://y/_data")
    assert len(builds) == 5
    (assets / "chart.tsx").write_text("updated chart presentation")
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://y/_data")
    assert len(builds) == 6
    score_filter_patch.write_text("score-filter-v2")
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://y/_data")
    assert len(builds) == 7
    episode_lengths_patch.write_text("episode-lengths-v2")
    bootstrap.build_if_needed(tmp_path, "bun", {}, "http://y/_data")
    assert len(builds) == 8


def test_corrupt_viewer_state_is_not_reported_as_stopped(settings_factory):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        path = settings.root / "state/viewer/service.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text("broken JSON")
        response = http.get(
            "/api/viewer", headers={"Authorization": "Bearer " + settings.token}
        )
        assert response.status_code == 500
        assert "状态文件损坏" in response.json()["detail"]
        with pytest.raises(DomainError) as invalid:
            ViewerManager(settings, app.state.ingest.store).recover()
        assert invalid.value.code == 500


def test_gateway_requires_the_server_token(settings_factory, tmp_path):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    registry = PreviewRegistry(store, ttl=300)
    registry.register("job-a", make_dataset(tmp_path))
    store.put("viewer", "service", {"state": "READY"})
    with TestClient(create_gateway(s)) as browser:
        # Browsers get a login form instead of a JSON error.
        page = browser.get("/bp/job-a/episode_0", headers={"Accept": "text/html"})
        assert page.status_code == 200
        assert "登录数据预览" in page.text
        assert 'id="token"' in page.text
        assert browser.get("/login").status_code == 200
        # Data, video ranges and the Doctor endpoints stay closed.
        for path in (
            "/_data/bp/job-a/resolve/main/meta/info.json",
            "/_data/doctor/?dataset=bp/job-a",
            "/_data/doctor/report.json?dataset=bp/job-a",
        ):
            assert browser.get(path).status_code == 401, path
        assert browser.get("/api/viewer").status_code == 401
        assert browser.post("/api/login", json={"token": "wrong"}).status_code == 401

        assert browser.post("/api/login", json={"token": s.token}).status_code == 200
        assert (
            browser.get("/_data/bp/job-a/resolve/main/meta/info.json").status_code
            == 200
        )
        me = browser.get("/_data/bp/job-a/resolve/main/meta/info.json")
        assert me.status_code == 200

        browser.post("/api/logout")
        assert (
            browser.get("/_data/bp/job-a/resolve/main/meta/info.json").status_code
            == 401
        )


def test_gateway_accepts_bearer_for_automation(settings_factory, tmp_path):
    s = settings_factory("viewer")
    store = Store(s.root / "state")
    registry = PreviewRegistry(store, ttl=300)
    registry.register("job-a", make_dataset(tmp_path))
    store.put("viewer", "service", {"state": "READY"})
    headers = {"Authorization": "Bearer " + s.token}
    with TestClient(create_gateway(s)) as browser:
        ok = browser.get("/_data/bp/job-a/resolve/main/meta/info.json", headers=headers)
        assert ok.status_code == 200
        assert (
            browser.get(
                "/_data/bp/job-a/resolve/main/meta/info.json",
                headers={"Authorization": "Bearer nope"},
            ).status_code
            == 401
        )
