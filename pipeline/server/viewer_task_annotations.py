"""Install the task studio and shared server draft into the pinned Viewer."""

import re
from pathlib import Path

MARKER = "// bodypipeline: browser task annotation studio v1"
PLAYER_MARKER = "// bodypipeline: precise annotation video seeks v1"


def patch_task_review(source: Path):
    """Expose the single server draft to the studio; keep VQA in its panel."""
    from .viewer_annotations import _replace

    context = source / "src/context/annotations-context.tsx"
    _replace(
        context,
        'import type { LanguageAtom } from "../types/language.types";',
        'import type { LanguageAtom } from "../types/language.types";\nimport type { TaskDocument } from "../utils/task-annotation-model";',
    )
    _replace(
        context,
        "  atoms: LanguageAtom[];",
        """  taskDocument: TaskDocument | null;
  updateTaskDocument: React.Dispatch<React.SetStateAction<TaskDocument | null>>;
  reviewRevision: number;
  reviewHasDraft: boolean;
  reviewExporting: boolean;
  atoms: LanguageAtom[];""",
    )
    # Insert outside the v1 bridge's context so it remains repeatable on restart.
    _replace(
        context,
        "      atoms,\n",
        """      taskDocument: managed ? serverReview.taskDocument : null,
      updateTaskDocument: serverReview.setTaskDocument,
      reviewRevision: serverReview.review?.revision || 0,
      reviewHasDraft: !!serverReview.review?.has_draft,
      reviewExporting: serverReview.exporting,
      atoms,
""",
    )
    panel = source / "src/components/annotations-panel.tsx"
    _replace(
        panel,
        'useState<QuickAddKind>("subtask")',
        'useState<QuickAddKind>(ident.repoId?.startsWith("bp/") ? "plan" : "subtask")',
    )
    _replace(
        panel,
        "{QUICK_ADD_DEFS.map((d) => (",
        '{QUICK_ADD_DEFS.filter(d => !ident.repoId?.startsWith("bp/") || d.kind !== "subtask").map((d) => (',
    )
    page = source / "src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    _replace(
        page,
        "                <AnnotationsTimeline duration={data.duration} />",
        '                {!datasetInfo.repoId.startsWith("bp/") && <AnnotationsTimeline duration={data.duration} />}',
    )


def patch_annotation_preview_layout(text: str) -> str:
    """Pin only the annotation preview; other Viewer tabs keep their layout."""
    start = text.find('{activeTab === "annotations" && (')
    end = text.find('{activeTab === "statistics" && (', start)
    if start < 0 or end < 0:
        raise RuntimeError("Unexpected Visualizer annotation tab layout")
    block = text[start:end]
    if 'className="ta-preview-dock"' in block:
        return text
    root = 'className="annotations-skin flex flex-col gap-4"'
    video = "              {videosInfo.length > 0 && ("
    playback = "              <PlaybackBar />"
    if block.count(root) != 1 or block.count(video) != 1 or block.count(playback) != 1:
        raise RuntimeError(
            "Unexpected Visualizer preview; cannot pin annotation videos"
        )
    block = block.replace(
        root, 'className="annotations-skin ta-annotation-page flex flex-col gap-4"'
    )
    block = block.replace(
        video, '              <div className="ta-preview-dock">\n' + video, 1
    )
    block = block.replace(playback, playback + "\n              </div>", 1)
    return text[:start] + block + text[end:]


