"""Template AI contracts, retries, scope checks and real dataset round trips."""

import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_annotations import make_argus, make_dataset, make_job

from pipeline.server import argus_annotation
from pipeline.server.app import create_app
from pipeline.server.argus_template_contract import (
    task_contract,
    template_identity,
    validation_problems,
)
from pipeline.server.subtask_export import build_plan
from pipeline.server.subtask_review import DraftRequest, SubtaskReview
from pipeline.server.task_annotations import (
    TemplateLibrary,
    TemplateRequest,
    document_segments,
)
from pipeline.server.template_annotation import (
    SPEC_NAME,
    assemble_results,
    invalid_received_units,
    make_document,
    retry_manifest,
)
from pipeline.shared.core import atomic_json, digest


@pytest.fixture
def template():
    return {
        "id": "cup",
        "name": "搬杯子",
        "subtasks": [{"id": "grasp", "name": "抓取"}, {"id": "place", "name": "放置"}],
        "fields": [
            {
                "id": "quality",
                "label": "整体质量",
                "scope": "episode",
                "kind": "rating",
            },
            {"id": "score", "label": "执行质量", "scope": "attempt", "kind": "rating"},
            {
                "id": "outcome",
                "label": "执行结果",
                "scope": "attempt",
                "kind": "single",
                "options": ["成功", "失败"],
            },
            {"id": "note", "label": "异常说明", "scope": "frame", "kind": "textarea"},
        ],
    }


def spec_for(template):
    return {"version": 1, "template": template, "signature": digest(template)}


def reply_for(template, duration=1):
    return {
        "parse_ok": True,
        "labels": {
            "bodypipeline_task": {
                "template_id": template["id"],
                "template_signature": digest(template),
                "values": {"quality": 4},
                "attempts": [
                    {
                        "subtask_id": "grasp",
                        "start_s": 0,
                        "end_s": duration / 2,
                        "values": {"score": 2, "outcome": "失败"},
                    },
                    {
                        "subtask_id": "grasp",
                        "start_s": duration / 2,
                        "end_s": duration * 0.8,
                        "values": {"score": 5, "outcome": "成功"},
                    },
                ],
                "null_segments": [
                    {
                        "start_s": duration * 0.8,
                        "end_s": duration * 0.9,
                        "note": "确认背景",
                    }
                ],
                "frames": [{"t_s": 0, "values": {"note": "抓取失败"}}],
            }
        },
    }


def context(frames=10, **updates):
    return {
        "episode_id": "episode_000000",
        "episode_index": 0,
        "fps": 10,
        "n_state_frames": frames,
        **updates,
    }


def test_prompt_is_bound_to_snapshot_and_inactive_without_template(
    template, tmp_path, monkeypatch
):
    monkeypatch.delenv("PIPELINE_ARGUS_TASK_TEMPLATE", raising=False)
    assert task_contract({}) == ""
    assert template_identity() is None
    spec = spec_for(template)
    path = tmp_path / SPEC_NAME
    atomic_json(path, spec)
    monkeypatch.setenv("PIPELINE_ARGUS_TASK_TEMPLATE", str(path))
    contract = task_contract(context())
    assert template_identity() == digest(template)
    assert (
        "bodypipeline_task" in contract
        and '"id": "grasp"' in contract
        and '"scope": "frame"' in contract
    )
    spec["template"]["name"] = "changed"
    atomic_json(path, spec)
    with pytest.raises(ValueError, match="snapshot"):
        template_identity()


