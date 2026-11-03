"""G1 raw integrity, causal alignment, schema binding and real direct conversion."""

import base64
import copy
import hashlib
import io
import json
import os
import struct
import subprocess
import zlib
from pathlib import Path

import msgpack
import numpy as np
import pytest
from PIL import Image

from pipeline.server.episode_validation import EpisodeValidationError
from pipeline.server.providers import G1SONICProvider, get_provider
from pipeline.server.providers.g1_sonic import SOURCE_DESCRIPTOR, numeric_features
from pipeline.server.providers.g1_sonic_raw import (
    ACTION_PADDING,
    HEADER,
    MAGIC,
    RAW_FORMAT,
    STATE_PADDING,
    index_recording,
    iter_raw_packets,
    recording_files,
)
from pipeline.shared.core import DomainError, digest

START = 1_000_000_000
CONFIG = {
    "hand_type": "brainco",
    "hand_num_motors": 6,
    "hand_units": "normalized",
    "model_path": "policy/sonic_v1_1/model_decoder.onnx",
}


@pytest.fixture
def g1_profile():
    profile = json.loads(Path("configs/g1-sonic11-brainco-v1.json").read_text())
    profile.update(output_fps=10, semantics_approved=True, clock_verified=True)
    profile["cameras"] = [
        {"id": "egocentric", "source_key": "ego_view", "width": 32, "height": 24},
    ]
    return profile


def _packet(source, topic, when, payload):
    return {
        "source": source,
        "topic": topic,
        "receive_monotonic_ns": when,
        "receive_time_ns": 1_700_000_000_000_000_000 + when,
        "receive_sequence": 1,
        "context": when < START,
        "frames": [topic.encode() + msgpack.packb(payload, use_bin_type=True)],
    }


def _state(index):
    return {
        "body_q": [index / 100] * 29,
        "left_hand_q": [0.2] * 6,
        "right_hand_q": [0.3] * 6,
        "last_action": [0.4 + index / 100] * 29,
        "last_left_hand_action": [0.7] * 6,
        "last_right_hand_action": [0.8] * 6,
        "token_state": [index / 100 + k for k in range(64)],
    }


def write_episode(
    directory, *, mutate=None, times=None, mapped_wall=False, combined=False
):
    directory.mkdir(parents=True)
    packets = [
        _packet("robot", "robot_config", START - 20_000_000, copy.deepcopy(CONFIG))
    ]
    times = times or [-10_000_000, 10_000_000, 90_000_000, 190_000_000, 290_000_000]
    for index, relative in enumerate(times):
        when = START + relative
        robot = _packet("robot", "g1_debug", when, _state(index))
        robot["receive_sequence"] = index + 1
        packets.append(robot)
        buf = io.BytesIO()
        Image.new("RGB", (32, 24), (index * 30, 40, 90)).save(buf, format="JPEG")
        keys = ["ego_view", "left_wrist", "right_wrist"] if combined else ["ego_view"]
        images = {key: base64.b64encode(buf.getvalue()).decode() for key in keys}
        payload = {"images": images}
        if mapped_wall:
            payload["timestamps"] = {
                key: (1_700_000_000_000_000_000 + when) / 1e9 for key in keys
            }
        else:
            payload["frame_metadata"] = {
                key: {"host_capture_monotonic_ns": when} for key in keys
            }
        packets.append(_packet("camera", "", when, payload))
    packets.sort(key=lambda p: p["receive_monotonic_ns"])
    if mutate:
        mutate(packets)
    raw = bytearray(MAGIC)
    for packet in packets:
        data = msgpack.packb(packet, use_bin_type=True)
        raw.extend(HEADER.pack(len(data), zlib.crc32(data)))
        raw.extend(data)
    path = directory / "packets.raw"
    path.write_bytes(raw)
    episode = {
        "state": "saved",
        "file": "packets.raw",
        "episode_index": 7,
        "start_monotonic_ns": START,
        "end_monotonic_ns": START + 400_000_000,
        "pause_intervals": [],
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "packet_count": sum(not p["context"] for p in packets),
        "context_count": sum(p["context"] for p in packets),
    }
    (directory / "episode.json").write_text(json.dumps(episode))
    return path, episode


def load(path, profile, episode=None):
    _, metadata, config = recording_files(path)
    return index_recording(path, episode or metadata, profile, config)


