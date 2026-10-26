"""Generate clearly labelled synthetic wire-format MCAP; never uses ROS/hardware.

Run using the native provider's Python: python tests/fixtures/tianji_mcap.py OUT
"""

import json
import sys
from fractions import Fraction
from pathlib import Path


def generate(root):
    import av
    import numpy as np
    import yaml
    from mcap_ros2.writer import Writer
    from tianji_provider.reader import JOINT_NAMES, TYPES, VIDEO_TOPIC

    root.mkdir(parents=True, exist_ok=False)
    (root / "SYNTHETIC.txt").write_text(
        "Generated test data, NOT a real robot recording.\n"
    )
    header = "std_msgs/Header header\n"
    dependencies = (
        "\n"
        + "=" * 80
        + "\nMSG: std_msgs/Header\nbuiltin_interfaces/Time stamp\nstring frame_id\n"
        + "=" * 80
        + "\nMSG: builtin_interfaces/Time\nint32 sec\nuint32 nanosec\n"
    )
    schemas = {
        "sensor_msgs/msg/JointState": header
        + "string[] name\nfloat64[] position\n"
        + dependencies,
        "marvin_msgs/msg/JointcmdArm": header + "float64[] positions\n" + dependencies,
        "std_msgs/msg/Float32MultiArray": "float32[] data\n",
        "std_msgs/msg/Float32": "float32 data\n",
        "std_msgs/msg/String": "string data\n",
        "geometry_msgs/msg/PoseStamped": header
        + "geometry_msgs/Pose pose\n"
        + dependencies
        + "=" * 80
        + "\nMSG: geometry_msgs/Pose\ngeometry_msgs/Point position\ngeometry_msgs/Quaternion orientation\n"
        + "=" * 80
        + "\nMSG: geometry_msgs/Point\nfloat64 x\nfloat64 y\nfloat64 z\n"
        + "=" * 80
        + "\nMSG: geometry_msgs/Quaternion\nfloat64 x\nfloat64 y\nfloat64 z\nfloat64 w\n",
        "geometry_msgs/msg/WrenchStamped": header
        + "geometry_msgs/Wrench wrench\n"
        + dependencies
        + "=" * 80
        + "\nMSG: geometry_msgs/Wrench\ngeometry_msgs/Vector3 force\ngeometry_msgs/Vector3 torque\n"
        + "=" * 80
        + "\nMSG: geometry_msgs/Vector3\nfloat64 x\nfloat64 y\nfloat64 z\n",
        "sensor_msgs/msg/CompressedImage": header
        + "string format\nuint8[] data\n"
        + dependencies,
    }
    t0 = 1_800_000_000_000_000_000

    def hdr(ns, frame="base_link"):
        return dict(stamp=dict(sec=ns // 10**9, nanosec=ns % 10**9), frame_id=frame)

    def vec(x=0.0, y=0.0, z=0.0):
        return dict(x=x, y=y, z=z)

    for part in ("data", "video"):
        folder = root / part
        folder.mkdir()
        with Writer(str(folder / "bag_0.mcap")) as writer:
            registered = {k: writer.register_msgdef(k, v) for k, v in schemas.items()}
            if part == "data":
                for i in range(241):
                    ns = t0 + i * 5_000_000
                    for topic, typename in TYPES.items():
                        phase = i / 240 * 6.28
                        state = 0.1 * np.sin(phase)
                        if topic == "/info/web_gripper":
                            value = dict(
                                data=json.dumps(
                                    dict(
                                        calibration=[[0, 1.6], [0, 1.6]],
                                        position_unit="motor_radian",
                                        target_unit="normalized_0_1",
                                    )
                                )
                            )
                        elif topic == "/tj/joint_states":
                            value = dict(
                                header=hdr(ns),
                                name=list(JOINT_NAMES),
                                position=[state] * 14,
                            )
                        elif "joint_cmd" in topic:
                            value = dict(header=hdr(ns), positions=[state + 0.05] * 7)
                        elif "gripper_feedback" in topic:
                            value = dict(data=[0.64, 0, 0, 0, 0])
                        elif topic.startswith("/control/gripperValue"):
                            value = dict(data=0.6)
                        elif "eef" in topic:
                            value = dict(
                                header=hdr(ns),
                                pose=dict(
                                    position=vec(x=state),
                                    orientation=dict(x=0.0, y=0.0, z=0.0, w=1.0),
                                ),
                            )
                        else:
                            value = dict(
                                header=hdr(
                                    ns,
                                    "flange_L"
                                    if topic.endswith("left")
                                    else "flange_R",
                                ),
                                wrench=dict(force=vec(x=state), torque=vec()),
                            )
                        writer.write_message(
                            topic,
                            registered[typename],
                            value,
                            log_time=ns,
                            publish_time=ns,
                        )
            else:
                encoder = av.CodecContext.create("libx264", "w")
                encoder.width, encoder.height, encoder.pix_fmt = 64, 48, "yuv420p"
                encoder.time_base, encoder.framerate = Fraction(1, 50), Fraction(50, 1)
                encoder.options = dict(
                    preset="ultrafast", tune="zerolatency", crf="18", g="1"
                )
                for i in range(61):
                    rgb = np.zeros((48, 64, 3), dtype=np.uint8)
                    for j, (row, col) in enumerate([(0, 0), (0, 1), (1, 0), (1, 1)]):
                        rgb[row * 24 : (row + 1) * 24, col * 32 : (col + 1) * 32] = [
                            (i * 3 + j * 30) % 255,
                            60 + j * 40,
                            200 - j * 30,
                        ]
                    image = av.VideoFrame.from_ndarray(rgb, format="rgb24")
                    image.pts = i
                    packets = encoder.encode(image)
                    if len(packets) != 1:
                        raise ValueError("Fixture expects one access unit per frame")
                    ns = t0 + i * 20_000_000
                    writer.write_message(
                        VIDEO_TOPIC,
                        registered["sensor_msgs/msg/CompressedImage"],
                        dict(header=hdr(ns), format="h264", data=bytes(packets[0])),
                        log_time=ns,
                        publish_time=ns,
                    )
                if encoder.encode(None):
                    raise ValueError("Unexpected delayed fixture frame")
        (folder / "metadata.yaml").write_text(
            yaml.safe_dump(
                dict(
                    rosbag2_bagfile_information=dict(relative_file_paths=["bag_0.mcap"])
                )
            )
        )
    return root


if __name__ == "__main__":
    print(generate(Path(sys.argv[1]).resolve()))