def test_patch_updates_prompt_and_request_identity_idempotently(tmp_path, monkeypatch):
    patch_path = Path("patches/argus/0002-task-template.py").resolve()
    spec = importlib.util.spec_from_file_location("template_patch", patch_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    label = tmp_path / "label"
    label.mkdir()
    (label / "episode.py").write_text(
        "def build_prompt():\n    return fixed, episode_text(ep, pl, cell_w, cell_h, native, blocks) + jaws.prompt(ep.get('jaw_evidence') or {})\n"
    )
    (label / "harness.py").write_text(
        'def input_identity():\n    return {"version": 2, "source_proof": source_proof(ctx, ep_dir), "settings": settings}\n\ndef annotation_problems(record):\n    if reply_truncated(record):\n        return ["truncated"]\n'
    )
    module.apply(tmp_path)
    before = {p.name: p.read_bytes() for p in label.glob("*.py")}
    module.apply(tmp_path)
    assert before == {p.name: p.read_bytes() for p in label.glob("*.py")}
    for p in label.glob("*.py"):
        ast.parse(p.read_text())
    assert "task_contract(ctx)" in (label / "episode.py").read_text()
    assert (
        "settings['bodypipeline_task_template']" in (label / "harness.py").read_text()
    )

    function = next(
        n
        for n in ast.parse((label / "harness.py").read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == "annotation_problems"
    )
    helper = types.ModuleType("label.pipeline_template")
    helper.validation_problems = lambda record: record.get("problems", [])
    monkeypatch.setitem(sys.modules, "label.pipeline_template", helper)
    namespace = {"reply_truncated": lambda _: False}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), "patched-argus", "exec"),
        namespace,
    )
    assert namespace["annotation_problems"]({"problems": ["template invalid"]}) == [
        "template invalid"
    ]
    assert namespace["annotation_problems"]({}) is None


def test_closed_template_preserves_attempts_fields_null_and_unlabelled_gap(template):
    ctx = context()
    document, warnings = make_document(
        spec_for(template), ctx, [(ctx, reply_for(template))]
    )
    assert warnings == []
    assert document["templateSnapshot"] == template
    assert document["values"] == {"quality": 4}
    assert [
        (a["subtaskId"], a["start"], a["end"], a["values"]["score"])
        for a in document["attempts"]
    ] == [("grasp", 0, 5, 2), ("grasp", 5, 8, 5)]
    assert len({a["id"] for a in document["attempts"]}) == 2
    assert document["nullSegments"][0]["start"] == 8
    assert document["nullSegments"][0]["end"] == 9  # frame 9 stays unlabelled
    assert document["frames"][0]["values"] == {"note": "抓取失败"}


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(template_id="wrong"),
        lambda r: r.update(template_signature="wrong"),
        lambda r: r["attempts"][0].update(subtask_id="invented"),
        lambda r: r["attempts"][0]["values"].update(score=6),
        lambda r: r["attempts"][0]["values"].update(score=True),
        lambda r: r["attempts"][0]["values"].update(outcome="maybe"),
        lambda r: r["attempts"][0]["values"].update(quality=3),
        lambda r: r["attempts"][0]["values"].update(invented=None),
        lambda r: r["attempts"][0].update(start_s=True),
        lambda r: r["attempts"][0].update(end_s=2),
        lambda r: r["attempts"][0].update(end_s=0.7),
        lambda r: r["attempts"][0].update(continues_after=True),
        lambda r: r["frames"][0].update(t_s=1),
    ],
)
def test_invalid_model_output_is_rejected_instead_of_growing_template(template, change):
    raw = reply_for(template)
    change(raw["labels"]["bodypipeline_task"])
    with pytest.raises(ValueError):
        make_document(spec_for(template), context(), [(context(), raw)])


def test_long_recording_owns_overlap_once_and_only_joins_explicit_continuations(
    template,
):
    parent = context(16)
    left = context(10, piece={"t0_s": 0, "t1_s": 1, "of": parent["episode_id"]})
    right = context(8, piece={"t0_s": 0.8, "t1_s": 1.6, "of": parent["episode_id"]})
    a, b = reply_for(template), reply_for(template, 0.8)
    for raw, length in ((a, 1), (b, 0.8)):
        r = raw["labels"]["bodypipeline_task"]
        r["attempts"] = [
            {
                "subtask_id": "grasp",
                "start_s": 0,
                "end_s": length,
                "values": {"score": 4},
            }
        ]
        r["null_segments"], r["frames"] = [], []
    a["labels"]["bodypipeline_task"]["attempts"][0]["continues_after"] = True
    b["labels"]["bodypipeline_task"]["attempts"][0]["continues_before"] = True
    doc, warnings = make_document(spec_for(template), parent, [(left, a), (right, b)])
    assert [(s["start"], s["end"]) for s in doc["attempts"]] == [(0, 16)]
    assert doc["values"] == {} and warnings  # no whole-recording judgement from a part
    b["labels"]["bodypipeline_task"]["attempts"][0]["continues_before"] = False
    doc, _ = make_document(spec_for(template), parent, [(left, a), (right, b)])
    assert [(s["start"], s["end"]) for s in doc["attempts"]] == [(0, 9), (9, 16)]
    with pytest.raises(ValueError, match="不完整"):
        make_document(spec_for(template), parent, [(left, a)])


