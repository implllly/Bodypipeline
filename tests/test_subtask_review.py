"""Viewer Subtask review: persistence, conflicts, identities and real rewrites."""

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.merges import source_snapshot
from pipeline.server.recycle import dataset_lock
from pipeline.server.subtask_review import SubtaskReview
from pipeline.server.viewer import create_gateway
from pipeline.server.viewer_bootstrap import patch_subtask_review
from pipeline.server.viewer_data import PreviewRegistry

SCENARIO = Path(__file__).parent / "fixtures/merge_scenario.py"


def segment(start=0, end=2, text="grasp"):
    return {"start_frame": start, "end_frame": end, "text": text}


@pytest.fixture
def review_app(settings_factory, monkeypatch):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    store = app.state.runner.store
    path = settings.root / "source"
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "fps": 10,
                "total_episodes": 2,
                "total_frames": 11,
                "robot_type": "test_arm",
            }
        )
    )
    store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(path),
            "dataset_name": "fixture",
            "created_at": 1,
            "robot_type": "test_arm",
        },
    )
    PreviewRegistry(store, settings.viewer_ttl).register("source", path)
    monkeypatch.setattr(
        SubtaskReview,
        "_source",
        lambda *args: {
            "fps": 10,
            "episodes": [
                {
                    "episode_index": 0,
                    "frames": 6,
                    "segments": [segment(), segment(4, 6)],
                },
                {"episode_index": 1, "frames": 5, "segments": [segment(1, 3, "place")]},
            ],
        },
    )
    return app


def client_for(app):
    return TestClient(
        app, headers={"Authorization": "Bearer " + app.state.runner.settings.token}
    )


def body(payload, segments):
    return {
        "revision": payload["revision"],
        "source_signature": payload["source_signature"],
        "segments": segments,
    }


def enriched_document(initial):
    document = deepcopy(initial["task_document"])
    document["values"] = {"episode-quality": 5, "episode-comment": "人工复核"}
    document["attempts"][0]["values"] = {
        "attempt-quality": 1,
        "attempt-outcome": "失败",
    }
    document["attempts"][1]["values"] = {
        "attempt-quality": 5,
        "attempt-outcome": "成功",
    }
    document["nullSegments"] = [
        {"id": "background", "label": "NULL", "start": 2, "end": 3, "note": "等待"}
    ]
    document["frames"] = [
        {"id": "frame-note", "frame": 5, "values": {"frame-comment": "物体稳定"}}
    ]
    return document


URL = "/api/subtask-review/source/episodes/0"


