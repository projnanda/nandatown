import { createHmac, timingSafeEqual } from "node:crypto";

// Pushes, reviews, and assignments do not change the gallery's displayed fields.
const REVALIDATING_ACTIONS = new Set([
  "opened", "reopened", "closed", "edited", "ready_for_review", "labeled", "unlabeled",
]);

/** Authenticate before parsing or touching the cache. No work outlives the request. */
export async function handleGitHubWebhook(
  req: Request,
  secret: string | undefined,
  invalidate: () => void,
): Promise<Response> {
  if (!secret) {
    return Response.json({ error: "Webhook not configured on this server." }, { status: 503 });
  }
  const payload = await req.text();
  const signature = Buffer.from(req.headers.get("x-hub-signature-256") ?? "");
  const expected = Buffer.from("sha256=" + createHmac("sha256", secret).update(payload).digest("hex"));
  if (signature.length !== expected.length || !timingSafeEqual(signature, expected)) {
    return Response.json({ error: "Bad signature" }, { status: 401 });
  }
  const event = req.headers.get("x-github-event") ?? "";
  if (event === "ping") return Response.json({ ok: true, pong: true });
  if (event !== "pull_request") return Response.json({ ok: true, ignored: event });

  let body;
  try {
    body = JSON.parse(payload);
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Invalid payload");
  } catch {
    return Response.json({ error: "Invalid JSON payload" }, { status: 400 });
  }
  if (body.repository?.full_name !== "projnanda/nandatown") {
    return Response.json({ ok: true, ignored: "repository" });
  }
  const action = body.action ?? "unknown";
  if (!REVALIDATING_ACTIONS.has(action)) {
    return Response.json({ ok: true, ignored_action: action });
  }
  invalidate();
  return Response.json({ ok: true, revalidated: true, action });
}