def seed_outputs(work, template, counts):
    atomic_json(work / SPEC_NAME, spec_for(template))
    for index, count in enumerate(counts):
        name = f"episode_{index:06d}"
        ctx = context(count, episode_id=name, episode_index=index)
        atomic_json(work / "argus/episodes" / name / "context.json", ctx)
        atomic_json(
            work / "argus/run/out" / f"{name}.json", reply_for(template, count / 10)
        )


def test_adapter_and_export_refuse_free_text_fallback(template, tmp_path):
    seed_outputs(tmp_path, template, [10])
    raw = tmp_path / "argus/run/out/episode_000000.json"
    raw.write_text(
        json.dumps(
            {
                "parse_ok": True,
                "labels": {
                    "timeline": [{"start_s": 0, "end_s": 1, "action": "free text"}]
                },
            }
        )
    )
    result = assemble_results(
        tmp_path, {"episode_000000": {"event_labels": [{"verb_class": "free text"}]}}
    )
    assert result["episode_000000"]["_label_failed"]["status"] == "template_invalid"
    assert argus_annotation.outputs(tmp_path)[0]["labels_available"] is False


def test_resume_only_retries_received_units_with_invalid_template_contract(
    template, tmp_path, monkeypatch
):
    seed_outputs(tmp_path, template, [10, 10])
    assert invalid_received_units(tmp_path) == []
    path = tmp_path / "argus/run/out/episode_000001.json"
    data = json.loads(path.read_text())
    data["labels"]["bodypipeline_task"]["attempts"][0]["values"]["score"] = 9
    atomic_json(path, data)
    assert invalid_received_units(tmp_path) == ["episode_000001"]
    atomic_json(tmp_path / "task-template-invalid.json", retry_manifest(tmp_path))
    monkeypatch.setenv("PIPELINE_ARGUS_TASK_TEMPLATE", str(tmp_path / SPEC_NAME))
    assert validation_problems(data)
    assert validation_problems(reply_for(template)) == []


def test_task_runner_executes_frozen_template_with_offline_subprocess(
    template, settings_factory, tmp_path, monkeypatch
):
    src = make_argus(tmp_path)
    for name in ("episode.py", "harness.py", "pipeline_template.py"):
        (src / "label" / name).write_text(
            "# pipeline-argus-task-template:v1\n# pipeline-argus-template-retry:v1\n"
        )
    (src / "review/__main__.py").write_text(
        Path("tests/fixtures/mock_argus_review.py").read_text()
    )
    monkeypatch.setenv("PIPELINE_ARGUS_API_KEY", "offline-dummy")
    imports = tmp_path / "imports"
    imports.mkdir()
    settings = settings_factory(
        "server",
        argus_src=str(src),
        argus_python=sys.executable,
        import_roots=[imports],
    )
    app = create_app(settings, background=False)
    store = app.state.ingest.store
    make_job(store, make_dataset(imports))
    TemplateLibrary(store).save(TemplateRequest(revision=0, templates=[template]))
    row = app.state.annotations.enqueue("job-1", template_id="cup", template_revision=1)
    result = app.state.annotations.run(row)
    assert result["state"] == "COMPLETED", result.get("error")
    assert result["episodes_done"] == 2 and result["cost_usd"] == 0
    episode = app.state.annotations.episode(row["id"], "episode_000000")
    assert episode["task_document"]["templateId"] == "cup"
    assert len(episode["task_document"]["attempts"]) == 3
    work = Path(result["work_path"])
    assert json.loads((work / SPEC_NAME).read_text())["template"] == template
    assert "offline-dummy" not in (work / "context.json").read_text()