def test_source_loading_and_empty_draft_authority(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    assert initial["frames"] == 6 and initial["revision"] == 0
    assert initial["segments"] == [segment(), segment(4, 6)]
    assert initial["has_draft"] is False
    assert client.post(URL, json=body(initial, [])).json()["revision"] == 1
    # A new service instance sees authoritative [], not the original labels.
    settings = review_app.state.runner.settings
    restarted = create_app(settings, background=False)
    result = client_for(restarted).get(URL).json()
    assert result["segments"] == [] and result["has_draft"] is True
    store = restarted.state.runner.store
    assert len(store.all("subtask_draft_history")) == 1


def test_ai_spans_and_complete_studio_document_share_one_server_draft(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    doc = initial["task_document"]
    assert [(a["start"], a["end"]) for a in doc["attempts"]] == [(0, 2), (4, 6)]
    assert doc["attempts"][0]["subtaskId"] == doc["attempts"][1]["subtaskId"]
    assert doc["attempts"][0]["id"] != doc["attempts"][1]["id"]
    document = enriched_document(initial)
    assert (
        client.post(
            URL, json={**body(initial, initial["segments"]), "task_document": document}
        ).status_code
        == 200
    )
    restarted = create_app(review_app.state.runner.settings, background=False)
    loaded = client_for(restarted).get(URL).json()
    assert loaded["task_document"] == document
    assert loaded["has_draft"] is True
    assert loaded["segments"] == initial["segments"]  # NULL is not a semantic Subtask.
    assert (
        client.get("/api/subtask-review/source/episodes/1").json()["task_document"][
            "templateId"
        ]
        == document["templateId"]
    )
    history = restarted.state.runner.store.all("subtask_draft_history")
    assert history[0]["task_documents"]["0"] == document


def test_old_interval_client_preserves_scores_notes_and_null(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    document = enriched_document(initial)
    assert (
        client.post(
            URL, json={**body(initial, initial["segments"]), "task_document": document}
        ).status_code
        == 200
    )
    head = client.get(URL).json()
    assert (
        client.post(
            URL, json=body(head, [segment(0, 2, "human"), segment(4, 6)])
        ).status_code
        == 200
    )
    changed = client.get(URL).json()["task_document"]
    assert changed["attempts"][0]["id"] == document["attempts"][0]["id"]
    assert changed["attempts"][0]["values"] == document["attempts"][0]["values"]
    assert changed["attempts"][1] == document["attempts"][1]
    head = client.get(URL).json()
    assert client.post(URL, json=body(head, [])).status_code == 200
    changed = client.get(URL).json()["task_document"]
    assert changed["attempts"] == []
    assert changed["frames"] == document["frames"]
    assert changed["nullSegments"] == document["nullSegments"]
    assert changed["values"] == document["values"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["attempts"][0].update(end=5),
        lambda d: d["nullSegments"][0].update(start=1),
        lambda d: d["frames"][0].update(frame=6),
        lambda d: d["frames"][0].update(id=d["attempts"][0]["id"]),
        lambda d: d["attempts"][0]["values"].update({"attempt-quality": True}),
        lambda d: d["attempts"][0]["values"].update({"attempt-quality": 6}),
        lambda d: d["values"].update({"frame-comment": "wrong scope"}),
    ],
)
def test_invalid_task_documents_never_advance_the_server_draft(review_app, mutate):
    client = client_for(review_app)
    initial = client.get(URL).json()
    document = enriched_document(initial)
    mutate(document)
    assert (
        client.post(
            URL, json={**body(initial, initial["segments"]), "task_document": document}
        ).status_code
        == 422
    )
    assert client.get(URL).json()["revision"] == 0


def test_task_document_cannot_disagree_with_exported_subtask_segments(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    assert (
        client.post(
            URL, json={**body(initial, []), "task_document": enriched_document(initial)}
        ).status_code
        == 422
    )
    document = enriched_document(initial)
    document["attempts"] = []
    assert (
        client.post(
            URL, json={**body(initial, []), "task_document": document}
        ).status_code
        == 200
    )
    assert client.get(URL).json()["segments"] == []


def test_task_templates_are_shared_durable_and_revision_checked(review_app):
    client = client_for(review_app)
    assert TestClient(review_app).get("/api/task-templates").status_code == 401
    initial = client.get("/api/task-templates").json()
    template = client.get(URL).json()["task_document"]["templateSnapshot"]
    template["name"] = "同类任务共用模板"
    payload = {"revision": initial["revision"], "templates": [template]}
    saved = client.post("/api/task-templates", json=payload)
    assert saved.status_code == 200
    assert client.post("/api/task-templates", json=payload).status_code == 409
    restarted = create_app(review_app.state.runner.settings, background=False)
    assert client_for(restarted).get("/api/task-templates").json()["templates"] == [
        template
    ]
    head = client.get(URL).json()
    assert (
        client.post(
            URL,
            json={
                **body(head, head["segments"]),
                "task_document": head["task_document"],
            },
        ).status_code
        == 200
    )
    template["subtasks"][1]["name"] = "renamed-place"
    assert (
        client.post(
            "/api/task-templates", json={"revision": 1, "templates": [template]}
        ).status_code
        == 200
    )
    other = client.get("/api/subtask-review/source/episodes/1").json()
    steps = other["task_document"]["templateSnapshot"]["subtasks"]
    assert len({s["id"] for s in steps}) == len(steps)
    assert other["segments"] == [segment(1, 3, "place")]


def test_revision_export_freezes_full_task_document(review_app, monkeypatch, tmp_path):
    runner = review_app.state.runner
    tool = tmp_path / "letools"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    tool.with_name("python").write_text("placeholder")
    monkeypatch.setattr(runner.settings, "letools_bin", str(tool))
    client = client_for(review_app)
    initial = client.get(URL).json()
    document = enriched_document(initial)
    assert (
        client.post(
            URL, json={**body(initial, initial["segments"]), "task_document": document}
        ).status_code
        == 200
    )
    head = client.get(URL).json()
    queued = client.post(
        "/api/subtask-review/source/exports",
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
        },
    )
    assert queued.status_code == 202
    plan = runner.store.get("job", queued.json()["id"])["revision_plan"]
    assert plan["episodes"][0]["task_document"] == document
    assert len(plan["episodes"][0]["segments"]) == 2
    assert all(v["subtask"] != "NULL" for v in plan["vocabulary"])
    changed = deepcopy(document)
    changed["values"]["episode-quality"] = 2
    assert (
        client.post(
            URL, json={**body(head, head["segments"]), "task_document": changed}
        ).status_code
        == 200
    )
    assert (
        runner.store.get("job", queued.json()["id"])["revision_plan"]["episodes"][0][
            "task_document"
        ]
        == document
    )


def test_two_windows_cannot_overwrite_a_draft(review_app):
    client = client_for(review_app)
    a = client.get(URL).json()
    assert client.post(URL, json=body(a, [segment(0, 1, "new")])).status_code == 200
    stale = client.post(URL, json=body(a, []))
    assert stale.status_code == 409 and "其他窗口" in stale.json()["detail"]
    assert client.get(URL).json()["segments"] == [segment(0, 1, "new")]


def test_repeated_text_edit_only_changes_selected_segment(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    edited = [segment(0, 2, "changed"), segment(4, 6)]
    assert client.post(URL, json=body(initial, edited)).status_code == 200
    assert client.get(URL).json()["segments"] == edited
    assert client.get("/api/subtask-review/source/episodes/1").json()["segments"] == [
        segment(1, 3, "place")
    ]


@pytest.mark.parametrize(
    "segments",
    [
        [segment(-1, 2)],
        [segment(2, 2)],
        [segment(2, 8)],
        [segment(0, 3), segment(2, 4)],
        [segment(text="  ")],
        [segment(text="x" * 401)],
        [{**segment(), "end_frame": 1.5}],
        [{**segment(), "start_frame": True}],
        [{**segment(), "extra": "ignored?"}],
    ],
)
def test_invalid_segments_rejected(review_app, segments):
    client = client_for(review_app)
    initial = client.get(URL).json()
    assert client.post(URL, json=body(initial, segments)).status_code == 422
    assert client.get(URL).json()["revision"] == 0


def test_dataset_and_episode_constraints(review_app, tmp_path):
    client = client_for(review_app)
    assert TestClient(review_app).get(URL).status_code == 401
    assert client.get("/api/subtask-review/unknown/episodes/0").status_code == 404
    assert client.get("/api/subtask-review/source/episodes/99").status_code == 404
    assert client.get(URL + "?local_path=/etc").json()["frames"] == 6
    store = review_app.state.runner.store
    path = tmp_path / "outside"
    (path / "meta").mkdir(parents=True)
    (path / "meta/info.json").write_text("{}")
    PreviewRegistry(store, 60).register("source", path)
    assert client.get(URL).status_code == 403


def test_dataset_lock_source_change_and_identity_change(review_app):
    runner = review_app.state.runner
    client = client_for(review_app)
    review = SubtaskReview(runner.settings, runner.store)
    key, path, _ = review.resolve("source")
    initial = client.get(URL).json()
    with dataset_lock(runner.store, key):
        assert client.post(URL, json=body(initial, [])).status_code == 409
    assert client.post(URL, json=body(initial, [])).status_code == 200
    (path / "changed").write_text("new source content")
    assert client.get(URL).status_code == 409
    assert client.post(URL, json=body(initial, [])).status_code == 409
    job = runner.store.get("job", "source")
    job["state"] = "DELETED"
    runner.store.put("job", "source", job)
    assert client.get(URL).status_code == 409


def test_gateway_forwards_only_authenticated_registered_id(review_app, monkeypatch):
    runner = review_app.state.runner
    gateway = create_gateway(runner.settings)
    real_client = httpx.AsyncClient
    calls = []

    def make_client(**kwargs):
        async def response(request):
            calls.append(request)
            assert request.headers["authorization"] == "Bearer " + runner.settings.token
            return httpx.Response(200, json={"revision": 3})

        return real_client(transport=httpx.MockTransport(response))

    monkeypatch.setattr("pipeline.server.viewer.httpx.AsyncClient", make_client)
    with TestClient(gateway) as client:
        assert client.get(URL).status_code == 401
        headers = {"Authorization": "Bearer " + runner.settings.token}
        assert client.get(URL, headers=headers).json() == {"revision": 3}
        payload = {"revision": 0, "source_signature": "f" * 64, "segments": []}
        assert client.post(URL, headers=headers, json=payload).status_code == 200
        assert len(calls) == 2
        assert json.loads(calls[1].content)["segments"] == []
        assert (
            client.get(
                "/api/subtask-review/missing/episodes/0", headers=headers
            ).status_code
            == 404
        )
        assert len(calls) == 2
        assert (
            client.post("/api/login", json={"token": runner.settings.token}).status_code
            == 200
        )
        assert client.post(URL, json=payload).status_code == 403  # Cookie needs Origin.
        assert client.get("/api/task-templates").status_code == 200
        templates = {"revision": 0, "templates": []}
        assert client.post("/api/task-templates", json=templates).status_code == 403
        assert (
            client.post(
                "/api/task-templates",
                json=templates,
                headers={"Origin": "http://testserver"},
            ).status_code
            == 200
        )
        assert calls[-1].url.path == "/api/task-templates"
        assert json.loads(calls[-1].content) == templates


def test_patch_is_repeatable_on_pinned_viewer(tmp_path, monkeypatch):
    from pipeline.server import viewer_annotations

    # Component anchors are exercised against the actual pinned source in the
    # UI build. This fixture checks migration of the previous replacement mount.
    replacements = []
    monkeypatch.setattr(
        viewer_annotations, "_replace", lambda *args: replacements.append(args)
    )
    path = tmp_path / "source/src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    path.parent.mkdir(parents=True)
    path.write_text("""import { AnnotationsPanel } from "@/components/annotations-panel";
              <div className="grounding-intro">legacy</div>
              <PlaybackBar />
              <AnnotationsTimeline duration={data.duration} />
              <AnnotationsPanel
                cameraKeys={videosInfo.map((v) => v.filename)}
              />""")
    patch_subtask_review(tmp_path)
    first = path.read_text()
    patch_subtask_review(tmp_path)
    assert path.read_text() == first
    assert "SubtaskReview" not in first
    assert "<PlaybackBar />" in first
    assert "<AnnotationsTimeline" in first and "<AnnotationsPanel" in first
    assert 'className="grounding-intro"' in first
    assert any("end_timestamp" in after for _, _, after in replacements)


def test_manual_annotations_are_saved_with_subtasks_and_survive_legacy_save(review_app):
    client = client_for(review_app)
    initial = client.get(URL).json()
    atoms = [
        {
            "role": "user",
            "style": "vqa",
            "content": "Where is the object?",
            "timestamp": 0.1,
            "camera": "observation.images.top",
            "tool_calls": None,
        },
        {
            "role": "assistant",
            "style": None,
            "content": None,
            "timestamp": 0.2,
            "camera": None,
            "tool_calls": [
                {
                    "type": "function",
                    "function": {"name": "say", "arguments": {"text": "hello"}},
                }
            ],
        },
    ]
    assert (
        client.post(
            URL, json={**body(initial, initial["segments"]), "atoms": atoms}
        ).status_code
        == 200
    )
    head = client.get(URL).json()
    assert head["atoms"] == atoms
    assert client.post(URL, json=body(head, [])).status_code == 200
    # Existing interval-only clients must not erase manual/VQA annotations.
    assert client.get(URL).json()["atoms"] == atoms
    latest = client.get(URL).json()
    assert client.post(URL, json={**body(latest, []), "atoms": []}).status_code == 200
    assert client.get(URL).json()["atoms"] == []


def test_export_revision_conflict_and_recycle_guard(review_app, monkeypatch, tmp_path):
    runner = review_app.state.runner
    tool = tmp_path / "letools"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    tool.with_name("python").write_text("placeholder")
    monkeypatch.setattr(runner.settings, "letools_bin", str(tool))
    client = client_for(review_app)
    initial = client.get(URL).json()
    request = {
        "revision": initial["revision"],
        "source_signature": initial["source_signature"],
    }
    assert client.post(URL, json=body(initial, [])).status_code == 200
    assert (
        client.post("/api/subtask-review/source/exports", json=request).status_code
        == 409
    )
    head = client.get(URL).json()
    request["revision"] = head["revision"]
    review = SubtaskReview(runner.settings, runner.store)
    key, path, _ = review.resolve("source")
    runner.store.put("recycle", key, {"entries": {"0": {}}, "revision": 1})
    assert (
        client.post("/api/subtask-review/source/exports", json=request).status_code
        == 409
    )
    runner.store.delete("recycle", key)
    queued = client.post("/api/subtask-review/source/exports", json=request)
    assert queued.status_code == 202, queued.text
    jid = queued.json()["id"]
    status = client.get(f"/api/subtask-review/source/exports/{jid}")
    assert status.json()["state"] == "QUEUED"
    assert "revision_plan" not in status.json()
    # A changed source fails before any output work or compute allocation.
    (path / "modified").write_text("changed after enqueue")
    runner.tick()
    result = runner.store.get("job", jid)
    assert result["state"] == "FAILED" and "提交后发生变化" in result["error"]
    assert not result.get("dataset_path")


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_studio_fields_survive_real_export_and_reopen(
    settings_factory, tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version], check=True
    )
    settings = settings_factory(
        "server", letools_bin=letools_binary, import_roots=[fixtures]
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    source = fixtures / "input-0"
    runner.store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(source),
            "dataset_name": "studio-fixture",
            "created_at": 1,
            "robot_type": "test_arm",
        },
    )
    PreviewRegistry(runner.store, settings.viewer_ttl).register("source", source)
    before = hashes(source)
    client = client_for(app)
    initial = client.get(URL).json()
    document = initial["task_document"]
    document["templateSnapshot"]["subtasks"] = [{"id": "grasp", "name": "grasp"}]
    # Adjacent identical labels must remain two independently scored attempts.
    document["attempts"] = [
        {
            "id": "first",
            "subtaskId": "grasp",
            "subtaskName": "grasp",
            "start": 0,
            "end": 1,
            "values": {"attempt-quality": 1},
        },
        {
            "id": "retry",
            "subtaskId": "grasp",
            "subtaskName": "grasp",
            "start": 1,
            "end": 2,
            "values": {"attempt-quality": 5},
        },
    ]
    document["values"] = {"episode-quality": 4, "episode-comment": "人工修订"}
    document["nullSegments"] = [
        {"id": "null", "label": "NULL", "start": 2, "end": 3, "note": "无意义"}
    ]
    document["frames"] = [
        {"id": "frame", "frame": 5, "values": {"frame-comment": "稳定"}}
    ]
    segments = [segment(0, 1), segment(1, 2)]
    saved = client.post(
        URL, json={**body(initial, segments), "task_document": document}
    )
    assert saved.status_code == 200, saved.text
    head = client.get(URL).json()
    queued = client.post(
        "/api/subtask-review/source/exports",
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
        },
    )
    assert queued.status_code == 202, queued.text
    runner.tick()
    result = runner.store.get("job", queued.json()["id"])
    assert result["state"] == "CONVERTED", result.get("error")
    output = Path(result["dataset_path"])
    sidecar = json.loads(
        (output / "meta/bodypipeline-task-annotations.json").read_text()
    )
    assert sidecar["episodes"]["0"] == document
    PreviewRegistry(runner.store, settings.viewer_ttl).register(result["id"], output)
    reloaded = client.get(f"/api/subtask-review/{result['id']}/episodes/0").json()
    assert reloaded["task_document"] == document
    assert reloaded["segments"] == segments
    assert result["subtask_report"]["labelled_frames"] == 2
    assert result["subtask_report"]["unannotated_frames"] == 9
    assert hashes(source) == before
    # A later AI candidate must not activate a copied sidecar for old labels.
    candidate = tmp_path / "ai-candidate"
    spec = tmp_path / "ai-spec.json"
    spec.write_text(
        json.dumps(
            {
                "source": str(output),
                "destination": str(candidate),
                "source_format": "v3.0",
                "target_format": "v3.0",
                "episodes": [
                    {"episode_index": 0, "frames": 6, "segments": [[4, 6, 0]]},
                    {"episode_index": 1, "frames": 5, "segments": []},
                ],
                "vocabulary": [{"subtask_index": 0, "subtask": "AI candidate"}],
                "unannotated_index": -1,
                "unannotated_text": "unannotated",
            }
        )
    )
    subprocess.run(
        [
            python,
            str(Path("pipeline/server/subtask_export_worker.py").resolve()),
            str(spec),
        ],
        check=True,
    )
    assert not (candidate / "meta/bodypipeline-task-annotations.json").exists()
    archived = list(
        (candidate / "meta").glob("bodypipeline-task-annotations-source-*.json")
    )
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["episodes"]["0"] == document
    assert (
        json.loads((output / "meta/bodypipeline-task-annotations.json").read_text())[
            "episodes"
        ]["0"]
        == document
    )


