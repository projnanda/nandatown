import type { Dataset } from "./hackathon-types";

const TTL_MS = 10 * 60_000;

export class PRFetchError extends Error {
  constructor(message: string, readonly requests: number, readonly retryAfterMs = 0) {
    super(message);
  }
}

/** One complete snapshot for Railway's long-running Node process. */
export function createDatasetCache(
  load: () => Promise<Dataset>,
  { now = Date.now, authenticated = false }: { now?: () => number; authenticated?: boolean } = {},
) {
  let snapshot: Dataset | undefined;
  let inFlight: Promise<Dataset> | undefined;
  let generation = 0;
  let loadedGeneration = -1;
  let expiresAt = 0;
  let nextAttemptAt = 0;
  let lastError: unknown;

  function refreshFloor(data?: Dataset, attemptedPages = 0) {
    // Exact multiples need a final empty page. Reserve 20% of GitHub's
    // request allowance, including for failures and other service traffic.
    const pages = Math.max(attemptedPages, Math.floor((data?.stats.total_submissions ?? 0) / 100) + 1);
    const requestsPerHour = authenticated ? 4000 : 48;
    return Math.max(authenticated ? 5_000 : 300_000, Math.ceil(pages * 3_600_000 / requestsPerHour));
  }

  function get(): Promise<Dataset> {
    if (inFlight) return inFlight;
    const startedAt = now();
    if (snapshot && generation === loadedGeneration && startedAt < expiresAt) {
      return Promise.resolve(snapshot);
    }
    if (startedAt < nextAttemptAt) {
      return snapshot ? Promise.resolve(snapshot) : Promise.reject(lastError);
    }
    const fetchingGeneration = generation;
    nextAttemptAt = startedAt + refreshFloor(snapshot);
    inFlight = (async () => {
      try {
        const fresh = await load();
        snapshot = fresh;
        loadedGeneration = fetchingGeneration;
        expiresAt = now() + TTL_MS;
        nextAttemptAt = Math.max(nextAttemptAt, startedAt + refreshFloor(fresh));
        return fresh;
      } catch (error) {
        lastError = error;
        if (error instanceof PRFetchError) {
          nextAttemptAt = Math.max(nextAttemptAt, startedAt + refreshFloor(snapshot, error.requests), now() + error.retryAfterMs);
        }
        // Never replace a complete history with an empty or partial refresh.
        if (snapshot) return snapshot;
        throw error;
      }
    })().finally(() => { inFlight = undefined; });
    return inFlight;
  }

  return { get, invalidate: () => { generation++; } };
}
