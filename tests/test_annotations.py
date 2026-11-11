"""Tests for the automatic annotation queue, Argus adaptation and deletion guard."""

import io
import json
import time
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.server import argus_annotation
from pipeline.server.annotations import (
    AnnotationRunner,
    BudgetExceeded,
    _failure_error,
)
from pipeline.server.app import create_app
from pipeline.shared.core import DomainError, Store


def make_dataset(root, episodes=2, name="Test Dataset"):
    data = root / "dataset"
    (data / "meta").mkdir(parents=True)
    (data / "meta/info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v3.0",
                "fps": 30,
                "total_episodes": episodes,
                "total_frames": 100,
            }
        )
    )
    return data


def make_argus(root):
    src = root / "argus"
    for module in ("review", "label", "board", "checks", "prepare"):
        (src / module).mkdir(parents=True, exist_ok=True)
        (src / module / "__init__.py").write_text("")
    venv = src / ".venv/bin"
    venv.mkdir(parents=True)
    python = venv / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    return src


def make_job(store, data, *, provider="agilex", robot_type="agilex_aloha"):
    profile = {
        "cameras": [
            {"id": "left", "mounting": "wrist_left"},
            {"id": "front", "mounting": "scene"},
            {"id": "right", "mounting": "wrist_right"},
        ],
        "state_streams": ["puppetLeft", "puppetRight"],
        "joint_names": ["j"] * 14,
        "joint_units": ["rad"] * 14,
        "output_fps": 30,
        "control_mode": "teleoperation",
        "robot_type": robot_type,
    }
    store.put(
        "job",
        "job-1",
        {
            "id": "job-1",
            "state": "CONVERTED",
            "dataset_ready": True,
            "dataset_path": str(data),
            "dataset_name": "Test Dataset",
            "robot_type": robot_type,
            "provider": provider,
            "profile": profile,
            "target_format": "v3.0",
        },
    )
    return profile


@pytest.fixture
def service(settings_factory, tmp_path, monkeypatch):
    src = make_argus(tmp_path)
    imports = tmp_path / "imports"
    imports.mkdir()
    monkeypatch.setenv("PIPELINE_ARGUS_API_KEY", "test-server-key")
    s = settings_factory(
        "server",
        argus_src=str(src),
        import_roots=[imports],
        argus_task_budget_usd=10.0,
        argus_daily_budget_usd=20.0,
        argus_per_episode_budget_usd=1.0,
        argus_annotation_concurrency=4,
    )
    store = Store(s.root / "state")
    data = make_dataset(imports)
    make_job(store, data)
    return AnnotationRunner(s, store), store, src, data


def test_options_and_candidates_come_from_registered_datasets(service):
    runner, store, _src, data = service
    options = runner.options()
    assert options["available"] is True
    assert options["models"]
    assert options["defaults"]["concurrency"] == 4
    candidates = runner.candidates()
    assert [row["job_id"] for row in candidates] == ["job-1"]
    assert candidates[0]["episode_count"] == 2
    assert candidates[0]["dataset_path"] == str(data)
    assert str(data) == store.get("job", "job-1")["dataset_path"]


def test_options_expose_the_server_default_base_url(service):
    runner, _store, _src, _data = service
    assert runner.options()["default_base_url"] == ""
    runner.settings.argus_base_url = "https://aiproxy.example/v1"
    assert runner.options()["default_base_url"] == "https://aiproxy.example/v1"


def test_annotation_context_uses_profile_rig_and_rejects_typos(service):
    runner, store, _src, _data = service
    job = store.get("job", "job-1")
    job["profile"]["annotation_rig"] = "ego_head"
    store.put("job", "job-1", job)
    row = runner.enqueue("job-1")
    assert row["rig"] == "ego_head"
    assert row["provider_context"]["rig"] == "ego_head"

    store.put("annotation", row["id"], dict(row, state="COMPLETED"))
    job["profile"]["annotation_rig"] = "handheld"
    store.put("job", "job-1", job)
    with pytest.raises(DomainError):
        runner.enqueue("job-1")


def test_enqueue_validates_model_budget_and_is_idempotent(service):
    runner, store, _src, _data = service
    row = runner.enqueue("job-1", model="openai/gpt-6-astra")
    assert row["state"] == "QUEUED"
    assert row["rig"] == "teleop_arms"
    assert row["cap_usd"] == 2.0  # two episodes at 1 USD each
    assert row["provider_context"]["cameras"]["left"] == "wrist_left"
    assert runner.enqueue("job-1", model="openai/gpt-6-astra")["id"] == row["id"]

    with pytest.raises(DomainError):
        runner.enqueue("job-1", model="unknown/model")
    with pytest.raises(DomainError):
        runner.enqueue("job-1", concurrency=0)
    with pytest.raises(DomainError):
        runner.enqueue("unknown-job")