def test_enqueue_freezes_shared_template_and_rejects_stale_selection(
    template, settings_factory, tmp_path, monkeypatch
):
    src = make_argus(tmp_path)
    for name in ("episode.py", "harness.py", "pipeline_template.py"):
        (src / "label" / name).write_text(
            "# pipeline-argus-task-template:v1\n# pipeline-argus-template-retry:v1\n"
        )
    monkeypatch.setenv("PIPELINE_ARGUS_API_KEY", "mock-key")
    imports = tmp_path / "imports"
    imports.mkdir()
    settings = settings_factory("server", argus_src=str(src), import_roots=[imports])
    app = create_app(settings, background=False)
    store = app.state.ingest.store
    data = make_dataset(imports)
    make_job(store, data)
    library = TemplateLibrary(store)
    library.save(TemplateRequest(revision=0, templates=[template]))
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + settings.token}
    response = client.post(
        "/api/annotations",
        headers=headers,
        json={"job_id": "job-1", "template_id": "cup", "template_revision": 0},
    )
    assert response.status_code == 409
    response = client.post(
        "/api/annotations",
        headers=headers,
        json={"job_id": "job-1", "template_id": "cup", "template_revision": 1},
    )
    assert response.status_code == 202, response.text
    row = response.json()
    template["subtasks"][0]["name"] = "renamed"
    library.save(TemplateRequest(revision=1, templates=[template]))
    assert (
        store.get("annotation", row["id"])["task_template"]["subtasks"][0]["name"]
        == "抓取"
    )
    assert (
        client.post(
            "/api/annotations",
            headers=headers,
            json={"job_id": "job-1", "template_id": "cup", "template_revision": 2},
        ).status_code
        == 409
    )
    app.state.annotations.cancel(row["id"])
    (src / "label/episode.py").write_text("")
    assert (
        client.post(
            "/api/annotations",
            headers=headers,
            json={"job_id": "job-1", "template_id": "cup", "template_revision": 2},
        ).status_code
        == 503
    )


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_ai_template_export_review_and_revision_roundtrip(
    template, settings_factory, tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    fixtures = tmp_path / "fixtures"
    subprocess.run(
        [
            python,
            str(Path("tests/fixtures/merge_scenario.py").resolve()),
            "create",
            str(fixtures),
            version,
        ],
        check=True,
        capture_output=True,
    )
    source = fixtures / "input-0"
    before = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    }
    settings = settings_factory(
        "server", letools_bin=letools_binary, import_roots=[fixtures]
    )
    app = create_app(settings, background=False)
    runner = app.state.runner
    runner.store.put(
        "job",
        "source",
        {
            "id": "source",
            "state": "CONVERTED",
            "dataset_path": str(source),
            "dataset_name": "mock",
            "robot_type": "test_arm",
        },
    )
    work = tmp_path / "annotation"
    seed_outputs(work, template, [6, 5])
    row = {
        "id": "ai",
        "job_id": "source",
        "state": "COMPLETED",
        "dataset_path": str(source),
        "work_path": str(work),
        "task_template": template,
        "template_signature": digest(template),
    }
    runner.store.put("annotation", "ai", row)
    plan = build_plan(settings, row)
    assert len(plan["episodes"]) == 2 and not plan["skipped"]
    assert plan["episodes"][0]["task_document"]["values"] == {"quality": 4}
    export = runner.subtask_exports.enqueue("ai")
    runner.tick()
    published = runner.store.get("job", export["id"])
    assert published["state"] == "CONVERTED", published.get("error")
    path = Path(published["dataset_path"])
    app.state.viewer.registry.register(export["id"], path)
    review = SubtaskReview(settings, runner.store)
    loaded = review.episode(export["id"], 0)
    doc = loaded["task_document"]
    assert doc == plan["episodes"][0]["task_document"]
    assert (
        len(doc["attempts"]) == 2
        and doc["attempts"][0]["subtaskId"] == doc["attempts"][1]["subtaskId"]
    )
    frozen_digest = plan["digest"]
    doc["attempts"][0]["values"]["score"] = 3
    review.save(
        export["id"],
        0,
        DraftRequest(
            revision=loaded["revision"],
            source_signature=loaded["source_signature"],
            segments=document_segments(doc),
            task_document=doc,
        ),
    )
    assert (
        review.episode(export["id"], 0)["task_document"]["attempts"][0]["values"][
            "score"
        ]
        == 3
    )
    assert build_plan(settings, row)["digest"] == frozen_digest
    assert {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    } == before
    subprocess.run(
        [letools_binary, "validate", str(path), "--deep"],
        check=True,
        capture_output=True,
    )
