import React from "react";
import { createRoot } from "react-dom/client";
import BatchPreview from "../../pipeline/server/viewer_ui/batch-preview";
import {
  FlaggedEpisodesProvider,
  useFlaggedEpisodes,
} from "../../pipeline/server/viewer_ui/review-flags-context";

function Probe() {
  const flags = useFlaggedEpisodes();
  return (
    <div>
      <output id="flags">{JSON.stringify([...flags.flagged])}</output>
      {[0, 1, 2, 3].map((id) => (
        <button key={id} onClick={() => flags.toggle(id)}>
          toggle {id}
        </button>
      ))}
      <button
        onClick={() =>
          flags.remap({ cleanup_generation: 0, cleanup_history: [] })
        }
      >
        initialize
      </button>
      <button
        onClick={() =>
          flags.remap({
            cleanup_generation: 2,
            cleanup_history: [
              { id: "A", from: 0, to: 1, mapping: { 1: 0, 2: 1, 3: 2, 4: 3 } },
              { id: "B", from: 1, to: 2, mapping: { 1: 0, 2: 1, 3: 2 } },
            ],
          })
        }
      >
        catch up
      </button>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  new URLSearchParams(location.search).has("flags") ? (
    <FlaggedEpisodesProvider repoId="bp/test">
      <Probe />
    </FlaggedEpisodesProvider>
  ) : (
    <BatchPreview repoId="bp/test" />
  ),
);