def mutate_robot(packets, field, value):
    for p in packets:
        if p["topic"] == "g1_debug":
            row = msgpack.unpackb(p["frames"][0][len(b"g1_debug") :], raw=False)
            row[field] = value
            p["frames"] = [b"g1_debug" + msgpack.packb(row, use_bin_type=True)]


def mutate_cameras(packets, change):
    for packet in packets:
        if packet["source"] == "camera":
            payload = msgpack.unpackb(packet["frames"][0], raw=False)
            change(payload)
            packet["frames"] = [msgpack.packb(payload, use_bin_type=True)]


def test_registered_provider_and_unapproved_example(g1_profile):
    assert isinstance(get_provider("g1_sonic"), G1SONICProvider)
    provider = G1SONICProvider()
    provider.validate_profile(g1_profile)
    assert provider.profile_blockers(g1_profile) == []
    original = json.loads(Path("configs/g1-sonic11-brainco-v1.json").read_text())
    assert {b["field"] for b in provider.profile_blockers(original)} == {
        "semantics_approved",
        "clock_verified",
    }


def test_prepare_preserves_raw_and_does_not_create_intermediate_dataset(
    tmp_path, g1_profile
):
    path, _ = write_episode(tmp_path / "raw/episode0")
    before = path.read_bytes()
    provider = G1SONICProvider()
    report = provider.prepare(path.parent.parent, tmp_path / "work", g1_profile)
    assert report["frames"] == 4
    staged = Path(report["aligned_path"])
    assert [p.name for p in staged.iterdir()] == [SOURCE_DESCRIPTOR]
    descriptor = json.loads((staged / SOURCE_DESCRIPTOR).read_text())
    assert descriptor["raw_path"] == str(path)
    assert descriptor["profile_hash"] == digest(g1_profile)
    assert path.read_bytes() == before
    assert provider.quality(staged, g1_profile)["status"] == "PASS"
    assert not list(tmp_path.rglob("*.hdf5")) and not list(tmp_path.rglob("*.parquet"))


def test_latest_not_future_preserves_distinct_state_and_action(tmp_path, g1_profile):
    path, _ = write_episode(tmp_path / "episode")
    recording = load(path, g1_profile)
    arrays = recording.arrays
    assert arrays["observation.state"].shape == (4, 41)
    assert arrays["action"].shape == (4, 76)
    assert arrays["action.wbc"].shape == (4, 41)
    np.testing.assert_allclose(arrays["observation.state"][:, 0], [0, 0.02, 0.03, 0.04])
    np.testing.assert_allclose(arrays["action"][:, 0], [0, 0.02, 0.03, 0.04])
    np.testing.assert_allclose(arrays["action"][:, 64:70], 0.7)
    np.testing.assert_allclose(arrays["observation.state"][:, 29:35], 0.2)
    np.testing.assert_allclose(arrays["source.robot_age_ms"], [10, 10, 10, 10])
    np.testing.assert_array_equal(
        arrays["source.monotonic_ns"], START + np.arange(4) * 100_000_000
    )
    assert len(list(iter_raw_packets(path))) == 11


def test_explicit_padding_preserves_native_values(tmp_path, g1_profile):
    g1_profile["g1_sonic"]["model_layout"] = "psi0_45_80"
    path, _ = write_episode(tmp_path / "episode")
    arrays = load(path, g1_profile).arrays
    for source, target, pads in (
        ("observation.state", "observation.model_state", STATE_PADDING),
        ("action", "action.model", ACTION_PADDING),
    ):
        np.testing.assert_array_equal(
            np.delete(arrays[target], pads, axis=1), arrays[source]
        )
        assert not arrays[target][:, pads].any()
    assert arrays["action.model_mask"].shape == (4, 80)
    assert not arrays["action.model_mask"][:, ACTION_PADDING].any()
    assert arrays["action.model_mask"].sum() == 4 * 76
    assert numeric_features(g1_profile)["action"]["units"][0] == "latent"


def test_first_pico_start_packet_may_precede_episode_without_changing_raw(
    tmp_path, g1_profile
):
    def mutate(packets):
        packet = _packet("pico", "manager_state", START - 1_005_146, {})
        packet["context"] = False
        packets.insert(3, packet)

    path, _ = write_episode(tmp_path / "episode", mutate=mutate)
    before = path.read_bytes()
    recording = load(path, g1_profile)
    assert recording.report["pre_start_control_lead_ms"] == [1.005146]
    assert recording.report["frames"] == 4
    np.testing.assert_allclose(recording.arrays["source.robot_age_ms"], 10)
    assert path.read_bytes() == before
    assert len(list(iter_raw_packets(path))) == 12