def patch_annotation_player(source: Path):
    """User seeks must be frame-precise and cannot loop a paused preview."""
    path = source / "src/components/simple-videos-player.tsx"
    text = path.read_text()
    time_binding = "  const { currentTime, seek, externalSeekVersion, isPlaying, setIsPlaying } =\n    useTime();"
    duration_binding = time_binding + "\n  const { setDuration } = useTime();"
    if PLAYER_MARKER in text:
        # Older local previews added the setter to the base patch's context.
        # Keep that context byte-for-byte so the next bootstrap still applies.
        normalized = text.replace(
            "  const { currentTime, seek, externalSeekVersion, isPlaying, setIsPlaying, setDuration } =\n    useTime();",
            duration_binding,
        )
        if normalized != text:
            path.write_text(normalized)
        return
    replacements = {
        time_binding: duration_binding,
        'import { useTime } from "../context/time-context";': 'import { useTime } from "../context/time-context";\n'
        + PLAYER_MARKER
        + '\nimport { externalSeekNeeded } from "../utils/task-annotation-model";',
        "  const lastSeekVersionRef = useRef(externalSeekVersion);": "  const lastSeekVersionRef = useRef(externalSeekVersion);\n"
        "  const playbackActiveRef = useRef(isPlaying);\n"
        "  playbackActiveRef.current = isPlaying;",
        "      setVideosReady(true);\n      onVideosReadyRef.current?.();": "      setVideosReady(true);\n"
        "      const primaryIndex = firstVisibleIdxRef.current;\n"
        "      const primary = videoRefs.current[primaryIndex];\n"
        "      const primaryInfo = videosInfo[primaryIndex];\n"
        "      if (primary && primaryInfo) {\n"
        "        const mediaDuration = primaryInfo.isSegmented\n"
        "          ? (primaryInfo.segmentEnd ?? primary.duration) - (primaryInfo.segmentStart ?? 0)\n"
        "          : primary.duration;\n"
        "        if (Number.isFinite(mediaDuration) && mediaDuration > 0) setDuration(mediaDuration);\n"
        "      }\n"
        "      onVideosReadyRef.current?.();",
        "      const handleTimeUpdate = () => {\n        if (info.isSegmented) {": "      const handleTimeUpdate = () => {\n"
        "        if (video.seeking) return;\n"
        "        if (info.isSegmented && playbackActiveRef.current) {",
        "        : () => {\n            // Same coordinated loop strategy": "        : () => {\n"
        "            if (!playbackActiveRef.current) return;\n"
        "            // Same coordinated loop strategy",
        """      if (
        Math.abs(video.currentTime - targetTime) >
        THRESHOLDS.VIDEO_SYNC_TOLERANCE
      ) {""": "      if (externalSeekNeeded(video.currentTime, targetTime)) {",
        "  }, [videosInfo, setIsPlaying, seek]);": "  }, [videosInfo, setIsPlaying, setDuration, seek]);",
    }
    for old in replacements:
        if text.count(old) != 1:
            raise RuntimeError(
                "Unexpected Visualizer player; cannot apply precise seeking"
            )
    for old, new in replacements.items():
        text = text.replace(old, new, 1)
    path.write_text(text)


def patch_task_annotations(cache: Path):
    source = cache / "source"
    assets = Path(__file__).with_name("viewer_ui")
    for name, destination in {
        "task-annotation-model.ts": "src/utils/task-annotation-model.ts",
        "task-annotation-studio.tsx": "src/components/task-annotation-studio.tsx",
        "task-annotation-timeline.tsx": "src/components/task-annotation-timeline.tsx",
        "task-annotation-menu.tsx": "src/components/task-annotation-menu.tsx",
        "task-review-client.ts": "src/utils/task-review-client.ts",
        "task-annotation.css": "src/app/task-annotation.css",
    }.items():
        (source / destination).write_bytes((assets / name).read_bytes())
    layout = source / "src/app/layout.tsx"
    text = layout.read_text()
    css_import = 'import "./task-annotation.css";\n'
    # Keep the base presentation patch's import context intact on restart.
    if not text.startswith(css_import):
        text = css_import + text.replace(css_import, "")
        layout.write_text(text)
    viewer = source / "src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    text = viewer.read_text()
    if MARKER in text:
        viewer.write_text(patch_annotation_preview_layout(text))
        patch_annotation_player(source)
        patch_task_review(source)
        return
    anchor = 'import { AnnotationsPanel } from "@/components/annotations-panel";'
    old = """              <AnnotationsTimeline duration={data.duration} />
              <AnnotationsPanel
                cameraKeys={videosInfo.map((v) => v.filename)}
              />"""
    if text.count(anchor) != 1 or text.count(old) != 1:
        raise RuntimeError(
            "Unexpected Visualizer source; cannot install task annotations"
        )
    text = text.replace(
        anchor,
        anchor
        + "\n"
        + MARKER
        + '\nimport { TaskAnnotationStudio } from "@/components/task-annotation-studio";',
    )
    text = text.replace(
        old,
        """              <TaskAnnotationStudio />
              <details className="ta-legacy">
                <summary>更多标注：语言、计划与 VQA</summary>
                <AnnotationsTimeline duration={data.duration} />
                <AnnotationsPanel cameraKeys={videosInfo.map((v) => v.filename)} />
              </details>""",
    )
    text = re.sub(
        r'              <div className="grounding-intro">.*?</div>\n',
        "",
        text,
        count=1,
        flags=re.DOTALL,
    )
    viewer.write_text(patch_annotation_preview_layout(text))
    patch_annotation_player(source)
    patch_task_review(source)
