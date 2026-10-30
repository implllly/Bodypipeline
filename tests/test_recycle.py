import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.server.recycle import RecycleBin, record_cleanup
from pipeline.server.viewer import create_gateway
from pipeline.server.viewer_data import PreviewRegistry
from pipeline.shared.core import DomainError, Store


def setup_bin(settings_factory, tmp_path):
    settings = settings_factory("viewer", import_roots=[tmp_path / "datasets"])
    dataset = tmp_path / "datasets/example"
    (dataset / "meta").mkdir(parents=True)
    (dataset / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "total_episodes": 3,
                "total_frames": 18,
                "fps": 6,
            }
        )
    )
    store = Store(settings.root / "state")
    registry = PreviewRegistry(store, 300)
    registry.register("example", dataset)
    registry.register("alias", dataset)
    return settings, store, registry, RecycleBin(settings, store, registry)


def test_dataset_scoped_trash_persists_and_restores(settings_factory, tmp_path):
    settings, store, registry, recycle = setup_bin(settings_factory, tmp_path)
    result = recycle.change("bp/example", "trash", [1], 0)
    assert [e["episode"] for e in result["entries"]] == [1]
    restarted = RecycleBin(settings, Store(store.path), registry)
    assert restarted.status("bp/alias")["entries"] == result["entries"]
    assert (tmp_path / "datasets/example/meta/info.json").is_file()
    assert restarted.change("bp/alias", "restore", [1], 1)["entries"] == []
    with pytest.raises(DomainError, match="列表已更新"):
        restarted.change("bp/example", "trash", [2], 0)
    with pytest.raises(DomainError, match="只能操作已在回收站"):
        restarted.change("bp/example", "purge", [0], 2)
    with pytest.raises(DomainError, match="不存在"):
        restarted.change("bp/example", "trash", [99], 2)
    assert restarted.status("bp/example")["entries"] == []


def test_recycle_api_authorization_validation_and_page(settings_factory, tmp_path):
    settings, store, registry, recycle = setup_bin(settings_factory, tmp_path)
    with TestClient(create_gateway(settings)) as client:
        assert client.get("/api/recycle/example").status_code == 401
        client.post("/api/login", json={"token": settings.token})
        url = "/api/recycle/example/trash"
        assert (
            client.post(url, json={"episodes": [1], "revision": 0}).status_code == 403
        )
        headers = {"Origin": "http://testserver"}
        assert (
            client.post(
                url, headers=headers, json={"episodes": [True], "revision": 0}
            ).status_code
            == 422
        )
        assert (
            client.post(
                url, headers=headers, json={"episodes": [], "revision": 0}
            ).status_code
            == 422
        )
        response = client.post(
            url, headers=headers, json={"episodes": [1, 1], "revision": 0}
        )
        assert response.status_code == 200 and len(response.json()["entries"]) == 1
        assert client.get("/api/recycle/alias").json()["entries"][0]["episode"] == 1
        store.put("viewer", "service", {"state": "READY"})
        assert (
            client.get("/bp/example/episode_1", follow_redirects=False).headers[
                "location"
            ]
            == "/bp/example/trash"
        )
        page = client.get("/bp/example/trash")
        assert page.status_code == 200 and "永久清理所选" in page.text
        assert "data-theme-toggle" in page.text and "themeScript" not in page.text
        assert (
            client.post(
                "/api/recycle/example/invalid",
                headers=headers,
                json={"episodes": [1], "revision": 1},
            ).status_code
            == 404
        )
        assert client.get("/api/recycle/unknown").status_code == 404


def test_recycle_rejects_outside_paths_and_symlinks(settings_factory, tmp_path):
    settings, store, registry, recycle = setup_bin(settings_factory, tmp_path)
    registry.register("outside", tmp_path)
    with pytest.raises(DomainError, match="允许的数据目录"):
        recycle.status("bp/outside")
    link = tmp_path / "datasets/linked"
    link.symlink_to(tmp_path / "datasets/example", target_is_directory=True)
    registry.register("linked", link)
    with pytest.raises(DomainError):
        recycle.status("bp/linked")


def test_real_shared_shard_cleanup(letools_binary):
    python = Path(letools_binary).parent / "python"
    result = subprocess.run(
        [str(python), "tests/fixtures/recycle_scenario.py"],
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert "crash recovery passed" in result.stdout


def test_cleanup_history_is_ordered_bounded_and_idempotent():
    row = {}
    for index in range(35):
        row["operation"] = {
            "id": str(index),
            "state": "completed",
            "mapping": {"1": 0},
        }
        assert record_cleanup(row)
        assert not record_cleanup(row)
    assert row["cleanup_generation"] == 35
    assert len(row["cleanup_history"]) == 32
    assert row["cleanup_history"][0]["from"] == 3
    assert row["cleanup_history"][-1] == {
        "id": "34",
        "from": 34,
        "to": 35,
        "mapping": {"1": 0},
    }
    row["operation"] = {"id": "failed", "state": "failed"}
    assert not record_cleanup(row)
    assert row["cleanup_generation"] == 35


def test_cleanup_status_migrates_legacy_completed_operation(settings_factory, tmp_path):
    _, store, _, recycle = setup_bin(settings_factory, tmp_path)
    key, path = recycle.resolve("bp/example")
    row = recycle._load(key, path)
    row["operation"] = {
        "id": "legacy",
        "state": "completed",
        "mapping": {"1": 0, "2": 1},
    }
    store.put("recycle", key, row)
    result = recycle.status("bp/example")
    assert result["cleanup_generation"] == 1
    assert result["cleanup_history"] == [
        {
            "id": "legacy",
            "from": 0,
            "to": 1,
            "mapping": {"1": 0, "2": 1},
        }
    ]
    assert recycle.status("bp/alias")["cleanup_history"] == result["cleanup_history"]
    assert store.get("recycle", key)["cleanup_generation"] == 1


def test_cleanup_recovery_records_each_mapping_once(settings_factory, tmp_path):
    from pipeline.server.recycle_worker import MARKER, recover_operation

    _, store, _, recycle = setup_bin(settings_factory, tmp_path)
    key, path = recycle.resolve("bp/example")
    row = recycle._load(key, path)
    for operation in ("first", "second"):
        row["operation"] = {
            "id": operation,
            "state": "running",
            "entries_before": {},
        }
        (path / MARKER).write_text(
            json.dumps(
                {
                    "operation": operation,
                    "mapping": {"1": 0, "2": 1},
                    "total_episodes": 2,
                    "deleted_frames": 6,
                }
            )
        )
        recover_operation(store, row)
        recover_operation(store, row)
    assert row["cleanup_generation"] == 2
    assert [item["id"] for item in row["cleanup_history"]] == ["first", "second"]


def test_legacy_replaced_dataset_without_completed_operation_is_not_original(
    settings_factory, tmp_path
):
    _, _, _, recycle = setup_bin(settings_factory, tmp_path)
    _, path = recycle.resolve("bp/example")
    info_path = path / "meta/info.json"
    info = json.loads(info_path.read_text())
    info["bodypipeline_revision"] = "old-cleanup"
    info_path.write_text(json.dumps(info))
    result = recycle.status("bp/example")
    assert result["cleanup_generation"] == 1
    assert result["cleanup_history"] == []