@pytest.mark.parametrize("indexed", [False, True])
def test_language_column_import_and_authoritative_sidecar(
    tmp_path, letools_binary, indexed
):
    """Old language-only sources and indexed AI sources use the same editor."""
    code = """
import json,sys
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
from pipeline.server.subtask_review_worker import read_subtasks
root=Path(sys.argv[1]); indexed=sys.argv[2]=='True'
(root/'meta').mkdir(); (root/'data').mkdir()
(root/'meta/info.json').write_text(json.dumps({'codebase_version':'v3.0','fps':10,'total_episodes':2,'total_frames':6}))
language=[{'role':'assistant','style':'subtask','content':'language subtask','timestamp':0.1},
          {'role':'assistant','style':'plan','content':'existing manual plan','timestamp':0.0}]
rows=[]
for ep in range(2):
 for frame in range(3):
  row={'episode_index':ep,'frame_index':frame,'timestamp':frame/10,'language_persistent':language,
       'language_events':[{'role':'user','style':'vqa','content':'question'}] if frame==1 else []}
  if indexed: row['subtask_index']=-1
  rows.append(row)
pq.write_table(pa.Table.from_pylist(rows),root/'data/shard.parquet')
loaded=read_subtasks(root)
for ep in loaded['episodes']:
 assert ep['segments']==([] if indexed else [{'start_frame':1,'end_frame':3,'text':'language subtask'}])
 assert len(ep['atoms'])==2
 assert ep['atoms'][0]['timestamp']==0
 assert ep['atoms'][1]['timestamp']==0.1
 assert ep['atoms'][0]['camera'] is None and ep['atoms'][0]['tool_calls'] is None
(root/'meta/bodypipeline-manual-annotations.json').write_text(json.dumps({'version':1,'episodes':{'0':[]}}))
loaded=read_subtasks(root)
assert loaded['episodes'][0]['atoms']==[]
assert len(loaded['episodes'][1]['atoms'])==2
"""
    subprocess.run(
        [
            str(Path(letools_binary).with_name("python")),
            "-c",
            code,
            str(tmp_path),
            str(indexed),
        ],
        check=True,
    )


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_review_roundtrip(settings_factory, tmp_path, letools_binary, version):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [python, str(SCENARIO), "create", str(fixtures), version], check=True
    )
    settings = settings_factory(
        "server", letools_bin=letools_binary, import_roots=[fixtures]
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    source = fixtures / "input-0"
    runner.store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(source),
            "dataset_name": "fixture",
            "created_at": 1,
            "robot_type": "test_arm",
        },
    )
    PreviewRegistry(runner.store, settings.viewer_ttl).register("source", source)
    before = hashes(source)
    client = client_for(app)
    initial = client.get(URL)
    assert initial.status_code == 200, initial.text
    initial = initial.json()
    assert initial["segments"] == []  # No labels in the source fixture.
    edited = [segment(0, 2), segment(4, 6)]
    manual = [
        {
            "role": "assistant",
            "style": "plan",
            "content": "manual plan",
            "timestamp": 0.1,
            "camera": None,
            "tool_calls": None,
        }
    ]
    assert (
        client.post(URL, json={**body(initial, edited), "atoms": manual}).status_code
        == 200
    )
    saved = client.get(URL).json()
    export_body = {
        "revision": saved["revision"],
        "source_signature": saved["source_signature"],
    }
    queued = client.post("/api/subtask-review/source/exports", json=export_body)
    assert queued.status_code == 202, queued.text
    export = queued.json()
    assert (
        client.post("/api/subtask-review/source/exports", json=export_body).json()["id"]
        == export["id"]
    )
    # Freeze the queued plan: a later saved empty draft does not affect it.
    assert client.post(URL, json=body(saved, [])).status_code == 200
    runner.tick()
    result = runner.store.get("job", export["id"])
    assert result["state"] == "CONVERTED", result.get("error")
    assert hashes(source) == before
    revised = Path(result["dataset_path"])
    PreviewRegistry(runner.store, settings.viewer_ttl).register(result["id"], revised)
    url = f"/api/subtask-review/{result['id']}/episodes/0"
    loaded = client.get(url).json()
    assert loaded["segments"] == edited  # Includes the deliberate two-frame gap.
    assert loaded["atoms"] == manual
    other_url = f"/api/subtask-review/{result['id']}/episodes/1"
    other = client.get(other_url).json()
    assert other["segments"] == []
    other_manual = [{**manual[0], "content": "independent Episode 1 plan"}]
    assert (
        client.post(
            other_url, json={**body(other, []), "atoms": other_manual}
        ).status_code
        == 200
    )
    # Rewrite an ALREADY labelled source: edit only one repeated-text interval.
    changed = [segment(0, 1, "human corrected"), segment(4, 6)]
    loaded = client.get(url).json()
    assert client.post(url, json=body(loaded, changed)).status_code == 200
    head = client.get(url).json()
    queued2 = client.post(
        f"/api/subtask-review/{result['id']}/exports",
        json={
            "revision": head["revision"],
            "source_signature": head["source_signature"],
        },
    ).json()
    runner.tick()
    result2 = runner.store.get("job", queued2["id"])
    assert result2["state"] == "CONVERTED", result2.get("error")
    assert result2["subtask_report"]["unannotated_frames"] == 8
    target = Path(result2["dataset_path"])
    PreviewRegistry(runner.store, settings.viewer_ttl).register(result2["id"], target)
    assert (
        client.get(f"/api/subtask-review/{result2['id']}/episodes/0").json()["segments"]
        == changed
    )
    assert (
        client.get(f"/api/subtask-review/{result2['id']}/episodes/0").json()["atoms"]
        == manual
    )
    assert (
        client.get(f"/api/subtask-review/{result2['id']}/episodes/1").json()["atoms"]
        == other_manual
    )
    assert client.get(url).json()["segments"] == changed  # Saved draft still exists.
    # Deleting every segment must export -1 for ALL frames, even on labelled input.
    assert client.post(url, json=body(head, [])).status_code == 200
    empty = client.get(url).json()
    queued3 = client.post(
        f"/api/subtask-review/{result['id']}/exports",
        json={
            "revision": empty["revision"],
            "source_signature": empty["source_signature"],
        },
    ).json()
    runner.tick()
    result3 = runner.store.get("job", queued3["id"])
    assert result3["state"] == "CONVERTED", result3.get("error")
    assert result3["subtask_report"]["unannotated_frames"] == 11
    assert result3["subtask_report"]["vocabulary"] == 0
    assert hashes(source) == before
    assert source_snapshot(revised) == result2["source_snapshot"]
