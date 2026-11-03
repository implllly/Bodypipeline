import { expect, test } from "bun:test";
import { reviewRequest } from "../pipeline/server/viewer_ui/annotation-review-client";

test("opening a dataset retries a transient read lock then loads its template document", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    return calls === 1
      ? Response.json(
          { detail: "该数据集正在处理，请稍后重试" },
          { status: 409 },
        )
      : Response.json({ task_document: { templateId: "cup" } });
  }) as typeof fetch;
  try {
    const result = await reviewRequest<{
      task_document: { templateId: string };
    }>("/api/subtask-review/dataset/episodes/0");
    expect(result.task_document.templateId).toBe("cup");
    expect(calls).toBe(2);
  } finally {
    globalThis.fetch = original;
  }
});

test("saving a conflicting draft is never automatically retried", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    return Response.json({ detail: "草稿版本已变化" }, { status: 409 });
  }) as typeof fetch;
  try {
    await expect(
      reviewRequest("/api/subtask-review/dataset/episodes/0", { revision: 1 }),
    ).rejects.toThrow("草稿版本已变化");
    expect(calls).toBe(1);
  } finally {
    globalThis.fetch = original;
  }
});
