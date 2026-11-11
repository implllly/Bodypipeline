"""Install the visual manual episode-trim panel into the pinned Viewer.

The Viewer is patched at startup from a pinned upstream checkout. The manual
trim panel belongs to the default Episode tab, next to the video playback bar,
so opening a registered preview shows it without switching tabs. This patch is
idempotent across restarts and also moves the panel out of the annotation tab
layout used by the first deployment.
"""

from pathlib import Path

MARKER = "// bodypipeline: manual episode trim v1"
COMPONENT = "src/components/manual-trim.tsx"
VIEWER = "src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
IMPORT_ANCHOR = 'import { TaskAnnotationStudio } from "@/components/task-annotation-studio";'
LEGACY_PANEL = "              <ManualTrimPanel />\n              <TaskAnnotationStudio />"
EPISODE_ANCHOR = (
    "              <PlaybackBar />\n"
    "            </>\n"
    "          )}\n"
    "\n"
    '          {activeTab === "annotations" && ('
)
EPISODE_REPLACEMENT = (
    "              <PlaybackBar />\n"
    "              <ManualTrimPanel />\n"
    "            </>\n"
    "          )}\n"
    "\n"
    '          {activeTab === "annotations" && ('
)


def patch_manual_trim(cache: Path):
    source = cache / "source"
    page = source / VIEWER
    text = page.read_text()
    if MARKER not in text:
        if text.count(IMPORT_ANCHOR) != 1:
            raise RuntimeError(
                "Unexpected Visualizer source; cannot install manual episode trim"
            )
        text = text.replace(
            IMPORT_ANCHOR,
            IMPORT_ANCHOR
            + "\n"
            + MARKER
            + '\nimport { ManualTrimPanel } from "@/components/manual-trim";',
        )
    if text.count(LEGACY_PANEL) == 1:
        text = text.replace(LEGACY_PANEL, "              <TaskAnnotationStudio />")
    if EPISODE_REPLACEMENT not in text:
        if text.count(EPISODE_ANCHOR) != 1:
            raise RuntimeError(
                "Unexpected Visualizer source; cannot place manual episode trim"
            )
        text = text.replace(EPISODE_ANCHOR, EPISODE_REPLACEMENT)
    page.write_text(text)
