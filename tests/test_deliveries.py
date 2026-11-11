import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.deliveries import DeliveryScanner
from pipeline.server.delivery_validation import header, inventory
from pipeline.server.delivery_validation_worker import validate
from pipeline.shared.core import DomainError, canonical, file_hash


def publish(
    root,
    *,
    batch_name="batch-001",
    delivery_id="delivery-001",
    episode_ids=None,
    dimensions=3,
):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    batch = root / "task" / batch_name
    dataset = batch / "lerobot"
    for name in (
        "meta/episodes/chunk-000",
        "data/chunk-000",
        "videos/observation.images.overview/chunk-000",
    ):
        (dataset / name).mkdir(parents=True)
    features = {
        key: {"dtype": "int64", "shape": [1], "names": None}
        for key in ("index", "episode_index", "frame_index", "task_index")
    }
    features["timestamp"] = {"dtype": "float32", "shape": [1], "names": None}
    for key in ("observation.state", "action"):
        features[key] = {
            "dtype": "float32",
            "shape": [dimensions],
            "names": [f"j{i}" for i in range(dimensions)],
        }
    features["observation.images.overview"] = {
        "dtype": "video",
        "shape": [32, 32, 3],
        "names": None,
    }
    info = {
        "codebase_version": "v3.0",
        "robot_type": root.name,
        "total_episodes": 2,
        "total_frames": 5,
        "total_tasks": 1,
        "fps": 10,
        "features": features,
        "splits": {"train": "0:2"},
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }
    (dataset / "meta/info.json").write_bytes(canonical(info))
    (dataset / "meta/platform.json").write_bytes(
        canonical(
            {
                "robot_type": root.name,
                "feature_schema_id": "joint-v1",
                "state_semantics": "measured radians",
                "action_semantics": "held joint target radians",
            }
        )
    )
    (dataset / "meta/stats.json").write_bytes(
        canonical(
            {
                key: {
                    "count": [5],
                    "min": [0.0] * dimensions,
                    "max": [4.0] * dimensions,
                    "mean": [2.0] * dimensions,
                    "std": [2**0.5] * dimensions,
                }
                for key in ("action", "observation.state")
            }
        )
    )
    pq.write_table(
        pa.table(
            {"task_index": pa.array([0], pa.int64()), "__index_level_0__": ["insert"]}
        ),
        dataset / "meta/tasks.parquet",
    )
    data = {
        "index": pa.array(list(range(5)), pa.int64()),
        "episode_index": pa.array([0, 0, 1, 1, 1], pa.int64()),
        "frame_index": pa.array([0, 1, 0, 1, 2], pa.int64()),
        "task_index": pa.array([0] * 5, pa.int64()),
        "timestamp": pa.array([0, 0.1, 0, 0.1, 0.2], pa.float32()),
    }
    for key in ("observation.state", "action"):
        data[key] = pa.array(
            [[i] * dimensions for i in range(5)], pa.list_(pa.float32(), dimensions)
        )
    pq.write_table(pa.table(data), dataset / "data/chunk-000/file-000.parquet")
    episodes = []
    for i, (start, end) in enumerate([(0, 2), (2, 5)]):
        episodes.append(
            {
                "episode_index": i,
                "tasks": ["insert"],
                "length": end - start,
                "data/chunk_index": 0,
                "data/file_index": 0,
                "dataset_from_index": start,
                "dataset_to_index": end,
                "videos/observation.images.overview/chunk_index": 0,
                "videos/observation.images.overview/file_index": 0,
                "videos/observation.images.overview/from_timestamp": start / 10,
                "videos/observation.images.overview/to_timestamp": end / 10,
            }
        )
    pq.write_table(
        pa.Table.from_pylist(episodes),
        dataset / "meta/episodes/chunk-000/file-000.parquet",
    )
    with av.open(
        str(dataset / "videos/observation.images.overview/chunk-000/file-000.mp4"), "w"
    ) as output:
        stream = output.add_stream("libx264", rate=10)
        stream.width = stream.height = 32
        stream.pix_fmt = "yuv420p"
        for i in range(5):
            frame = av.VideoFrame.from_ndarray(
                np.full((32, 32, 3), i * 30, np.uint8), format="rgb24"
            )
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    manifest = {
        "schema": "bodypipeline-lerobot-delivery/1",
        "delivery_id": delivery_id,
        "dataset_id": root.name + "-insert",
        "robot_type": root.name,
        "feature_schema_id": "joint-v1",
        "format": "lerobot",
        "codebase_version": "v3.0",
        "dataset_path": "lerobot",
        "total_episodes": 2,
        "total_frames": 5,
        "fps": 10,
        "created_at": "2026-11-11T00:00:00Z",
        "episode_ids": episode_ids or [delivery_id + "-0", delivery_id + "-1"],
        "supersedes_delivery_id": None,
    }
    seal(batch, manifest)
    return batch


