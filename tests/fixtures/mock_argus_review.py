"""Offline Argus-shaped fixture. Produces synthetic replies; never calls a model."""

import argparse
import json
import os
from pathlib import Path


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args, _ = parser.parse_known_args()
    spec = json.loads(Path(os.environ["PIPELINE_ARGUS_TASK_TEMPLATE"]).read_text())
    template = spec["template"]
    info = json.loads((args.data / "meta/info.json").read_text())
    episodes_file = args.data / "meta/episodes.jsonl"
    episodes = (
        [
            json.loads(line)
            for line in episodes_file.read_text().splitlines()
            if line.strip()
        ]
        if episodes_file.exists()
        else [
            {
                "episode_index": i,
                "length": info["total_frames"] // info["total_episodes"],
            }
            for i in range(info["total_episodes"])
        ]
    )

    def values(scope, failed=False):
        result = {}
        for f in template["fields"]:
            if f["scope"] != scope:
                continue
            kind = f["kind"]
            result[f["id"]] = (
                (2 if failed else 5)
                if kind == "rating"
                else (not failed)
                if kind == "bool"
                else 1
                if kind == "number"
                else (f["options"][0] if failed else f["options"][-1])
                if kind == "single"
                else f["options"][:1]
                if kind == "multi"
                else "模拟 AI 初稿，待人工确认"
            )
        return result

    print("== label MOCK: offline template fixture, no model requests", flush=True)
    for ep in episodes:
        index, count = ep["episode_index"], ep["length"]
        name = f"episode_{index:06d}"
        duration = count / info["fps"]
        attempts = []
        for j, (a, b) in enumerate([(0, 0.25), (0.25, 0.5), (0.5, 0.75)]):
            if not template["subtasks"]:
                break
            step = template["subtasks"][min(j // 2, len(template["subtasks"]) - 1)]
            attempts.append(
                {
                    "subtask_id": step["id"],
                    "start_s": duration * a,
                    "end_s": duration * b,
                    "values": values("attempt", failed=j == 0),
                }
            )
        raw = {
            "parse_ok": True,
            "labels": {
                "task_summary": "MOCK 模板标注",
                "completion": {"task_completed": "success"},
                "bodypipeline_task": {
                    "template_id": template["id"],
                    "template_signature": spec["signature"],
                    "values": values("episode"),
                    "attempts": attempts,
                    "null_segments": [
                        {
                            "start_s": duration * 0.8,
                            "end_s": duration * 0.9,
                            "note": "模拟背景片段",
                        }
                    ],
                    "frames": [{"t_s": 0, "values": values("frame")}],
                },
            },
            "usage": {"est_cost_usd": 0},
            "config": {},
            "episode_dir": name,
        }
        write(
            args.out / "episodes" / name / "context.json",
            {
                "episode_id": name,
                "episode_index": index,
                "fps": info["fps"],
                "n_state_frames": count,
            },
        )
        write(args.out / "run/out" / f"{name}.json", raw)
        write(
            args.out / "qa" / f"{name}.json",
            {
                "episode_prompt": "MOCK 模板标注",
                "duration_s": duration,
                "completion": {"task_completed": "success"},
                "_meta": {"episode_id": name, "model": "mock/template-demo"},
            },
        )
        videos = sorted((args.data / "videos").rglob(f"{name}.mp4"))
        if videos:
            clip = args.out / "clips" / f"{name}.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            if not clip.exists():
                clip.symlink_to(videos[0])
    write(
        args.out / "run/run.json",
        {
            "status": "done",
            "cost_usd": 0,
            "reserved_cost_usd": 0,
            "cost_complete": True,
        },
    )
    print("== board MOCK done", flush=True)


if __name__ == "__main__":
    main()
