import assert from "node:assert/strict";
import { test } from "node:test";
import { buildDataset, classifyLayer, fetchAllPRs, type GitHubPR } from "../src/lib/hackathon-github";
import { compareSubmissionStatus } from "../src/lib/hackathon-types";
import { createDatasetCache } from "../src/lib/pr-cache";

function pr(number: number, overrides: Partial<GitHubPR> = {}): GitHubPR {
  return {
    number, state: "open", title: "A protocol contribution", body: "",
    html_url: `https://github.com/projnanda/nandatown/pull/${number}`,
    diff_url: "", created_at: "2026-11-04T00:00:00Z", merged_at: null,
    head: { ref: "main" }, user: null, ...overrides,
  };
}

test("lifetime totals include closed unmerged PRs and every PR belongs to one layer", () => {
  const data = buildDataset([
    pr(1, { title: "transport: reliable delivery" }),
    pr(2, { state: "closed", merged_at: "2026-11-04T01:00:00Z", title: "identity: signing" }),
    pr(3, { state: "closed", title: "docs: guide" }),
  ]);
  assert.equal(data.stats.total_submissions, 3);
  assert.equal(data.stats.total_merged, 1);
  assert.equal(data.stats.total_open, 1);
  assert.equal(data.stats.total_closed, 1);
  assert.equal(data.submissions.find(s => s.pr_number === 3)?.state, "closed");
  assert.equal(data.layers.reduce((sum, layer) => sum + layer.submission_count, 0), 3);
  assert.equal(data.stats.layers_covered, 2);
});

test("explicit layer declarations take precedence over incidental branch and body keywords", () => {
  assert.equal(classifyLayer("dutch-auction", "[Fellowship Challenge] negotiation: posted-price clocks", "Transport delays a payment."), "negotiation");
  assert.equal(classifyLayer("latency", "New plugin", "## Layer\n\n`registry`. New plugin.\nTransport faults and signing."), "registry");
  assert.equal(classifyLayer("transport", "New plugin", "**Track:** Lab · **Layer:** privacy · encrypt payloads"), "privacy");
  assert.equal(classifyLayer("TRANSPORT", "A protocol", ""), "transport");
});

test("repository layer labels are authoritative and unrelated labels do not change classification", () => {
  const data = buildDataset([
    pr(1, { title: "transport: retry", labels: [{name: "layer:negotiation"}] }),
    pr(2, { title: "auth: token", labels: [{name: "enhancement"}] }),
  ]);
  assert.equal(data.submissions.find(s => s.pr_number === 1)?.layer, "negotiation");
  assert.equal(data.submissions.find(s => s.pr_number === 2)?.layer, "auth");
});

test("status ordering is merged, open, closed and compares equal statuses equally", () => {
  const data = buildDataset([pr(1), pr(2, {state:"closed"}), pr(3, {state:"closed", merged_at:"2026-11-04T01:00:00Z"}), pr(4)]);
  assert.deepEqual(data.submissions.sort(compareSubmissionStatus).map(s => s.state), ["merged", "open", "open", "closed"]);
  const open = data.submissions.find(s => s.state === "open")!;
  assert.equal(compareSubmissionStatus(open, open), 0);
});

test("pagination includes lifetime PRs beyond the former 1000-PR ceiling and deduplicates shifting pages", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    const page = calls++;
    const batch = page < 10 ? Array.from({length:100}, (_, i) => pr(page * 100 + i + 1)) : [pr(1000), pr(1001)];
    return Response.json(batch);
  });
  const prs = await fetchAllPRs();
  assert.equal(calls, 11);
  assert.equal(prs.length, 1001);
});

test("a failed GitHub page rejects instead of returning a partial lifetime count", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("rate limited", {status:403}));
  await assert.rejects(fetchAllPRs, /403/);
});

test("a stalled GitHub refresh times out and serves the last complete snapshot", async (t) => {
  let calls = 0, now = 0;
  t.mock.method(globalThis, "fetch", async (_url: unknown, options?: RequestInit) => {
    if (calls++ === 0) return Response.json([pr(1)]);
    return new Promise<Response>((resolve, reject) => {
      const timer = setTimeout(() => resolve(Response.json([])), 100);
      options?.signal?.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(options.signal?.reason);
      }, {once:true});
    });
  });
  const cache = createDatasetCache(async () => buildDataset(await fetchAllPRs(10)), {now:()=>now});
  await cache.get();
  cache.invalidate();
  now = 300_000;
  assert.equal((await cache.get()).stats.total_submissions, 1);
  await cache.get();
  assert.equal(calls, 2);
});
