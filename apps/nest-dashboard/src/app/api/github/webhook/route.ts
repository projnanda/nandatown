import { revalidatePath } from "next/cache";
import { invalidatePrDataset } from "@/lib/hackathon-github";
import { handleGitHubWebhook } from "@/lib/github-webhook";

export const dynamic = "force-dynamic";

/** GitHub's existing pull_request webhook shares GITHUB_WEBHOOK_SECRET with Railway. */
export async function POST(req: Request) {
  return handleGitHubWebhook(req, process.env.GITHUB_WEBHOOK_SECRET, () => {
    // Mark the shared snapshot dirty synchronously. The next eligible read
    // refreshes it within the GitHub API budget; no timer outlives this request.
    invalidatePrDataset();
    revalidatePath("/prgallery", "layout");
    revalidatePath("/");
  });
}