def seal(batch, manifest=None):
    if manifest is None:
        manifest = json.loads((batch / "delivery.json").read_text())
    manifest["files"] = [
        {
            "path": p.relative_to(batch).as_posix(),
            "size_bytes": p.stat().st_size,
            "sha256": file_hash(p),
        }
        for p in sorted((batch / "lerobot").rglob("*"))
        if p.is_file()
    ]
    (batch / "delivery.json").write_bytes(canonical(manifest))
    (batch / "READY").write_bytes(
        canonical(
            {
                "delivery_id": manifest["delivery_id"],
                "manifest_sha256": file_hash(batch / "delivery.json"),
            }
        )
    )


def setup_scanner(settings_factory, tmp_path, monkeypatch):
    root = tmp_path / "deliveries" / "sim"
    root.mkdir(parents=True)
    settings = settings_factory(
        "server",
        import_roots=[root.parent],
        delivery_sources=[{"root": str(root), "robot_type": "sim"}],
    )
    monkeypatch.setattr(
        "pipeline.server.deliveries.validate_dataset",
        lambda batch, data, settings, **kwargs: validate(batch, data),
    )
    app = create_app(settings, background=False)
    return root, settings, app


def test_incremental_registration_restart_and_read_only(
    settings_factory, tmp_path, monkeypatch
):
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    batch = publish(root)
    with TestClient(app) as http:
        auth = {"Authorization": "Bearer " + settings.token}
        assert http.get("/api/deliveries").status_code == 401
        assert http.post("/api/deliveries/scan", headers=auth).status_code == 202
        app.state.deliveries.tick()
        status = http.get("/api/deliveries", headers=auth).json()
        assert status["counts"] == {"REGISTERED": 1}, status
        job_id = status["deliveries"][0]["job_id"]
        job = app.state.ingest.store.get("job", job_id)
        assert job["robot_type"] == "sim" and job["immutable_delivery"]
        assert "runtime_id" not in job and "provider" not in job
        assert job["dataset_name"] == "sim-insert"
        assert (
            http.post(
                "/api/imports", headers=auth, json={"path": str(batch / "lerobot")}
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/api/dataset-deletions",
                headers=auth,
                json={"kind": "result", "id": job_id, "confirmation": "sim-insert"},
            ).status_code
            == 403
        )
        app.state.viewer.registry.register(job_id, batch / "lerobot")
        with pytest.raises(DomainError, match="只读"):
            from pipeline.server.recycle import RecycleBin

            recycle = RecycleBin(
                settings, app.state.ingest.store, app.state.viewer.registry
            )
            state = recycle.change(f"bp/{job_id}", "trash", [0], 0)
            recycle.change(f"bp/{job_id}", "purge", [0], state["revision"])
        # A separate scanner over the same durable state must not revalidate.
        restarted = DeliveryScanner(
            settings, app.state.imports, app.state.datasets, app.state.runner
        )
        monkeypatch.setattr(
            "pipeline.server.deliveries.validate_dataset",
            lambda *args: pytest.fail("unchanged delivery revalidated"),
        )
        restarted.tick()
        assert len(app.state.ingest.store.all("job")) == 1
        # Modification is reported, never silently overwritten.
        (batch / "lerobot/meta/stats.json").write_text("{}")
        restarted.request()
        restarted.tick()
        assert restarted.status()["deliveries"][0]["conflict"]