@pytest.mark.parametrize(
    "source,topic,relative,first",
    [
        ("robot", "g1_debug", -1_000_000, True),
        ("robot", "robot_config", -1_000_000, True),
        ("camera", "", -1_000_000, True),
        ("pico", "pose", -1_000_000, True),
        ("pico", "manager_state", -1_000_000, False),
        ("pico", "manager_state", 401_000_000, True),
        ("robot", "g1_debug", 401_000_000, False),
        ("camera", "", 401_000_000, False),
    ],
)
def test_other_noncontext_packets_still_require_episode_bounds(
    tmp_path, g1_profile, source, topic, relative, first
):
    def mutate(packets):
        packet = _packet(source, topic, START + relative, {})
        packet["context"] = False
        packets.insert(3 if first else len(packets), packet)

    path, _ = write_episode(tmp_path / "episode", mutate=mutate)
    with pytest.raises(DomainError, match="outside the declared episode"):
        load(path, g1_profile)


def test_second_pre_start_manager_packet_is_rejected(tmp_path, g1_profile):
    def mutate(packets):
        for relative in (-1_000_000, -2_000_000):
            packet = _packet("pico", "manager_state", START + relative, {})
            packet["context"] = False
            packets.insert(3, packet)

    path, _ = write_episode(tmp_path / "episode", mutate=mutate)
    with pytest.raises(DomainError, match="outside the declared episode"):
        load(path, g1_profile)


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_state", [0] * 63),
        ("body_q", [float("nan")] * 29),
        ("left_hand_q", [True] * 6),
    ],
)
def test_recording_defects_are_explicitly_skippable(tmp_path, g1_profile, field, value):
    path, _ = write_episode(
        tmp_path / "episode", mutate=lambda ps: mutate_robot(ps, field, value)
    )
    with pytest.raises(EpisodeValidationError):
        load(path, g1_profile)


def test_integrity_error_is_not_hidden_by_an_earlier_bad_vector(tmp_path, g1_profile):
    path, episode = write_episode(
        tmp_path / "episode", mutate=lambda ps: mutate_robot(ps, "token_state", [0])
    )
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    episode["sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(DomainError, match="CRC") as error:
        load(path, g1_profile, episode)
    assert not isinstance(error.value, EpisodeValidationError)


@pytest.mark.parametrize(
    "field,value", [("state", "aborted"), ("end_monotonic_ns", START)]
)
def test_integrity_error_precedes_invalid_episode_timing(
    tmp_path, g1_profile, field, value
):
    path, episode = write_episode(tmp_path / "episode")
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    episode[field] = value
    with pytest.raises(DomainError, match="CRC") as error:
        load(path, g1_profile, episode)
    assert not isinstance(error.value, EpisodeValidationError)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda p: p.update(frame_metadata=[]), "metadata must be mappings"),
        (lambda p: p.update(timestamps=None), "metadata must be mappings"),
        (lambda p: p.update(frame_metadata={"ego_view": []}), "must be an object"),
        (lambda p: p.update(images={}), "missing"),
        (lambda p: p["images"].update(ego_view="aW52YWxpZA=="), "cannot be decoded"),
        (
            lambda p: p["frame_metadata"]["ego_view"].update(
                host_capture_monotonic_ns=START + 900_000_000
            ),
            "ahead of receiver clock",
        ),
    ],
)
def test_camera_defects_are_reported(tmp_path, g1_profile, change, message):
    path, _ = write_episode(
        tmp_path / "episode", mutate=lambda ps: mutate_cameras(ps, change)
    )
    with pytest.raises(EpisodeValidationError, match=message):
        load(path, g1_profile)


