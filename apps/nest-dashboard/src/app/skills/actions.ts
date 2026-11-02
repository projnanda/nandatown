"use server";

import { revalidatePath, revalidateTag } from "next/cache";
import { headers } from "next/headers";
import { createSkill, SKILLS_CACHE_TAG } from "@/lib/skills";
import { initialSubmitState, type SubmitState } from "./form-state";
import {
  validateSkillSubmission,
  type FieldName,
  type SkillSubmissionProblem,
} from "@/lib/skill-submission";

/**
 * Best-effort client IP from the proxy headers Railway sets. Falls back to
 * null when we can't tell (e.g. local dev without a proxy).
 */
async function clientIp(): Promise<string | null> {
  const h = await headers();
  const forwarded = h.get("x-forwarded-for");
  if (forwarded) return forwarded.split(",")[0].trim() || null;
  return h.get("x-real-ip");
}

const FIELD_LABELS: Record<FieldName, string> = {
  name: "name",
  author: "author name",
  email: "email",
  github_username: "GitHub username",
  description: "description",
  source_url: "link",
  content: "SkillMD text",
  endpoints: "endpoints",
  tags: "tags",
};

/** The form's own wording for each refusal; the API keeps its field names. */
function formMessage(problem: SkillSubmissionProblem): string {
  switch (problem.code) {
    case "too_long":
      return `That ${FIELD_LABELS[problem.field]} is too long (maximum ${problem.limit.toLocaleString("en-US")} characters).`;
    case "name_required":
      return "Give your SkillMD a name.";
    case "email_invalid":
      return "That email doesn't look right.";
    case "github_username_invalid":
      return "Enter just your GitHub username (e.g. octocat).";
    case "source_type_invalid":
      return "Pick how you want to submit it.";
    case "source_url_required":
      return "Add the link to your SkillMD.";
    case "source_url_invalid":
      return "That link doesn't look like a real URL.";
    case "content_short":
      return "Paste the full SkillMD text — that looks too short.";
  }
}

/**
 * Save a SkillMD submission from the form. Validation is shared with
 * POST /api/skills. The server never fetches the submitted URL: a hosted link
 * is stored with `reachable: null` and the catalog labels it "link not
 * checked". Saving a record is not evidence that the link is live or safe.
 */
export async function submitSkill(
  _prev: SubmitState,
  formData: FormData,
): Promise<SubmitState> {
  const fields = Object.fromEntries(formData.entries());
  const validated = validateSkillSubmission(fields);
  if (!validated.ok) {
    return { ...initialSubmitState, error: formMessage(validated.problem) };
  }

  // --- Save ---------------------------------------------------------------
  try {
    const submitterIp = await clientIp();
    const skill = await createSkill({
      ...validated.value,
      submitter_ip: submitterIp,
    });
    revalidateTag(SKILLS_CACHE_TAG, "max");
    revalidatePath("/skills");
    return { ok: true, error: null, createdId: skill.id, createdName: skill.name };
  } catch (err) {
    console.error("submitSkill failed:", err);
    return {
      ...initialSubmitState,
      error: "Something went wrong saving it. Please try again.",
    };
  }
}