def test_unready_bad_hash_retry_and_other_batches_continue(
    settings_factory, tmp_path, monkeypatch
):
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    pending = publish(root)
    ready = (pending / "READY").read_bytes()
    (pending / "READY").unlink()
    scanner = app.state.deliveries
    scanner.tick()
    assert not scanner.status()["deliveries"]
    (pending / "READY").write_bytes(ready)
    (pending / "lerobot/meta/stats.json").write_text("{}")
    publish(root, batch_name="batch-002", delivery_id="delivery-002")
    scanner.request()
    scanner.tick()
    assert scanner.status()["counts"] == {"FAILED": 1, "REGISTERED": 1}
    (pending / "lerobot/meta/stats.json").write_bytes(
        canonical(
            {
                key: {
                    "count": [5],
                    "min": [0.0] * 3,
                    "max": [4.0] * 3,
                    "mean": [2.0] * 3,
                    "std": [2**0.5] * 3,
                }
                for key in ("action", "observation.state")
            }
        )
    )
    scanner.request()
    scanner.tick()
    assert scanner.status()["counts"] == {"REGISTERED": 2}


def test_duplicate_ids_conflicts_and_schema_version(
    settings_factory, tmp_path, monkeypatch
):
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    publish(root)
    scanner = app.state.deliveries
    scanner.tick()
    publish(
        root,
        batch_name="batch-002",
        delivery_id="delivery-002",
        episode_ids=["delivery-001-0", "new"],
    )
    publish(root, batch_name="batch-003", delivery_id="delivery-003", dimensions=5)
    scanner.request()
    scanner.tick()
    errors = [
        r["error"] for r in scanner.status()["deliveries"] if r["state"] == "FAILED"
    ]
    assert (
        len(errors) == 2
        and any("episode_id" in e for e in errors)
        and any("feature_schema_id" in e for e in errors)
    )
    assert len(app.state.ingest.store.all("job")) == 1


def test_conflicting_manifest_and_missing_mount(
    settings_factory, tmp_path, monkeypatch
):
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    batch = publish(root)
    scanner = app.state.deliveries
    scanner.tick()
    manifest = json.loads((batch / "delivery.json").read_text())
    manifest["created_at"] = "2026-11-12T00:00:00Z"
    seal(batch, manifest)
    scanner.request()
    scanner.tick()
    assert len(app.state.ingest.store.all("job")) == 1
    assert any(r.get("conflict") for r in scanner.status()["deliveries"])
    root.rename(root.with_name("offline"))
    scanner.request()
    scanner.tick()
    assert scanner.status()["sources"][0]["state"] == "UNAVAILABLE"
    assert len(app.state.ingest.store.all("job")) == 1


def test_worker_rejects_bad_timestamps_and_video(
    settings_factory, tmp_path, monkeypatch
):
    pq = pytest.importorskip("pyarrow.parquet")
    pa = pytest.importorskip("pyarrow")
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    batch = publish(root)
    path = batch / "lerobot/data/chunk-000/file-000.parquet"
    table = pq.read_table(path)
    table = table.set_column(
        table.schema.get_field_index("timestamp"),
        "timestamp",
        pa.array([0, 0.1, 0, 0.1, 0.9], pa.float32()),
    )
    pq.write_table(table, path)
    seal(batch)
    app.state.deliveries.tick()
    assert "timestamp" in app.state.deliveries.status()["deliveries"][0]["error"]
    other = publish(root, batch_name="batch-002", delivery_id="delivery-002")
    (
        other / "lerobot/videos/observation.images.overview/chunk-000/file-000.mp4"
    ).write_bytes(b"not a video")
    seal(other)
    app.state.deliveries.request()
    app.state.deliveries.tick()
    assert app.state.deliveries.status()["counts"] == {"FAILED": 2}