@pytest.mark.parametrize("independent", [False, True])
def test_all_three_cameras_support_combined_and_independent_packets(
    tmp_path, g1_profile, independent
):
    g1_profile["cameras"] = [
        {"id": key, "source_key": key, "width": 32, "height": 24}
        for key in ("ego_view", "left_wrist", "right_wrist")
    ]

    def split_packets(packets):
        for packet in list(packets):
            if packet["source"] != "camera":
                continue
            payload = msgpack.unpackb(packet["frames"][0], raw=False)
            for key in ("left_wrist", "right_wrist"):
                separate = copy.deepcopy(packet)
                separate["frames"] = [
                    msgpack.packb(
                        {
                            name: {key: values.pop(key)}
                            for name, values in payload.items()
                        },
                        use_bin_type=True,
                    )
                ]
                packets.append(separate)
            packet["frames"] = [msgpack.packb(payload, use_bin_type=True)]
        packets.sort(key=lambda p: p["receive_monotonic_ns"])

    path, _ = write_episode(
        tmp_path / "episode",
        combined=True,
        mutate=split_packets if independent else None,
    )
    recording = load(path, g1_profile)
    assert set(recording.cameras) == {"ego_view", "left_wrist", "right_wrist"}
    for frames in recording.cameras.values():
        assert len(frames.offsets) == 4


def test_changed_config_inside_episode_is_fatal(tmp_path, g1_profile):
    def mutate(packets):
        changed = {**CONFIG, "model_path": "other/sonic_v1_1/model_decoder.onnx"}
        packets.append(_packet("robot", "robot_config", START + 250_000_000, changed))
        packets.sort(key=lambda p: p["receive_monotonic_ns"])

    path, _ = write_episode(tmp_path / "episode", mutate=mutate)
    with pytest.raises(DomainError, match="configuration changed") as error:
        load(path, g1_profile)
    assert not isinstance(error.value, EpisodeValidationError)


def test_prepared_profile_binding_cannot_be_reused_with_changed_profile(
    tmp_path, g1_profile
):
    path, _ = write_episode(tmp_path / "episode")
    provider = G1SONICProvider()
    prepared = provider.prepare(path, tmp_path / "prepared", g1_profile)
    g1_profile["output_fps"] = 20
    with pytest.raises(DomainError, match="binding mismatch"):
        provider.quality(Path(prepared["aligned_path"]), g1_profile)


@pytest.mark.parametrize(
    "field,value", [("sha256", "0" * 64), ("packet_count", 999), ("bytes", 1)]
)
def test_metadata_integrity_mismatch_is_fatal(tmp_path, g1_profile, field, value):
    path, episode = write_episode(tmp_path / "episode")
    episode[field] = value
    with pytest.raises(DomainError) as error:
        load(path, g1_profile, episode)
    assert not isinstance(error.value, EpisodeValidationError)


def test_stale_sources_are_rejected(tmp_path, g1_profile):
    path, _ = write_episode(tmp_path / "episode", times=[-10_000_000, 10_000_000])
    with pytest.raises(EpisodeValidationError, match="age budget"):
        load(path, g1_profile)


def test_wall_clock_fallback_is_explicit_and_reported(tmp_path, g1_profile):
    path, _ = write_episode(tmp_path / "episode", mapped_wall=True)
    report = load(path, g1_profile).report
    assert report["camera_timestamp_fallbacks"]["ego_view"] == 5
    g1_profile["g1_sonic"]["camera_time_fallback"] = "forbid"
    with pytest.raises(EpisodeValidationError, match="capture monotonic"):
        load(path, g1_profile)


def test_pause_time_is_removed_without_borrowing_future_samples(tmp_path, g1_profile):
    path, episode = write_episode(
        tmp_path / "episode", times=[0, 200_000_000, 300_000_000]
    )
    episode["pause_intervals"] = [
        {
            "start_monotonic_ns": START + 100_000_000,
            "end_monotonic_ns": START + 200_000_000,
        }
    ]
    recording = load(path, g1_profile, episode)
    assert recording.report["frames"] == 3
    np.testing.assert_array_equal(
        recording.arrays["source.robot_monotonic_ns"],
        [START, START + 200_000_000, START + 300_000_000],
    )


def test_zero_and_outside_nominal_hands_are_preserved_and_reported(
    tmp_path, g1_profile
):
    def mutate(packets):
        mutate_robot(packets, "left_hand_q", [0] * 6)
        mutate_robot(packets, "right_hand_q", [0] * 6)
        mutate_robot(packets, "last_left_hand_action", [1.5] * 6)

    path, _ = write_episode(tmp_path / "episode", mutate=mutate)
    recording = load(path, g1_profile)
    assert len(recording.report["warnings"]) == 2
    np.testing.assert_array_equal(recording.arrays["action"][:, 64:70], 1.5)


