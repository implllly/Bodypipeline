"""Subscription-only live quad preview. No publishers, service clients or SDK."""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path


def main():
    import av
    import cv2
    import rclpy
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from sensor_msgs.msg import CompressedImage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--topic", default="/quad_tile/compressed_undistorted")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    codec = av.CodecContext.create("h264", "r")
    codec.thread_count = 2
    pending, lock, stamps = [], threading.Lock(), OrderedDict()
    stopping = threading.Event()
    rclpy.init()
    node = rclpy.create_node("bodypipeline_tianji_preview")

    def receive(msg):
        try:
            if "h264" not in msg.format.lower():
                raise ValueError("Expected the verified H.264 quad topic")
            ns = msg.header.stamp.sec * 10**9 + msg.header.stamp.nanosec
            stamps[ns] = time.time()
            while len(stamps) > 128:
                stamps.popitem(last=False)
            packet = av.Packet(bytes(msg.data))
            packet.pts = packet.dts = ns
            packet.time_base = Fraction(1, 10**9)
            for frame in codec.decode(packet):
                if frame.is_corrupt or frame.pts not in stamps:
                    continue
                with lock:
                    pending[:] = [(frame, stamps[frame.pts], frame.pts / 1e9)]
        except (ValueError, av.error.FFmpegError) as exc:
            node.get_logger().warning(str(exc))

    node.create_subscription(
        CompressedImage,
        args.topic,
        receive,
        QoSProfile(
            depth=32,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        ),
    )

    def render():
        sequence = 0
        cameras = (
            ("head_left", 0, 0),
            ("head_right", 0, 1),
            ("left_wrist", 1, 0),
            ("right_wrist", 1, 1),
        )
        while not stopping.wait(0.15):
            with lock:
                value = pending.pop() if pending else None
            if value is None:
                continue
            frame, received, stamp = value
            if not 0 <= time.time() - received <= 0.5:
                continue
            try:
                rgb = frame.to_ndarray(format="rgb24")
                height, width = rgb.shape[:2]
                if rgb.ndim != 3 or rgb.shape[2] != 3 or height % 2 or width % 2:
                    raise ValueError("Unexpected quad dimensions")
                half_h, half_w = height // 2, width // 2
                files = {}
                # Two slots plus atomic metadata prevent partially written JPEGs.
                for name, row, col in cameras:
                    tile = rgb[
                        row * half_h : (row + 1) * half_h,
                        col * half_w : (col + 1) * half_w,
                    ]
                    ok, payload = cv2.imencode(
                        ".jpg",
                        cv2.cvtColor(tile, cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 85],
                    )
                    if not ok:
                        raise ValueError("JPEG encoder failed")
                    filename = f"{name}-{sequence % 2}.jpg"
                    temp = args.root / (filename + ".tmp")
                    temp.write_bytes(payload.tobytes())
                    os.replace(temp, args.root / filename)
                    files[name] = filename
                temp = args.root / "latest.tmp"
                temp.write_text(
                    json.dumps(
                        dict(
                            files=files,
                            source_time=stamp,
                            received_time=received,
                            tile_width=half_w,
                            tile_height=half_h,
                        )
                    )
                )
                os.replace(temp, args.root / "latest.json")
                sequence += 1
            except Exception as exc:
                node.get_logger().error(str(exc))

    worker = threading.Thread(target=render, daemon=True)
    worker.start()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        worker.join(timeout=3)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
