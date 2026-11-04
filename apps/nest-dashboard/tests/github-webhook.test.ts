import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";
import { handleGitHubWebhook } from "../src/lib/github-webhook";

const secret = "test-webhook-secret";
function request(body: string, event = "pull_request", signingSecret = secret) {
  return new Request("http://localhost/api/github/webhook", {
    method: "POST", body,
    headers: {
      "x-github-event": event,
      "x-hub-signature-256": "sha256=" + createHmac("sha256", signingSecret).update(body).digest("hex"),
    },
  });
}
const payload = (action: string, repo = "projnanda/nandatown") => JSON.stringify({action, repository:{full_name:repo}});

test("only authenticated events can invalidate the cache", async () => {
  let invalidations = 0;
  const invalidate = () => { invalidations++; };
  assert.equal((await handleGitHubWebhook(request(payload("opened")), undefined, invalidate)).status, 503);
  assert.equal((await handleGitHubWebhook(request(payload("opened"), "pull_request", "wrong"), secret, invalidate)).status, 401);
  assert.equal((await handleGitHubWebhook(new Request("http://localhost", {method:"POST", body:"{}"}), secret, invalidate)).status, 401);
  assert.equal(invalidations, 0);
});

test("signed ping succeeds and malformed JSON is rejected without invalidation", async () => {
  let invalidations = 0;
  const invalidate = () => { invalidations++; };
  const ping = await handleGitHubWebhook(request("{}", "ping"), secret, invalidate);
  assert.deepEqual(await ping.json(), {ok:true, pong:true});
  assert.equal((await handleGitHubWebhook(request("not JSON"), secret, invalidate)).status, 400);
  assert.equal((await handleGitHubWebhook(request("null"), secret, invalidate)).status, 400);
  assert.equal(invalidations, 0);
});

test("consecutive relevant PR events invalidate immediately, including close and layer-label changes", async () => {
  let invalidations = 0;
  const actions = ["opened", "edited", "closed", "reopened", "labeled", "unlabeled", "ready_for_review"];
  for (const action of actions) {
    const response = await handleGitHubWebhook(request(payload(action)), secret, () => { invalidations++; });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).revalidated, true);
  }
  assert.equal(invalidations, actions.length);
});

test("unrelated repos, events, and non-display actions do not invalidate", async () => {
  let invalidations = 0;
  for (const req of [request(payload("opened", "other/repo")), request(payload("opened"), "push"), request(payload("synchronize"))]) {
    assert.equal((await handleGitHubWebhook(req, secret, () => { invalidations++; })).status, 200);
  }
  assert.equal(invalidations, 0);
});
