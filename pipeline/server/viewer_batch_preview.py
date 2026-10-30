"""Install the batch video wall into the pinned Viewer, including cache upgrades."""

from pathlib import Path

ASSETS = Path(__file__).with_name("viewer_ui")


def patch_batch_preview(cache: Path):
    source = cache / "source"
    destinations = {
        "batch-preview.tsx": "src/components/batch-preview.tsx",
        "batch-preview-model.ts": "src/utils/batch-preview-model.ts",
        "batch-preview-data.ts": "src/utils/batch-preview-data.ts",
        "batch-preview.css": "src/app/batch-preview.css",
        "review-flags-context.tsx": "src/context/flagged-episodes-context.tsx",
    }
    for name, target in destinations.items():
        (source / target).write_bytes((ASSETS / name).read_bytes())
    page = source / "src/app/[org]/[dataset]/batch/page.tsx"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("""import BatchPreview from "@/components/batch-preview";
export const dynamic = "force-dynamic";
export default async function BatchPage({params}: {
  params: Promise<{org: string; dataset: string}>;
}) {
  const {org, dataset} = await params;
  return <BatchPreview repoId={`${org}/${dataset}`} />;
}
""")
    layout = source / "src/app/layout.tsx"
    text = layout.read_text()
    line = 'import "./batch-preview.css";\n'
    text = text.replace(line, "")
    anchor = 'import "./task-annotation.css";\n'
    layout.write_text(text.replace(anchor, anchor + line, 1))
    fetch_data = source / "src/app/[org]/[dataset]/[episode]/fetch-data.ts"
    text = fetch_data.read_text()
    anchor = "async function* iterateEpisodeMetadataFilesV3("
    if text.count(anchor) != 1:
        raise RuntimeError("Unexpected Viewer episode metadata reader")
    if "export " + anchor not in text:
        fetch_data.write_text(text.replace(anchor, "export " + anchor))
    viewer = source / "src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    text = viewer.read_text()
    anchor = "<FlaggedEpisodesProvider>"
    replacement = "<FlaggedEpisodesProvider repoId={`${org}/${dataset}`}>"
    if anchor not in text and replacement not in text:
        raise RuntimeError("Unexpected Viewer flagged episode provider")
    viewer.write_text(text.replace(anchor, replacement))
