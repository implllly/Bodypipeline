// Only catalog IO and the unrelated theme control are mocked. The flag context,
// storage, request lifecycle and batch wall are the production components.
export function ThemeToggle() {
  return null;
}
export async function loadBatchCatalog(repoId: string) {
  return {
    repoId,
    cameras: ["main"],
    episodes: [0, 1, 2, 3],
    revision: "original",
    clips: { main: {} },
  };
}
