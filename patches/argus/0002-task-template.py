"""Add a template contract and its content identity to pinned Argus requests."""

import shutil
from pathlib import Path

MARKER = "# pipeline-argus-task-template:v1"
RETRY_MARKER = "# pipeline-argus-template-retry:v1"


def apply(root: Path):
    episode = root / "label/episode.py"
    harness = root / "label/harness.py"
    text = episode.read_text()
    if MARKER not in text:
        anchor = "    return fixed, episode_text(ep, pl, cell_w, cell_h, native, blocks) + jaws.prompt(ep.get('jaw_evidence') or {})"
        if text.count(anchor) != 1:
            raise RuntimeError(
                "Unexpected Argus build_prompt; task template patch refused"
            )
        text = text.replace(
            anchor,
            "    " + MARKER + "\n"
            "    from label.pipeline_template import task_contract\n"
            "    fixed += task_contract(ctx)\n" + anchor,
        )
        episode.write_text(text)
    text = harness.read_text()
    if MARKER not in text:
        anchor = '    return {"version": 2, "source_proof": source_proof(ctx, ep_dir), "settings": settings}'
        if text.count(anchor) != 1:
            raise RuntimeError(
                "Unexpected Argus input_identity; task template patch refused"
            )
        text = text.replace(
            anchor,
            "    " + MARKER + "\n"
            "    from label.pipeline_template import template_identity\n"
            "    identity = template_identity()\n"
            "    if identity is not None:\n"
            "        settings['bodypipeline_task_template'] = identity\n" + anchor,
        )
        harness.write_text(text)
    if RETRY_MARKER not in text:
        anchor = "    if reply_truncated(record):\n"
        if text.count(anchor) != 1:
            raise RuntimeError(
                "Unexpected Argus annotation_problems; template retry patch refused"
            )
        text = text.replace(
            anchor,
            "    " + RETRY_MARKER + "\n"
            "    from label.pipeline_template import validation_problems\n"
            "    problems = validation_problems(record)\n"
            "    if problems:\n"
            "        return problems\n" + anchor,
        )
        harness.write_text(text)
    helper = (
        Path(__file__).resolve().parents[2]
        / "pipeline/server/argus_template_contract.py"
    )
    shutil.copyfile(helper, root / "label/pipeline_template.py")


if __name__ == "__main__":
    import sys

    apply(Path(sys.argv[1]))
