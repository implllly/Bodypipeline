import {
  getDatasetVersionAndInfo,
  buildVersionedUrl,
} from "@/utils/versionUtils";
import { authHeaders } from "@/utils/auth";
import { iterateEpisodeMetadataFilesV3 } from "@/app/[org]/[dataset]/[episode]/fetch-data";
import {
  batchCatalog,
  type BatchInfo,
  type BatchCatalog,
} from "./batch-preview-model";

export async function loadBatchCatalog(repoId: string): Promise<BatchCatalog> {
  const { version, info } = await getDatasetVersionAndInfo(repoId);
  const rows: Record<string, unknown>[] = [];
  if (version === "v3.0") {
    for await (const file of iterateEpisodeMetadataFilesV3(repoId, version))
      rows.push(...file);
  } else {
    const response = await fetch(
      buildVersionedUrl(repoId, version, "meta/episodes.jsonl"),
      {
        cache: "no-store",
        headers: authHeaders(),
      },
    );
    if (!response.ok)
      throw new Error(`Episode 元数据读取失败（${response.status}）`);
    for (const line of (await response.text()).split("\n")) {
      if (line.trim()) rows.push(JSON.parse(line));
    }
  }
  return batchCatalog(
    repoId,
    version,
    info as unknown as BatchInfo,
    rows,
    (path) => buildVersionedUrl(repoId, version, path),
  );
}
