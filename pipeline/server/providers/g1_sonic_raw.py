"""Read-only SONIC 1.1 / BrainCo raw decoding and causal frame selection.

Image bytes stay in packets.raw. An index holds record offsets, numeric arrays,
and the selected source times; it never writes an intermediate data format.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import msgpack
import numpy as np

from pipeline.shared.core import DomainError, confined

from ..episode_validation import EpisodeValidationError

MAGIC = b"GR00TRAW\x01"
HEADER = struct.Struct("<QI")
MAX_RECORD_BYTES = 256 * 1024 * 1024
RAW_FORMAT = "g1_sonic_raw_v1"
ROBOT_TYPE = "g1_brainco"
SCHEMA = "g1-brainco-sonic11-v1"
BODY_NAMES = (
    "left_hip_pitch_joint",
    "left_hip_roll_joint",
    "left_hip_yaw_joint",
    "left_knee_joint",
    "left_ankle_pitch_joint",
    "left_ankle_roll_joint",
    "right_hip_pitch_joint",
    "right_hip_roll_joint",
    "right_hip_yaw_joint",
    "right_knee_joint",
    "right_ankle_pitch_joint",
    "right_ankle_roll_joint",
    "waist_yaw_joint",
    "waist_roll_joint",
    "waist_pitch_joint",
    "left_shoulder_pitch_joint",
    "left_shoulder_roll_joint",
    "left_shoulder_yaw_joint",
    "left_elbow_joint",
    "left_wrist_roll_joint",
    "left_wrist_pitch_joint",
    "left_wrist_yaw_joint",
    "right_shoulder_pitch_joint",
    "right_shoulder_roll_joint",
    "right_shoulder_yaw_joint",
    "right_elbow_joint",
    "right_wrist_roll_joint",
    "right_wrist_pitch_joint",
    "right_wrist_yaw_joint",
)
HAND_NAMES = tuple(
    f"{side}_{motor}"
    for side in ("left", "right")
    for motor in ("thumb", "thumb_aux", "index", "middle", "ring", "pinky")
)
STATE_NAMES = (*BODY_NAMES, *HAND_NAMES)
ACTION_NAMES = (*(f"sonic11_token_{i}" for i in range(64)), *HAND_NAMES)
STATE_UNITS = (*(["rad"] * 29), *(["normalized"] * 12))
ACTION_UNITS = (*(["latent"] * 64), *(["normalized"] * 12))
STATE_PADDING = (35, 42, 43, 44)
ACTION_PADDING = (70, 77, 78, 79)
FIELDS = {
    "body_q": 29,
    "left_hand_q": 6,
    "right_hand_q": 6,
    "last_action": 29,
    "last_left_hand_action": 6,
    "last_right_hand_action": 6,
    "token_state": 64,
}


def regular_file(path: Path) -> Path:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise DomainError(f"G1 source must be a regular file without symlinks: {path}")
    return path


def json_object(path: Path) -> dict:
    regular_file(path)
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise DomainError(f"Invalid G1 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DomainError(f"G1 metadata must be an object: {path}")
    return value


def recording_files(raw: Path) -> tuple[Path, dict, dict | None]:
    """Select exactly one episode; batch expansion belongs to the caller."""
    if raw.is_file():
        packet = regular_file(raw)
    else:
        candidates = [raw / "packets.raw", raw / "episode0/packets.raw"]
        candidates += sorted(raw.glob("episodes/episode_*/packets.raw"))
        candidates = [p for p in candidates if p.exists()]
        if len(candidates) != 1:
            raise DomainError(
                "G1 input must select exactly one saved packets.raw episode"
            )
        packet = regular_file(candidates[0])
    if packet.name != "packets.raw":
        raise DomainError("G1 only accepts finalized packets.raw, never partial files")
    episode = json_object(packet.with_name("episode.json"))
    config_paths = [packet.with_name("robot_config.json")]
    if packet.parent.parent.name == "episodes":
        root = packet.parent.parent.parent
        capture = episode.get("capture_directory", ".")
        capture_root = root if capture == "." else confined(root, capture)
        config_paths.append(capture_root / "robot_config.json")
    config = next((json_object(p) for p in config_paths if p.exists()), None)
    return packet, episode, config


def read_record(stream, *, digest=None) -> dict | None:
    header = stream.read(HEADER.size)
    if not header:
        return None
    if len(header) != HEADER.size:
        raise DomainError("G1 raw record header is truncated")
    size, crc = HEADER.unpack(header)
    if not 0 < size <= MAX_RECORD_BYTES:
        raise DomainError("G1 raw record length is invalid")
    payload = stream.read(size)
    if len(payload) != size or zlib.crc32(payload) != crc:
        raise DomainError("G1 raw record is truncated or has a CRC mismatch")
    if digest is not None:
        digest.update(header)
        digest.update(payload)
    try:
        value = msgpack.unpackb(payload, raw=False)
    except (ValueError, TypeError) as exc:
        raise DomainError("G1 raw record is not valid MessagePack") from exc
    if not isinstance(value, dict):
        raise DomainError("G1 raw record envelope must be an object")
    return value


def iter_raw_packets(path: Path):
    """Yield CRC-checked envelopes from the versioned raw format."""
    for _, packet in _iter_records(path):
        yield packet


def _iter_records(path: Path, expected_sha256: str | None = None):
    with regular_file(path).open("rb") as stream:
        magic = stream.read(len(MAGIC))
        if magic != MAGIC:
            raise DomainError("Unsupported G1 raw file header")
        digest = hashlib.sha256(magic)
        while True:
            offset = stream.tell()
            packet = read_record(stream, digest=digest)
            if packet is None:
                break
            yield offset, packet
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise DomainError("G1 raw SHA256 mismatch")


def _payload(packet: dict, prefix: bytes = b"") -> dict:
    frames = packet.get("frames")
    if not isinstance(frames, list) or not frames or not isinstance(frames[0], bytes):
        raise EpisodeValidationError("G1 packet has no original ZMQ frame")
    if not frames[0].startswith(prefix):
        raise EpisodeValidationError("G1 ZMQ topic prefix disagrees with envelope")
    try:
        result = msgpack.unpackb(frames[0][len(prefix) :], raw=False)
    except (ValueError, TypeError) as exc:
        raise EpisodeValidationError("Invalid G1 ZMQ MessagePack payload") from exc
    if not isinstance(result, dict):
        raise EpisodeValidationError("G1 ZMQ payload must be an object")
    return result


def _config_identity(config: dict) -> tuple:
    model = config.get("model_path")
    if (
        config.get("hand_type") != "brainco"
        or config.get("hand_num_motors") != 6
        or config.get("hand_units") != "normalized"
        or not isinstance(model, str)
        or "sonic_v1_1" not in PurePosixPath(model).parts
    ):
        raise DomainError(
            "G1SONICProvider requires recorded SONIC 1.1 / BrainCo configuration"
        )
    return tuple(
        config.get(k)
        for k in ("hand_type", "hand_num_motors", "hand_units", "model_path")
    )


def _vector(row: dict, name: str, dimensions: int) -> np.ndarray:
    values = row.get(name)
    if (
        not isinstance(values, list)
        or len(values) != dimensions
        or any(type(v) not in (int, float) for v in values)
    ):
        raise EpisodeValidationError(
            f"G1 {name} must contain {dimensions} numeric values"
        )
    result = np.asarray(values, dtype=np.float64)
    if (
        not np.isfinite(result).all()
        or (np.abs(result) > np.finfo(np.float32).max).any()
    ):
        raise EpisodeValidationError(
            f"G1 {name} contains non-finite or unrepresentable values"
        )
    return result.astype(np.float32)


def _ns(value, label: str) -> int:
    if type(value) is not int or not 0 <= value <= np.iinfo(np.int64).max:
        raise EpisodeValidationError(
            f"G1 {label} must be nonnegative int64 nanoseconds"
        )
    return value


def _grid(
    episode: dict, fps: int, maximum: int
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    start = _ns(episode.get("start_monotonic_ns"), "episode start")
    end = _ns(episode.get("end_monotonic_ns"), "episode end")
    if end <= start:
        raise EpisodeValidationError("G1 episode has no positive duration")
    pauses = episode.get("pause_intervals", [])
    if not isinstance(pauses, list):
        raise EpisodeValidationError("Invalid G1 pause intervals")
    cursor, segments = start, []
    for pause in pauses:
        if not isinstance(pause, dict):
            raise EpisodeValidationError("Invalid G1 pause interval")
        a = _ns(pause.get("start_monotonic_ns"), "pause start")
        b = _ns(pause.get("end_monotonic_ns"), "pause end")
        if not cursor <= a <= b <= end:
            raise EpisodeValidationError("G1 pause intervals overlap or exceed episode")
        segments.append((cursor, a))
        cursor = b
    segments.append((cursor, end))
    lengths = [(b - a) * fps // 1_000_000_000 for a, b in segments]
    if not 0 < sum(lengths) <= maximum:
        raise EpisodeValidationError(
            "G1 active frame count is empty or exceeds Profile max_frames"
        )
    grid = np.concatenate(
        [
            a + np.rint(np.arange(n) * 1e9 / fps).astype(np.int64)
            for (a, _), n in zip(segments, lengths, strict=True)
            if n
        ]
    )
    return grid, segments


def _select(
    times: list[int], grid: np.ndarray, age_ms: float, label: str, segments
) -> np.ndarray:
    values = np.asarray(times, dtype=np.int64)
    if not len(values) or (np.diff(values) < 0).any():
        raise EpisodeValidationError(
            f"G1 {label} is missing or its timestamps move backwards"
        )
    selected = np.searchsorted(values, grid, side="right") - 1
    if (selected < 0).any():
        raise EpisodeValidationError(
            f"G1 {label} has no sample before the output frame"
        )
    age = grid - values[selected]
    if (age > round(age_ms * 1e6)).any():
        raise EpisodeValidationError(
            f"G1 {label} exceeds the {age_ms:g} ms source age budget"
        )
    for previous, current in zip(segments, segments[1:]):
        if previous[1] < current[0]:
            after_pause = (grid >= current[0]) & (grid < current[1])
            if (values[selected[after_pause]] < current[0]).any():
                raise EpisodeValidationError(
                    f"G1 {label} would reuse a sample across a pause"
                )
    return selected


def image_bytes(packet: dict, source_key: str) -> bytes:
    payload = _payload(packet)
    images = payload.get("images")
    image = images.get(source_key) if isinstance(images, dict) else None
    if not isinstance(image, (str, bytes)):
        raise EpisodeValidationError(
            f"G1 image {source_key} is absent or not encoded bytes"
        )
    if isinstance(image, bytes) and image.startswith(b"\xff\xd8"):
        return image
    try:
        return base64.b64decode(image, validate=True)
    except (ValueError, TypeError) as exc:
        raise EpisodeValidationError(
            f"G1 image {source_key} is not JPEG/base64"
        ) from exc


def read_image(stream, offset: int, source_key: str) -> bytes:
    stream.seek(offset)
    packet = read_record(stream)
    if packet is None or packet.get("source") != "camera":
        raise DomainError("G1 camera record changed after indexing")
    return image_bytes(packet, source_key)


@dataclass
class CameraFrames:
    source_key: str
    width: int
    height: int
    offsets: np.ndarray


@dataclass
class RecordingIndex:
    path: Path
    episode: dict
    robot_config: dict
    arrays: dict[str, np.ndarray]
    cameras: dict[str, CameraFrames]
    report: dict


def index_recording(
    path: Path,
    episode: dict,
    profile: dict,
    robot_config: dict | None = None,
    *,
    check_images: bool = True,
) -> RecordingIndex:
    """Check integrity, bind the recorded schema, and select causal samples."""
    path = regular_file(path)
    if episode.get("file") != "packets.raw":
        raise DomainError("G1 episode metadata does not name finalized packets.raw")
    expected = episode.get("sha256")
    if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
        raise DomainError("G1 episode metadata requires its raw SHA256")
    if episode.get("bytes") != path.stat().st_size:
        raise DomainError("G1 raw size disagrees with episode metadata")
    cfg = profile["g1_sonic"]
    identity = _config_identity(robot_config) if robot_config is not None else None
    robot, robot_times, robot_sequences = [], [], []
    cameras = {c["source_key"]: [] for c in profile["cameras"]}
    fallbacks = {key: 0 for key in cameras}
    counts = {True: 0, False: 0}
    # Finish integrity checks before classifying any recording-local defect.
    # Otherwise a malformed first sample could hide a later CRC/SHA failure
    # under the batch's skip-invalid-episodes policy.
    for _, packet in _iter_records(path, expected):
        if not isinstance(packet, dict) or type(packet.get("context")) is not bool:
            raise DomainError("Invalid G1 raw envelope/context marker")
        counts[packet["context"]] += 1
    if counts[False] != episode.get("packet_count") or counts[True] != episode.get(
        "context_count"
    ):
        raise DomainError("G1 raw packet counts disagree with episode metadata")
    if episode.get("state") != "saved":
        raise EpisodeValidationError("G1 episode is not saved")
    grid, segments = _grid(
        episode, profile["output_fps"], cfg.get("max_frames", 1_000_000)
    )
    first_recorded_packet = True
    pre_start_control_lead_ms = []
    for offset, packet in _iter_records(path, expected):
        if not isinstance(packet, dict) or type(packet.get("context")) is not bool:
            raise DomainError("Invalid G1 raw envelope/context marker")
        received = _ns(packet.get("receive_monotonic_ns"), "receive timestamp")
        # The collector handles the PICO start command before appending its
        # original packet, so that first control packet predates episode start.
        start_control = (
            first_recorded_packet
            and not packet["context"]
            and packet.get("source") == "pico"
            and packet.get("topic") == "manager_state"
            and received < episode["start_monotonic_ns"]
        )
        if (
            not packet["context"]
            and not episode["start_monotonic_ns"]
            <= received
            <= episode["end_monotonic_ns"]
            and not start_control
        ):
            raise DomainError("G1 packet lies outside the declared episode")
        if not packet["context"]:
            first_recorded_packet = False
        if start_control:
            pre_start_control_lead_ms.append(
                (episode["start_monotonic_ns"] - received) / 1e6
            )
            continue
        if packet.get("source") == "robot" and packet.get("topic") == "robot_config":
            recorded = _payload(packet, b"robot_config")
            current = _config_identity(recorded)
            if identity is not None and current != identity:
                raise DomainError("G1 robot configuration changed inside the recording")
            identity, robot_config = current, recorded
        elif packet.get("source") == "robot" and packet.get("topic") == "g1_debug":
            row = _payload(packet, b"g1_debug")
            robot.append({k: _vector(row, k, n) for k, n in FIELDS.items()})
            robot_times.append(received)
            robot_sequences.append(
                _ns(packet.get("receive_sequence"), "robot receive sequence")
            )
        elif packet.get("source") == "camera":
            payload = _payload(packet)
            images = payload.get("images")
            if not isinstance(images, dict):
                raise EpisodeValidationError("G1 camera packet has no image mapping")
            metadata = payload.get("frame_metadata", {})
            timestamps = payload.get("timestamps", {})
            if not isinstance(metadata, dict) or not isinstance(timestamps, dict):
                raise EpisodeValidationError("G1 camera metadata must be mappings")
            for key, samples in cameras.items():
                if key not in images:
                    continue
                frame_meta = metadata.get(key, {})
                if not isinstance(frame_meta, dict):
                    raise EpisodeValidationError(
                        f"G1 {key} frame metadata must be an object"
                    )
                captured = frame_meta.get("host_capture_monotonic_ns")
                rounding_ns = 0
                if captured is None:
                    if cfg.get("camera_time_fallback") != "mapped_wall":
                        raise EpisodeValidationError(
                            f"G1 {key} lacks host capture monotonic time"
                        )
                    stamp = timestamps.get(key)
                    if type(stamp) not in (int, float) or not math.isfinite(stamp):
                        raise EpisodeValidationError(
                            f"G1 {key} has no usable camera timestamp"
                        )
                    wall = _ns(packet.get("receive_time_ns"), "receive wall timestamp")
                    captured = received + round(stamp * 1e9) - wall
                    # Epoch seconds stored as float lose sub-microsecond precision.
                    # Preserve the mapped timestamp; causal selection still forbids
                    # using it before its mapped capture time.
                    rounding_ns = 1024
                    fallbacks[key] += 1
                captured = _ns(captured, "camera capture timestamp")
                if captured > received + rounding_ns:
                    raise EpisodeValidationError(
                        f"G1 {key} capture time is ahead of receiver clock"
                    )
                samples.append((captured, offset))
    if identity is None or robot_config is None:
        raise DomainError(
            "G1 recording has no robot_config; refusing to guess hand or SONIC version"
        )
    ri = _select(robot_times, grid, cfg["robot_max_age_ms"], "robot", segments)
    chosen = [robot[i] for i in ri]
    matrix = {key: np.stack([r[key] for r in chosen]) for key in FIELDS}
    hands = np.concatenate(
        [matrix["last_left_hand_action"], matrix["last_right_hand_action"]], axis=1
    )
    arrays = {
        "observation.state": np.concatenate(
            [matrix["body_q"], matrix["left_hand_q"], matrix["right_hand_q"]], axis=1
        ),
        "action": np.concatenate([matrix["token_state"], hands], axis=1),
        "action.wbc": np.concatenate([matrix["last_action"], hands], axis=1),
        "source.monotonic_ns": grid,
        "source.robot_monotonic_ns": np.asarray(robot_times, dtype=np.int64)[ri],
        "source.robot_receive_sequence": np.asarray(robot_sequences, dtype=np.int64)[
            ri
        ],
    }
    arrays["source.robot_age_ms"] = (
        (grid - arrays["source.robot_monotonic_ns"]) / 1e6
    ).astype(np.float32)
    if cfg.get("model_layout") == "psi0_45_80":
        for source, target, padding in (
            ("observation.state", "observation.model_state", STATE_PADDING),
            ("action", "action.model", ACTION_PADDING),
        ):
            values = np.zeros(
                (len(grid), arrays[source].shape[1] + len(padding)), dtype=np.float32
            )
            keep = [i for i in range(values.shape[1]) if i not in padding]
            values[:, keep] = arrays[source]
            arrays[target] = values
        mask = np.ones(80, dtype=np.float32)
        mask[list(ACTION_PADDING)] = 0
        arrays["action.model_mask"] = np.broadcast_to(mask, (len(grid), 80)).copy()
    media = {}
    for camera in profile["cameras"]:
        key, camera_id = camera["source_key"], camera["id"]
        times = [t for t, _ in cameras[key]]
        ci = _select(times, grid, cfg["camera_max_age_ms"], key, segments)
        offsets = np.asarray([cameras[key][i][1] for i in ci], dtype=np.int64)
        if check_images:
            from PIL import Image

            with path.open("rb") as stream:
                for offset in np.unique(offsets):
                    encoded = read_image(stream, int(offset), key)
                    try:
                        with Image.open(io.BytesIO(encoded)) as image:
                            if (
                                image.format != "JPEG"
                                or image.mode != "RGB"
                                or image.size != (camera["width"], camera["height"])
                            ):
                                raise EpisodeValidationError(
                                    f"G1 {key} image format/size disagrees with Profile"
                                )
                            image.load()
                    except (OSError, ValueError) as exc:
                        raise EpisodeValidationError(
                            f"G1 {key} image cannot be decoded"
                        ) from exc
        media[camera_id] = CameraFrames(key, camera["width"], camera["height"], offsets)
        selected_times = np.asarray(times, dtype=np.int64)[ci]
        arrays[f"source.{camera_id}_monotonic_ns"] = selected_times
        arrays[f"source.{camera_id}_age_ms"] = ((grid - selected_times) / 1e6).astype(
            np.float32
        )
    warnings = []
    measured_hands = arrays["observation.state"][:, 29:]
    if not measured_hands.any():
        warnings.append(
            "All measured BrainCo hand values are zero; preserved unchanged, feedback requires review"
        )
    for label, values in (("measured hands", measured_hands), ("hand targets", hands)):
        if ((values < 0) | (values > 1)).any():
            warnings.append(
                f"{label} contain values outside nominal [0,1]; preserved without clipping"
            )
    report = {
        "schema": SCHEMA,
        "fps": profile["output_fps"],
        "frames": len(grid),
        "method": "latest-not-future; no interpolation; pause time removed",
        "streams": {
            "observation.state": {"dimensions": [41]},
            "action": {"dimensions": [76]},
            **{
                f"observation.images.{k}": {"dimensions": [3, v.height, v.width]}
                for k, v in media.items()
            },
        },
        "errors": [],
        "warnings": warnings,
        "raw_sha256": expected,
        "source_episode_index": episode.get("episode_index"),
        "source_start_monotonic_ns": int(grid[0]),
        "source_end_monotonic_ns": int(grid[-1]),
        "robot_max_age_ms": float(arrays["source.robot_age_ms"].max()),
        "camera_timestamp_fallbacks": fallbacks,
        "pre_start_control_lead_ms": pre_start_control_lead_ms,
        "recorded_config": {
            k: robot_config[k]
            for k in ("hand_type", "hand_num_motors", "hand_units", "model_path")
        },
    }
    return RecordingIndex(path, episode, robot_config, arrays, media, report)