def test_options_never_default_to_a_model_outside_the_allowlist(service):
    runner, _store, _src, _data = service
    runner.settings.argus_models = ["openai/gpt-6.1-sol"]
    options = runner.options()
    assert options["default_model"] == "openai/gpt-6.1-sol"
    # The queue applies the same fallback when the request omits a model, but an
    # explicitly disallowed model is still rejected.
    assert runner.enqueue("job-1")["model"] == "openai/gpt-6.1-sol"
    with pytest.raises(DomainError):
        runner.enqueue("job-1", model="openai/gpt-6-astra")
    runner.settings.argus_models = []
    runner.settings.argus_model = "any/model"
    assert runner.options()["default_model"] == "any/model"


def test_enqueue_requires_credentials_and_keeps_the_key_off_the_record(
    service, monkeypatch
):
    runner, _store, _src, _data = service
    monkeypatch.delenv("PIPELINE_ARGUS_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(DomainError, match="API Key"):
        runner.enqueue("job-1")

    row = runner.enqueue(
        "job-1", base_url="https://gateway.example.com/v1/", api_key="task-secret"
    )
    assert row["base_url"] == "https://gateway.example.com/v1"
    assert "task-secret" not in json.dumps(row)
    assert runner.credentials[row["id"]]["api_key"] == "task-secret"
    assert runner.credentials[row["id"]]["base_url"] == "https://gateway.example.com/v1"
    runner.cancel(row["id"])
    assert row["id"] not in runner.credentials


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gateway.example/v1",
        "gateway.example/v1",
        "https://user:pass@gateway.example/v1",
        "https://gateway.example/v1?key=1",
        "https://gateway.example/v1#fragment",
        "https://gateway.example/v 1",
    ],
)
def test_enqueue_rejects_unsafe_base_urls(service, url):
    runner, _store, _src, _data = service
    with pytest.raises(DomainError):
        runner.enqueue("job-1", base_url=url, api_key="task-secret")


def test_environment_prefers_per_task_credentials(service):
    runner, _store, _src, _data = service
    credentials = argus_annotation.resolve_credentials(
        runner.settings, "https://gateway.example/v1", "task-secret"
    )
    env = argus_annotation.environment(
        runner.settings,
        {"concurrency": 2, "model": "gpt-6.1-sol"},
        {},
        credentials,
    )
    assert env["PIPELINE_ARGUS_BASE_URL"] == "https://gateway.example/v1"
    assert env["PIPELINE_ARGUS_GENERIC_GATEWAY"] == "1"
    assert env["PIPELINE_ARGUS_API_KEY"] == "task-secret"
    # The gateway need not serve Argus's OpenRouter-only routing model.
    assert env["PIPELINE_ARGUS_ROUTE_MODEL"] == "gpt-6.1-sol"

    # Server defaults still work when the task does not name a key.
    env = argus_annotation.environment(
        runner.settings, {"concurrency": 2, "model": "gpt-6.1-sol"}, {}
    )
    assert env["PIPELINE_ARGUS_API_KEY"] == "test-server-key"
    assert "PIPELINE_ARGUS_BASE_URL" not in env
    assert "PIPELINE_ARGUS_ROUTE_MODEL" not in env


def test_environment_routes_with_the_task_model_on_the_server_gateway(service):
    runner, _store, _src, _data = service
    runner.settings.argus_base_url = "https://gateway.example/v1"
    env = argus_annotation.environment(runner.settings, {"model": "gpt-6.1-sol"}, {})
    assert env["PIPELINE_ARGUS_BASE_URL"] == "https://gateway.example/v1"
    assert env["PIPELINE_ARGUS_GENERIC_GATEWAY"] == "1"
    assert env["PIPELINE_ARGUS_ROUTE_MODEL"] == "gpt-6.1-sol"


def test_task_key_is_redacted_from_logs_and_cleared_after_cancel(service):
    runner, _store, _src, _data = service
    row = runner.enqueue("job-1", api_key="task-secret")
    work = runner.work(row)
    (work / "logs").mkdir(parents=True)
    (work / "logs/run.log").write_text("request failed with task-secret\n")
    assert "task-secret" not in runner.log(row["id"], "run")
    assert "[redacted]" in runner.log(row["id"], "run")
    runner.cancel(row["id"])
    assert row["id"] not in runner.credentials