def test_unknown_hand_config_and_profile_never_fall_back(tmp_path, g1_profile):
    def mutate(packets):
        packets[0]["frames"] = [
            b"robot_config"
            + msgpack.packb({**CONFIG, "hand_type": "dex3"}, use_bin_type=True)
        ]

    path, _ = write_episode(tmp_path / "episode", mutate=mutate)
    with pytest.raises(DomainError, match="SONIC 1.1 / BrainCo") as error:
        load(path, g1_profile)
    assert not isinstance(error.value, EpisodeValidationError)
    g1_profile["robot_type"] = "agilex_aloha"
    with pytest.raises(DomainError, match="robot_type"):
        G1SONICProvider().prepare(path, tmp_path / "work", g1_profile)


def test_symlinks_and_ambiguous_episode_selection_are_rejected(tmp_path, g1_profile):
    path, _ = write_episode(tmp_path / "source/episodes/episode_000000")
    write_episode(tmp_path / "source/episodes/episode_000001")
    with pytest.raises(DomainError, match="exactly one"):
        recording_files(tmp_path / "source")
    link = tmp_path / "linked.raw"
    link.symlink_to(path)
    with pytest.raises(DomainError, match="symlink"):
        recording_files(link)


def test_truncated_raw_record_is_fatal(tmp_path):
    path = tmp_path / "packets.raw"
    path.write_bytes(MAGIC + struct.pack("<QI", 100, 0) + b"short")
    with pytest.raises(DomainError, match="truncated"):
        list(iter_raw_packets(path))


