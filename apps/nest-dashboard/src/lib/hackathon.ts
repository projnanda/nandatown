/**
 * Server-only data loader for the homepage and /prgallery routes.
 *
 * Since 2026-07-08 this pulls LIVE from the GitHub API (see
 * `./hackathon-github`) instead of the deploy-time
 * `public/hackathon-data.json` snapshot, so merged PRs show up in their
 * layer automatically. Freshness comes from two directions:
 *
 * 1. The shared snapshot refreshes on demand every ten minutes, within
 *    GitHub's API budget (see hackathon-github.ts).
 * 2. Signed GitHub webhooks mark it dirty; the next eligible request
 *    refreshes it without losing updates received during a fetch.
 *
 * This module is not safe to import from client components — the public
 * surface is the types and helpers in `./hackathon-types`.
 */

import { EMPTY_DATASET, type Dataset } from "./hackathon-types";
import { fetchLiveDataset, layerStatsFor } from "./hackathon-github";

// Re-export the runtime API consumers actually use from server pages,
// so call-sites can keep importing from a single module.
export * from "./hackathon-types";

/**
 * Load the live dataset. Never throws: if GitHub is unreachable and
 * nothing is cached, we return an empty dataset and the UI shows its
 * graceful error state — same shape, zero rows. The process cache
 * retains its last complete snapshot on failed refreshes.
 */
export async function loadDataset(): Promise<Dataset> {
  try {
    return await fetchLiveDataset();
  } catch {
    // Keep the 13 layer pages alive (zero counts, "open" affordances)
    // rather than 404ing them while GitHub is unreachable.
    return { ...EMPTY_DATASET, layers: layerStatsFor([]) };
  }
}

export const DATA_REVALIDATE_SECONDS = 600;