def test_resume_asks_for_the_key_again_after_memory_is_cleared(service, monkeypatch):
    runner, store, _src, _data = service
    row = runner.enqueue("job-1")
    store.put("annotation", row["id"], dict(row, state="FAILED", error="boom"))
    runner.credentials.pop(row["id"], None)
    monkeypatch.delenv("PIPELINE_ARGUS_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(DomainError, match="API Key"):
        runner.resume(row["id"])

    resumed = runner.resume(
        row["id"], base_url="https://gateway.example/v1", api_key="resume-secret"
    )
    assert resumed["state"] == "QUEUED"
    assert resumed["base_url"] == "https://gateway.example/v1"
    assert runner.credentials[row["id"]]["api_key"] == "resume-secret"


def test_recover_marks_queued_interrupted(service):
    runner, store, _src, _data = service
    store.put(
        "annotation",
        "q1",
        {
            "id": "q1",
            "job_id": "job-1",
            "state": "QUEUED",
            "created_at": time.time(),
            "dataset_path": "/x",
        },
    )
    runner.recover()
    recovered = store.get("annotation", "q1")
    assert recovered["state"] == "INTERRUPTED"
    assert "续跑" in recovered["error"]


def test_native_argus_keys_still_work_without_a_task_key(service, monkeypatch):
    runner, _store, _src, _data = service
    monkeypatch.delenv("PIPELINE_ARGUS_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEYS", "sk-or-native")
    row = runner.enqueue("job-1")
    assert runner.credentials[row["id"]]["api_key"] == ""
    env = argus_annotation.environment(
        runner.settings, row, {}, runner.credentials[row["id"]]
    )
    assert env["OPENROUTER_API_KEYS"] == "sk-or-native"
    assert "PIPELINE_ARGUS_API_KEY" not in env
    assert runner.options()["api_key_configured"] is True


def test_custom_gateway_can_use_a_model_outside_the_server_allowlist(service):
    runner, _store, _src, _data = service
    runner.settings.argus_models = ["openai/gpt-6-astra"]
    with pytest.raises(DomainError, match="允许列表"):
        runner.enqueue("job-1", model="acme/vision")
    # The allowlist only governs the server's default upstream; a task gateway
    # still needs a rate the spend cap can bound.
    runner.settings.argus_prices = {"acme/vision": [[1e-6, 1e-7, 5e-6]]}
    row = runner.enqueue(
        "job-1",
        model="acme/vision",
        base_url="https://gateway.example/v1",
        api_key="task-secret",
    )
    assert row["model"] == "acme/vision"
    assert row["base_url"] == "https://gateway.example/v1"


def test_gateway_model_list_uses_task_credentials(service, monkeypatch):
    runner, _store, _src, _data = service
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return json.dumps(
                {
                    "data": [
                        {"id": "acme/vision"},
                        {"id": "acme/fast"},
                        {"id": "acme/vision"},
                    ]
                }
            ).encode()

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["auth"] = request.headers.get("Authorization")
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(argus_annotation.urllib.request, "urlopen", fake_urlopen)
    result = runner.models(
        base_url="https://gateway.example/v1/", api_key="task-secret"
    )
    assert result == {
        "models": ["acme/vision", "acme/fast"],
        "base_url": "https://gateway.example/v1",
    }
    assert seen["url"] == "https://gateway.example/v1/models"
    assert seen["auth"] == "Bearer task-secret"


def test_gateway_model_list_errors_never_echo_the_key(service, monkeypatch):
    runner, _store, _src, _data = service

    def fake_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":"incorrect key task-secret"}'),
        )

    monkeypatch.setattr(argus_annotation.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(DomainError) as info:
        runner.models(base_url="https://gateway.example/v1", api_key="task-secret")
    assert info.value.code == 502
    assert "401" in str(info.value)
    assert "task-secret" not in str(info.value)
    assert "[redacted]" in str(info.value)


def test_gateway_model_list_needs_a_base_url(service):
    runner, _store, _src, _data = service
    with pytest.raises(DomainError, match="baseURL"):
        runner.models(api_key="task-secret")


def test_declared_prices_reach_argus_and_gate_the_allowlist(service):
    """A gateway model is usable only once the cap can bound it.

    Argus prices the worst case of a call from ``OPENAI_PRICES``, which the
    pinned checkout only fills with three OpenAI models.  Anything else is
    refused before dispatch, so the server must pass its declared rates and
    reject an unpriced model at submit time instead.
    """

    runner, _store, _src, _data = service
    runner.settings.argus_models = ["gpt-6-luna", "gpt-6.1-terra"]

    with pytest.raises(DomainError, match="没有已知单价"):
        runner.enqueue("job-1", model="gpt-6-luna")
    options = runner.options()
    assert "gpt-6-luna" not in options["priced_models"]
    assert "gpt-6.1-sol" in options["priced_models"]

    runner.settings.argus_prices = {
        "gpt-6-luna": [[1e-7, 1e-8, 5e-7], [2e-7, 2e-8, 7.5e-7]]
    }
    row = runner.enqueue("job-1", model="gpt-6-luna")
    assert row["model"] == "gpt-6-luna"
    assert "gpt-6-luna" in runner.options()["priced_models"]

    env = argus_annotation.environment(runner.settings, row, {})
    declared = json.loads(env["PIPELINE_ARGUS_PRICES"])
    assert declared["gpt-6-luna"] == [[1e-7, 1e-8, 5e-7], [2e-7, 2e-8, 7.5e-7]]
    # A model without a rate keeps being refused, never silently priced.
    with pytest.raises(DomainError, match="gpt-6.1-terra"):
        runner.enqueue("job-1", model="gpt-6.1-terra")


def test_prices_are_optional_and_absent_from_the_environment_when_unset(service):
    runner, _store, _src, _data = service
    env = argus_annotation.environment(runner.settings, {"model": "gpt-6.1-sol"}, {})
    assert "PIPELINE_ARGUS_PRICES" not in env
    assert runner.options()["prices"]["gpt-6.1-sol"]


def test_default_model_prefers_one_the_cap_can_price(service):
    """A configured default Argus cannot price would fail after prepare."""

    runner, _store, _src, _data = service
    runner.settings.argus_models = ["gpt-6-luna", "gpt-6.1-sol"]
    runner.settings.argus_model = "gpt-6-luna"
    assert runner.options()["default_model"] == "gpt-6.1-sol"

    runner.settings.argus_prices = {"gpt-6-luna": [[1e-7, 1e-8, 5e-7]]}
    assert runner.options()["default_model"] == "gpt-6-luna"
    # Nothing priced: keep the configured default rather than inventing one.
    runner.settings.argus_prices = {}
    runner.settings.argus_models = ["gpt-6-luna"]
    assert runner.options()["default_model"] == "gpt-6-luna"


@pytest.mark.parametrize(
    "prices",
    [
        {"gpt-6-luna": [[1e-7, 1e-8]]},
        {"gpt-6-luna": [[1e-7, 1e-8, 5e-7], [2e-7, -1, 7.5e-7]]},
        {"gpt-6-luna": [[1e-7, "x", 5e-7]]},
        {"gpt-6-luna": [[1e-7, True, 5e-7]]},
        {"gpt-6-luna": [[1e-7, 1e-8, 5e-7], [2e-7, 2e-8, 7.5e-7], [3e-7, 3e-8, 9e-7]]},
        {"": [[1e-7, 1e-8, 5e-7]]},
        {"gpt-6-luna": {"input": 1e-7, "output": 5e-7}},
        {"gpt-6-luna": [[1e-7, 1e-8, 5e-7]], "openai/gpt-6-luna": [[1e-7, 1e-8, 5e-7]]},
    ],
)
def test_malformed_price_tables_fail_at_config_load(settings_factory, prices):
    with pytest.raises(ValueError, match="argus_prices"):
        settings_factory("server", argus_prices=prices)


def test_config_accepts_named_and_provider_qualified_prices(settings_factory):
    settings = settings_factory(
        "server",
        argus_prices={
            "openai/gpt-6-luna": {
                "input": 1e-7,
                "cached_input": 1e-8,
                "output": 5e-7,
            },
            "gpt-6.1-terra": [[1e-6, 5e-8, 5e-6]],
        },
    )
    assert settings.argus_prices["gpt-6-luna"] == [
        [1e-7, 1e-8, 5e-7],
        [1e-7, 1e-8, 5e-7],
    ]
    assert settings.argus_prices["gpt-6.1-terra"] == [
        [1e-6, 5e-8, 5e-6],
        [1e-6, 5e-8, 5e-6],
    ]


def test_budget_is_shared_with_analyses(service):
    runner, store, _src, _data = service
    store.put(
        "analysis",
        "old",
        {
            "id": "old",
            "job_id": "other",
            "state": "COMPLETED",
            "cost_usd": 20.0,
            "created_at": time.time(),
            "finished_at": time.time(),
        },
    )
    budget = runner.budget("job-1", 2)
    assert budget["daily_used_usd"] == 20.0
    assert budget["available_usd"] == 0.0
    with pytest.raises(BudgetExceeded):
        runner.enqueue("job-1")


def test_cancel_queued_and_running(service):
    runner, store, _src, _data = service
    row = runner.enqueue("job-1")
    assert runner.cancel(row["id"])["state"] == "CANCELLED"
    again = runner.enqueue("job-1")
    store.put("annotation", again["id"], dict(again, state="RUNNING"))
    pending = runner.cancel(again["id"])
    assert pending["state"] == "RUNNING" and pending["cancel_requested"] is True


def test_recover_marks_running_interrupted(service):
    runner, store, _src, _data = service
    store.put(
        "annotation",
        "a1",
        {
            "id": "a1",
            "job_id": "job-1",
            "state": "RUNNING",
            "created_at": time.time(),
            "dataset_path": "/x",
        },
    )
    runner.recover()
    assert store.get("annotation", "a1")["state"] == "INTERRUPTED"


def test_resume_restarts_preparation_only_before_any_paid_label(service):
    runner, store, _src, _data = service
    cancelled = runner.enqueue("job-1")
    store.put(
        "annotation",
        cancelled["id"],
        dict(
            cancelled,
            state="CANCELLED",
            stage="准备 Episode",
            attempts=1,
            finished_at=time.time(),
        ),
    )
    assert runner.resume(cancelled["id"])["fresh_start"] is True
    store.put(
        "annotation",
        cancelled["id"],
        dict(store.get("annotation", cancelled["id"]), state="COMPLETED"),
    )

    labelled = runner.enqueue("job-1")
    store.put(
        "annotation",
        labelled["id"],
        dict(
            labelled,
            state="CANCELLED",
            stage="大模型标注",
            episodes_done=1,
            cost_usd=0.4,
            finished_at=time.time(),
        ),
    )
    assert runner.resume(labelled["id"]).get("fresh_start") is None


def test_resume_requires_unchanged_source_and_can_replace_preparation(service):
    runner, store, _src, data = service
    row = runner.enqueue("job-1")
    store.put(
        "annotation",
        row["id"],
        dict(
            row,
            state="FAILED",
            error="preparation cannot be verified",
            attempts=1,
            finished_at=time.time(),
        ),
    )
    resumed = runner.resume(row["id"])
    assert resumed["state"] == "QUEUED"
    assert resumed["fresh_start"] is True

    (data / "meta/info.json").write_text(json.dumps({"total_episodes": 3}))
    store.put("annotation", row["id"], dict(resumed, state="FAILED"))
    with pytest.raises(DomainError):
        runner.resume(row["id"])


def test_command_maps_every_annotation_parameter_to_argus(service):
    runner, _store, _src, data = service
    row = runner.enqueue(
        "job-1",
        model="openai/gpt-6.1-sol",
        cap_usd=1.5,
        concurrency=3,
        reasoning="high",
        sample_fps=2.5,
        inspection="all",
        field_dictionary=True,
        max_tokens=4096,
        note="fold shirts",
    )
    argv = argus_annotation.command(runner.settings, row, data, Path("/tmp/work"))
    assert argv[1:] == [
        "-m",
        "review",
        "--data",
        str(data),
        "--rig",
        "teleop_arms",
        "--out",
        "/tmp/work/argus",
        "--dataset",
        "Test Dataset",
        "--cap",
        "1.5000",
        "--concurrency",
        "3",
        "--dataset-note",
        "fold shirts",
        "--model",
        "openai/gpt-6.1-sol",
        "--reasoning",
        "high",
        "--sample-fps",
        "2.5000",
        "--inspection",
        "all",
        "--field-dictionary",
        "on",
        "--max-tokens",
        "4096",
    ]


def test_unlimited_annotation_bypasses_all_monetary_limits_and_keeps_costs(service):
    runner, store, _src, data = service
    runner.settings.argus_annotation_unlimited = True
    runner.settings.argus_models = ["gateway/unpriced"]
    runner.settings.argus_model = "gateway/unpriced"
    store.put(
        "analysis",
        "spent",
        {
            "id": "spent",
            "cost_usd": 1000,
            "created_at": time.time(),
        },
    )
    budget = runner.budget("job-1", 2)
    assert budget["unlimited"] is True
    assert budget["daily_used_usd"] == 1000
    assert budget["available_usd"] is None
    assert budget["task_cap_usd"] is None
    assert budget["daily_cap_usd"] is None
    assert runner.options()["unlimited"] is True
    # Ignore stale clients' explicit small caps under deployment-wide no-cap mode.
    row = runner.enqueue("job-1", cap_usd=0.01)
    assert row["cap_usd"] is None
    argv = argus_annotation.command(runner.settings, row, data, Path("/tmp/work"))
    assert "--no-cap" in argv and "--cap" not in argv
    json.dumps(row, allow_nan=False)
    store.put("annotation", row["id"], dict(row, state="PARTIAL", cost_usd=500))
    resumed = runner.resume(row["id"])
    assert resumed["cap_usd"] is None
    assert resumed["cost_usd"] == 500
    assert resumed["budget"]["daily_used_usd"] == 1500


def test_outputs_episode_and_cost_are_read_from_argus_sidecars(tmp_path):
    work = tmp_path / "annotation"
    argus = work / "argus"
    (argus / "run/out").mkdir(parents=True)
    (argus / "qa").mkdir()
    (argus / "run/run.json").write_text(
        json.dumps(
            {
                "status": "done",
                "cost_usd": 0.42,
                "reserved_cost_usd": 0.5,
                "cost_complete": True,
                "cap_usd": 0.5,
            }
        )
    )
    (argus / "qa/episode_000000.json").write_text(
        json.dumps(
            {
                "episode_prompt": "pick and place",
                "duration_s": 4.2,
                "event_labels": [{"start_s": 0}],
                "completion": {"task_completed": "success"},
                "_meta": {
                    "model": "openai/gpt-6-astra",
                    "run_episode": "episode_000000",
                },
            }
        )
    )
    (argus / "run/out/episode_000001.json").write_text(
        json.dumps(
            {
                "episode_dir": str(work / "episodes/episode_000001"),
                "episode_prompt": "fold",
                "parse_ok": True,
                "labels": {
                    "completion": {"task_completed": False, "failure_kind": "grasp"}
                },
                "usage": {"est_cost_usd": 0.18},
            }
        )
    )
    (argus / "run/out/failed_episode_000002.json").write_text(
        json.dumps(
            {
                "episode_dir": str(work / "episodes/episode_000002"),
                "parse_ok": False,
                "no_reply": "output limit",
                "usage": {"est_cost_usd": 0.03},
            }
        )
    )
    rows = argus_annotation.outputs(work)
    assert [row["id"] for row in rows] == [
        "episode_000000",
        "episode_000001",
        "episode_000002",
    ]
    assert rows[0]["source"] == "board"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["completed"] is True
    assert rows[1]["completed"] is False
    # A boolean from an older adapter still maps to the word list.
    assert rows[1]["outcome"] == "failure"
    assert rows[1]["failure_kind"] == "grasp"
    assert rows[2]["labels_available"] is False
    assert "output limit" in rows[2]["label_failure"]["why"]
    assert (
        argus_annotation.episode(work, "episode_000001")["labels"]["completion"][
            "failure_kind"
        ]
        == "grasp"
    )
    cost = argus_annotation.cost(work)
    assert cost["cost_usd"] == 0.42
    assert cost["reserved_cost_usd"] == 0.5


def test_clips_are_discovered_and_confined_to_the_run(tmp_path):
    work = tmp_path / "annotation"
    clips = work / "argus/clips"
    for folder in ("wrist_left", "wrist_right", "depth_exo"):
        (clips / folder).mkdir(parents=True)
    (clips / "episode_000000.mp4").write_bytes(b"head")
    (clips / "wrist_left/episode_000000.mp4").write_bytes(b"left")
    (clips / "wrist_right/episode_000000.mp4").write_bytes(b"right")
    (clips / "depth_exo/episode_000000.mp4").write_bytes(b"depth")
    (clips / "wrist_left/episode_000001.mp4").write_bytes(b"other")
    (clips / "failed.json").write_text("{}")

    rows = argus_annotation.clips(work, "episode_000000")
    assert [(row["camera"], row["label"]) for row in rows] == [
        ("exo", "主相机"),
        ("left", "左腕"),
        ("right", "右腕"),
        ("depth_exo", "主相机 深度"),
    ]
    assert argus_annotation.clip(work, "episode_000000", "left").read_bytes() == b"left"
    # A client names a camera, never a path, and only a discovered clip is served.
    assert argus_annotation.clip(work, "episode_000000", "../clips") is None
    assert argus_annotation.clip(work, "episode_000000", "wrist_left") is None
    assert argus_annotation.clip(work, "episode_000000", "missing") is None
    assert argus_annotation.clips(work, "../episode_000000") == []
    assert argus_annotation.clips(work, "episode_000009") == []


def test_clip_endpoint_lists_and_streams_byte_ranges(service):
    runner, store, _src, _data = service
    row = runner.enqueue("job-1")
    clips = runner.work(row) / "argus/clips"
    (clips / "wrist_left").mkdir(parents=True)
    payload = bytes(range(256)) * 16
    (clips / "episode_000000.mp4").write_bytes(payload)
    (clips / "wrist_left/episode_000000.mp4").write_bytes(b"left")
    store.put("annotation", row["id"], dict(row, state="COMPLETED"))

    with TestClient(create_app(runner.settings, background=False)) as http:
        headers = {"Authorization": "Bearer " + runner.settings.token}
        listing = http.get(
            f"/api/annotations/{row['id']}/episodes/episode_000000/clips",
            headers=headers,
        )
        assert listing.status_code == 200, listing.text
        found = listing.json()["clips"]
        assert [(item["camera"], item["size"]) for item in found] == [
            ("exo", len(payload)),
            ("left", len(b"left")),
        ]
        assert found[0]["url"].endswith(
            f"/api/annotations/{row['id']}/episodes/episode_000000/clips/exo"
        )

        video = http.get(found[0]["url"], headers={**headers, "Range": "bytes=4-11"})
        assert video.status_code == 206
        assert video.content == payload[4:12]
        assert video.headers["content-range"] == f"bytes 4-11/{len(payload)}"
        assert video.headers["accept-ranges"] == "bytes"

        beyond = http.get(
            found[0]["url"], headers={**headers, "Range": "bytes=999999-"}
        )
        assert beyond.status_code == 416
        missing = http.get(
            f"/api/annotations/{row['id']}/episodes/episode_000000/clips/wrist_left",
            headers=headers,
        )
        assert missing.status_code == 404
        assert http.get(found[0]["url"]).status_code == 401


def test_failure_error_names_why_an_episode_was_not_labelled(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("== label\nreview finished with label failures\n")
    episodes = [
        {
            "id": "episode_000000",
            "episode_id": "episode_000000",
            "labels_available": False,
            "label_failure": {
                "status": "failed",
                "why": "ValueError: gateway rejected the key",
            },
        }
    ]
    message = _failure_error(log, episodes)
    assert "review finished" in message
    assert "episode_000000: ValueError: gateway rejected the key" in message
    assert _failure_error(log, []) == "== label\nreview finished with label failures"


def test_cost_falls_back_to_episode_usage(tmp_path):
    work = tmp_path / "annotation"
    out = work / "argus/run/out"
    out.mkdir(parents=True)
    (out / "episode_000000.json").write_text(
        json.dumps({"parse_ok": True, "usage": {"est_cost_usd": 0.1}})
    )
    (out / "episode_000001.json").write_text(
        json.dumps({"parse_ok": True, "usage": {"est_cost_usd": 0.2}})
    )
    cost = argus_annotation.cost(work)
    assert cost["cost_usd"] == 0.3
    assert cost["cost_complete"] is True


def test_stage_from_log_tracks_review_stages():
    text = "== convert\nworking\n== checks_jumps\nworking\n== label\n"
    assert argus_annotation.stage_from_log(text) == "大模型标注"
    assert argus_annotation.stage_from_log("") == ""
    assert "文件不存在" not in argus_annotation.stage_from_log("== board")


def test_run_completes_with_outputs(service, monkeypatch):
    runner, store, _src, _data = service
    fake = runner.settings.root / "fake-argus-python"
    fake.write_text(
        """#!/bin/sh
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--out" ]; then out="$arg"; fi
  prev="$arg"
done
mkdir -p "$out/run/out" "$out/qa" "$out/logs"
printf '%s' '{"status":"done","cost_usd":0.25,"reserved_cost_usd":0.3,"cost_complete":true,"cap_usd":2}' > "$out/run/run.json"
printf '%s' '{"episode_dir":"episode_000000","episode_prompt":"pick","parse_ok":true,"labels":{"completion":{"task_completed":true}},"event_labels":[{}],"usage":{"est_cost_usd":0.25}}' > "$out/run/out/episode_000000.json"
echo "gateway key=$PIPELINE_ARGUS_API_KEY"
exit 0
"""
    )
    fake.chmod(0o755)
    runner.settings.argus_python = str(fake)
    row = runner.enqueue("job-1")
    finished = runner.run(row)
    assert finished["state"] == "COMPLETED"
    assert finished["episodes_done"] == 1
    assert finished["cost_usd"] == 0.25
    assert (runner.work(finished) / "logs/run.log").is_file()
    assert "test-server-key" not in (runner.work(finished) / "logs/run.log").read_text()
    context = json.loads((runner.work(finished) / "context.json").read_text())
    assert "test-server-key" not in json.dumps(context)
    assert runner.episodes(finished["id"])[0]["prompt"] == "pick"
    assert runner.export(finished["id"])["argus"]["cost_usd"] == 0.25

    # Re-preparing archives the old output. Its settled cost must survive in the
    # task and daily ledgers even though its sidecars are no longer read.
    store.put(
        "annotation",
        finished["id"],
        dict(
            finished,
            state="QUEUED",
            fresh_start=True,
            error="preparation cannot be verified",
        ),
    )
    again = runner.run(store.get("annotation", finished["id"]))
    assert (runner.work(again) / "argus-attempt-1").is_dir()
    assert again["carried_cost_usd"] == 0.25
    assert again["cost_usd"] == 0.5
    assert again["episodes_done"] == 1


def test_api_options_candidates_enqueue_and_delete_guard(service):
    runner, store, _src, _data = service
    settings = runner.settings
    app = create_app(settings, background=False)
    app.state.annotations = runner
    headers = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        options = http.get("/api/annotations/options", headers=headers)
        assert options.status_code == 200 and options.json()["available"] is True
        candidates = http.get("/api/annotations/candidates", headers=headers)
        assert candidates.status_code == 200
        assert candidates.json()["datasets"][0]["job_id"] == "job-1"

        created = http.post(
            "/api/annotations",
            headers=headers,
            json={"job_id": "job-1", "model": "openai/gpt-6-astra"},
        )
        assert created.status_code == 202, created.text
        aid = created.json()["id"]
        assert http.get("/api/annotations", headers=headers).json()[0]["id"] == aid
        assert http.get(f"/api/annotations/{aid}", headers=headers).status_code == 200
        assert (
            http.get(f"/api/annotations/{aid}/budget", headers=headers).json()[
                "task_cap_usd"
            ]
            == 2.0
        )
        assert http.get(f"/api/annotations/{aid}/episodes", headers=headers).json() == {
            "episodes": []
        }
        assert (
            http.get(f"/api/annotations/{aid}/logs", headers=headers).json()["logs"][0]
            == "run"
        )
        cancelled = http.post(f"/api/annotations/{aid}/cancel", headers=headers)
        assert cancelled.json()["state"] == "CANCELLED"

        # An active annotation must block permanent deletion of its source.
        store.put(
            "annotation",
            "running",
            {
                "id": "running",
                "job_id": "job-1",
                "state": "RUNNING",
                "dataset_path": str(store.get("job", "job-1")["dataset_path"]),
                "created_at": time.time(),
            },
        )
        blocked = http.post(
            "/api/dataset-deletions",
            headers=headers,
            json={
                "kind": "result",
                "id": "job-1",
                "confirmation": "Test Dataset",
            },
        )
        assert blocked.status_code == 409
        assert "自动标注" in blocked.json()["detail"]

        bad = http.post(
            "/api/annotations",
            headers=headers,
            json={"job_id": "job-1", "model": "unknown/model"},
        )
        assert bad.status_code == 400


def test_api_accepts_task_credentials_without_persisting_them(service):
    _fixture_runner, _store, _src, _data = service
    settings = _fixture_runner.settings
    app = create_app(settings, background=False)
    runner = app.state.annotations
    headers = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        created = http.post(
            "/api/annotations",
            headers=headers,
            json={
                "job_id": "job-1",
                "model": "openai/gpt-6-astra",
                "base_url": "https://gateway.example/v1",
                "api_key": "api-secret",
            },
        )
        assert created.status_code == 202, created.text
        row = created.json()
        aid = row["id"]
        assert "api-secret" not in created.text
        assert row["base_url"] == "https://gateway.example/v1"
        assert runner.credentials[aid]["api_key"] == "api-secret"

        detail = http.get(f"/api/annotations/{aid}", headers=headers)
        assert "api-secret" not in detail.text
        export = http.get(f"/api/annotations/{aid}/export", headers=headers)
        assert export.status_code == 200
        assert "api-secret" not in export.text

        assert (
            http.post(f"/api/annotations/{aid}/cancel", headers=headers).json()["state"]
            == "CANCELLED"
        )
        assert aid not in runner.credentials

        resumed = http.post(
            f"/api/annotations/{aid}/resume",
            headers=headers,
            json={"api_key": "resume-secret"},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["state"] == "QUEUED"
        assert "resume-secret" not in resumed.text
        assert runner.credentials[aid]["api_key"] == "resume-secret"
        assert (
            http.post(f"/api/annotations/{aid}/cancel", headers=headers).json()["state"]
            == "CANCELLED"
        )

        # Older callers post no body; server defaults still have to work.
        legacy = http.post(f"/api/annotations/{aid}/resume", headers=headers)
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["state"] == "QUEUED"


def test_api_model_list_returns_gateway_catalogue(service, monkeypatch):
    _runner, _store, _src, _data = service
    settings = _runner.settings
    app = create_app(settings, background=False)
    headers = {"Authorization": "Bearer " + settings.token}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return json.dumps({"models": [{"name": "acme/vision"}]}).encode()

    monkeypatch.setattr(
        argus_annotation.urllib.request,
        "urlopen",
        lambda request, timeout=0: FakeResponse(),
    )
    with TestClient(app) as http:
        response = http.post(
            "/api/annotations/models",
            headers=headers,
            json={
                "base_url": "https://gateway.example/v1",
                "api_key": "api-secret",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "models": ["acme/vision"],
            "base_url": "https://gateway.example/v1",
        }
        assert "api-secret" not in response.text


def test_unavailable_toolchain_disables_enqueue(settings_factory, tmp_path):
    s = settings_factory("server", argus_src=str(tmp_path / "missing"))
    store = Store(s.root / "state")
    data = make_dataset(s.root)
    make_job(store, data)
    runner = AnnotationRunner(s, store)
    assert runner.available()[0] is False
    assert runner.options()["available"] is False
    with pytest.raises(DomainError):
        runner.enqueue("job-1")


def test_annotation_storage_area(settings_factory, tmp_path):
    from pipeline.server.storage import AREAS, storage_path

    assert "annotations" in AREAS
    s = settings_factory("server", root=tmp_path / "state", data_root=tmp_path / "data")
    (tmp_path / "data").mkdir()
    row = {"data_layout": "robot_type_v1", "robot_type": "agilex_aloha"}
    assert (
        storage_path(s, row, "annotations", "abc")
        == (tmp_path / "data/agilex_aloha/annotations/abc").resolve()
    )