def test_raw_source_publication_guards(tmp_path, g1_profile, letools_binary):
    path, _ = write_episode(tmp_path / "raw/episodes/episode_000000")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(g1_profile))
    script = """
import json, sys
from pathlib import Path
from pipeline.server.providers.g1_sonic_source import G1SONICSource
from pipeline.shared.core import DomainError
root, profile_path = map(Path, sys.argv[1:])
source = G1SONICSource(root, json.loads(profile_path.read_text()), 'Move the box', raw=True)
for path in (root / 'converted', root / 'episodes/episode_000000/converted'):
    try:
        source.validate_destination(path)
    except DomainError:
        pass
    else:
        raise AssertionError('Source directory accepted as output')
try:
    source.validate_destination(root)
except FileExistsError:
    pass
else:
    raise AssertionError('Existing output accepted')
source.validate_destination(root.parent / 'converted')
packet = root / 'episodes/episode_000000/packets.raw'
with packet.open('r+b') as stream:
    stream.seek(-1, 2)
    stream.write(b'!')
try:
    source.verify_sources()
except DomainError:
    pass
else:
    raise AssertionError('Changed source accepted for publication')
"""
    result = subprocess.run(
        [
            str(Path(letools_binary).with_name("python")),
            "-c",
            script,
            str(path.parents[2]),
            str(profile_path),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_raw_cli_validates_jpeg_dimensions(tmp_path, g1_profile, letools_binary):
    path, _ = write_episode(tmp_path / "raw")
    g1_profile["cameras"][0]["width"] = 64
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(g1_profile))
    destination = tmp_path / "dataset"
    result = subprocess.run(
        [
            str(Path(letools_binary).with_name("python")),
            str(Path("pipeline/server/providers/g1_sonic_source.py").absolute()),
            str(path.parent),
            str(destination),
            "--raw",
            "--profile",
            str(profile_path),
            "--task",
            "Move the box",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0 and "format/size" in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_direct_batch_conversion(tmp_path, g1_profile, letools_binary, version):
    provider = G1SONICProvider()
    g1_profile["g1_sonic"]["model_layout"] = "psi0_45_80"
    aligned = tmp_path / "aligned"
    aligned.mkdir()
    for index in range(2):
        path, _ = write_episode(tmp_path / f"raw{index}/episode0")
        prepared = provider.prepare(path, tmp_path / f"prepared{index}", g1_profile)
        Path(prepared["aligned_path"]).rename(aligned / f"episode{index}")
    destination = tmp_path / "dataset"
    args = provider.conversion_args(
        executable=letools_binary,
        source=aligned,
        destination=destination,
        profile=g1_profile,
        job={
            "instruction": "Move the box",
            "target_format": version,
            "convert_workers": 2,
            "video_workers": 2,
        },
    )
    result = subprocess.run(
        args, capture_output=True, text=True, cwd=tmp_path, timeout=120
    )
    assert result.returncode == 0, result.stderr
    info = json.loads((destination / "meta/info.json").read_text())
    assert info["total_episodes"] == 2 and info["total_frames"] == 8
    assert info["codebase_version"] == version
    assert info["features"]["action"]["shape"] == [76]
    assert info["features"]["action"]["units"][0] == "latent"
    assert info["features"]["observation.state"]["shape"] == [41]
    assert info["features"]["action.model"]["shape"] == [80]
    assert info["g1_sonic"]["intermediate_format"] is None
    assert (destination / "meta/modality.json").is_file()
    validation = subprocess.run(
        [letools_binary, "validate", str(destination), "--deep"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert validation.returncode == 0, validation.stdout + validation.stderr
    check = subprocess.run(
        [
            args[0],
            "-c",
            "import json,sys; from letools import open_dataset; s=open_dataset(sys.argv[1]); ts=[s.read_episode(e) for e in s.episodes]; print(json.dumps({'indices':[t['index'].to_pylist() for t in ts], 'frames':[t['frame_index'].to_pylist() for t in ts], 'state':[t['observation.state'].to_pylist() for t in ts], 'action':[t['action'].to_pylist() for t in ts]}))",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert check.returncode == 0, check.stderr
    data = json.loads(check.stdout)
    assert data["indices"] == [[0, 1, 2, 3], [4, 5, 6, 7]]
    assert data["frames"] == [[0, 1, 2, 3]] * 2
    np.testing.assert_allclose(
        np.asarray(data["state"])[0, :, 0], [0, 0.02, 0.03, 0.04]
    )
    np.testing.assert_allclose(np.asarray(data["action"])[:, :, 64:70], 0.7)
    assert not list(tmp_path.rglob("*.hdf5"))


@pytest.mark.parametrize("processing_mode", ["demo", "real"])
def test_provider_uses_existing_server_batch_pipeline(
    settings_factory, tmp_path, g1_profile, request, processing_mode
):
    from test_multi_runtime import manifest_for
    from test_processing import upload_sample

    from pipeline.server.ingest import IngestService
    from pipeline.server.jobs import JobRunner

    settings = settings_factory(
        "server",
        processing_mode=processing_mode,
        **(
            {"letools_bin": request.getfixturevalue("letools_binary")}
            if processing_mode == "real"
            else {}
        ),
        profiles={
            "g1": {
                "profile": g1_profile,
                "provider": "g1_sonic",
                "raw_formats": [RAW_FORMAT],
            }
        },
        default_profile="g1",
    )
    ingest = IngestService(settings)
    ids = []
    for index in range(2):
        path, _ = write_episode(tmp_path / f"capture{index}/episode0")
        manifest, _ = manifest_for(
            settings,
            g1_profile,
            robot_id=settings.robot_id,
            project_id=settings.project_id,
            raw_format=RAW_FORMAT,
        )
        manifest["session_id"] = f"g1-source-{index}"
        manifest["capture_mode"] = "ros2"
        manifest["files"] = [
            {
                "path": f"episode0/{p.name}",
                "size_bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in sorted(path.parent.iterdir())
        ]
        ids.append(upload_sample(ingest, path.parent.parent, manifest))
    runner = JobRunner(settings, ingest)
    job = runner.enqueue_collection(
        ingest.store.get("upload", ids[0])["collection_id"],
        "Move the box",
        "v3.0",
        processing_mode == "real",
    )
    runner.tick()
    result = ingest.store.get("job", job["id"])
    assert result["state"] == (
        "DEMO_READY" if processing_mode == "demo" else "QUARANTINED"
    ), result
    assert result["provider"] == "g1_sonic" and result["runtime_id"] == "g1"
    assert result["alignment_report"]["frames"] == 8
    assert result["alignment_report"]["episode_count"] == 2
    if processing_mode == "real":
        assert result["dataset_ready"]
        assert result["doctor_report"]["total_frames"] == 8
        assert not result["quality_gate"]["passed"]
        assert not list(Path(result["work_path"]).rglob("*.hdf5"))