def test_configuration_and_manifest_safety(settings_factory, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="import_roots"):
        settings_factory(
            "server",
            delivery_sources=[{"root": str(tmp_path / "sim"), "robot_type": "sim"}],
        )
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    batch = publish(root)
    source = settings.delivery_sources[0]
    data = header(batch, source, settings)
    extra = batch / "lerobot/extra.txt"
    extra.write_text("not in manifest")
    with pytest.raises(DomainError, match="清单不一致"):
        inventory(batch, data, settings)
    extra.unlink()
    path = batch / "lerobot/meta/stats.json"
    original = path.read_bytes()
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(original)
    path.symlink_to(outside)
    with pytest.raises(DomainError, match="符号链接"):
        inventory(batch, data, settings)


def test_durable_claim_recovers_crash_between_registration_and_ledger(
    settings_factory, tmp_path, monkeypatch
):
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    publish(root)
    scanner = app.state.deliveries
    scanner.tick()
    row = app.state.ingest.store.all("delivery")[0]
    row.update(state="VALIDATING", next_retry_at=0)
    row.pop("job_id")
    app.state.ingest.store.put("delivery", row["id"], row)
    scanner.request()
    scanner.tick()
    assert scanner.status()["counts"] == {"REGISTERED": 1}
    assert len(app.state.ingest.store.all("job")) == 1


def test_independent_robot_dimensions_and_approved_schema_upgrade(
    settings_factory, tmp_path, monkeypatch
):
    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    publish(root)
    batch = publish(
        root, batch_name="batch-002", delivery_id="delivery-002", dimensions=8
    )
    platform_path = batch / "lerobot/meta/platform.json"
    platform = json.loads(platform_path.read_text())
    platform["feature_schema_id"] = "joint-v2"
    platform_path.write_bytes(canonical(platform))
    data = json.loads((batch / "delivery.json").read_text())
    data["feature_schema_id"] = "joint-v2"
    seal(batch, data)
    app.state.deliveries.tick()
    assert app.state.deliveries.status()["counts"] == {"REGISTERED": 2}
    other = tmp_path / "deliveries/another_robot"
    other.mkdir()
    settings.delivery_sources.append(
        {"root": str(other), "robot_type": "another_robot"}
    )
    publish(other, delivery_id="another-delivery", dimensions=11)
    app.state.deliveries.request()
    app.state.deliveries.tick()
    assert app.state.deliveries.status()["counts"] == {"REGISTERED": 3}


def test_real_subprocess_validator_and_cancel(settings_factory, tmp_path, monkeypatch):
    import sys

    from pipeline.server.delivery_validation import validate_dataset

    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    batch = publish(root)
    # Use this test environment as the external PyArrow/PyAV toolchain.
    monkeypatch.setattr(
        "pipeline.server.delivery_validation.executable_path",
        lambda _: str(Path(sys.executable).with_name("letools")),
    )
    data = header(batch, settings.delivery_sources[0], settings)
    assert validate_dataset(batch, data, settings)["frames"] == 5
    app.state.deliveries.stopping.set()
    with pytest.raises(DomainError, match="取消交付文件校验"):
        inventory(batch, data, settings, stopping=app.state.deliveries.stopping)
    with pytest.raises(DomainError, match="取消交付校验"):
        validate_dataset(batch, data, settings, stopping=app.state.deliveries.stopping)


def test_read_only_survives_removing_scan_source_and_alias(
    settings_factory, tmp_path, monkeypatch
):
    from pipeline.server.datasets import require_mutable_dataset

    root, settings, app = setup_scanner(settings_factory, tmp_path, monkeypatch)
    batch = publish(root)
    app.state.deliveries.tick()
    settings.delivery_sources.clear()
    with pytest.raises(DomainError, match="只读"):
        require_mutable_dataset(
            settings, batch / "lerobot", store=app.state.ingest.store
        )
    with pytest.raises(DomainError, match="只读"):
        require_mutable_dataset(settings, root.parent, store=app.state.ingest.store)
