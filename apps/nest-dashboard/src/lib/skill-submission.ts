import type { NewSkill, SkillSourceType } from "./skills";

/** Largest JSON body POST /api/skills reads. Checked before the field limits. */
export const MAX_SKILL_REQUEST_BYTES = 256 * 1024;

export const FIELD_LIMITS = {
  name: 200,
  author: 200,
  email: 254,
  github_username: 39,
  description: 2_000,
  source_url: 2_048,
  content: 200_000,
  endpoints: 8_192,
  tags: 512,
} as const;

export type FieldName = keyof typeof FIELD_LIMITS;

/** Why a submission was refused, so each caller can word it for its audience. */
export type SkillSubmissionProblem =
  | { code: "too_long"; field: FieldName; limit: number }
  | { code: "name_required" }
  | { code: "email_invalid" }
  | { code: "github_username_invalid" }
  | { code: "source_type_invalid" }
  | { code: "source_url_required" }
  | { code: "source_url_invalid" }
  | { code: "content_short" };

export type SkillSubmissionResult =
  | { ok: true; value: NewSkill }
  | { ok: false; error: string; problem: SkillSubmissionProblem };

function refuse(error: string, problem: SkillSubmissionProblem): SkillSubmissionResult {
  return { ok: false, error, problem };
}

function stringValue(
  input: Record<string, unknown>,
  field: FieldName | "source_type",
): string {
  const value = input[field];
  return typeof value === "string" ? value.trim() : "";
}

/** Source syntax only. This does not authorize a fetch or attest reachability. */
function isValidHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function normalizeGithubUsername(value: string): string {
  let normalized = value.trim().replace(/^@/, "");
  const urlMatch = normalized.match(/github\.com\/([^/\s?#]+)/i);
  if (urlMatch) normalized = urlMatch[1];
  return normalized.replace(/\/+$/, "").trim();
}

function isValidGithubUsername(value: string): boolean {
  return /^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$/.test(value);
}

function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

/**
 * Shared validation for the JSON API and the HTML form. Bounds every field,
 * checks source syntax only, and never records a reachability claim: a saved
 * catalog entry is discoverable, not verified, fetched, hosted or endorsed.
 * `error` is worded for API clients; the form maps `problem` to its own copy.
 */
export function validateSkillSubmission(
  input: Record<string, unknown>,
): SkillSubmissionResult {
  const values = {
    name: stringValue(input, "name"),
    author: stringValue(input, "author"),
    email: stringValue(input, "email"),
    github_username: normalizeGithubUsername(stringValue(input, "github_username")),
    description: stringValue(input, "description"),
    source_url: stringValue(input, "source_url"),
    content: stringValue(input, "content"),
    endpoints: stringValue(input, "endpoints"),
    tags: stringValue(input, "tags"),
  };

  for (const field of Object.keys(FIELD_LIMITS) as FieldName[]) {
    const limit = FIELD_LIMITS[field];
    if (values[field].length > limit) {
      return refuse(
        `${field} is too long (maximum ${limit} characters).`,
        { code: "too_long", field, limit },
      );
    }
  }

  const sourceType = stringValue(input, "source_type") as SkillSourceType;
  if (!values.name) {
    return refuse("name is required", { code: "name_required" });
  }
  if (values.email && !isValidEmail(values.email)) {
    return refuse("email must be a valid address", { code: "email_invalid" });
  }
  if (values.github_username && !isValidGithubUsername(values.github_username)) {
    return refuse("github_username must be a valid GitHub username", {
      code: "github_username_invalid",
    });
  }
  if (!(["url", "github", "content"] as string[]).includes(sourceType)) {
    return refuse("source_type must be one of: url, github, content", {
      code: "source_type_invalid",
    });
  }
  if ((sourceType === "url" || sourceType === "github") && !values.source_url) {
    return refuse("source_url is required for url/github submissions", {
      code: "source_url_required",
    });
  }
  if (
    (sourceType === "url" || sourceType === "github") &&
    !isValidHttpUrl(values.source_url)
  ) {
    return refuse("source_url must be a valid http(s) URL", {
      code: "source_url_invalid",
    });
  }
  if (sourceType === "content" && values.content.length < 20) {
    return refuse("content is required (and must be the full SkillMD)", {
      code: "content_short",
    });
  }

  return {
    ok: true,
    value: {
      name: values.name,
      author: values.author || null,
      description: values.description || null,
      source_type: sourceType,
      source_url: sourceType === "content" ? null : values.source_url,
      content: sourceType === "content" ? values.content : null,
      endpoints: values.endpoints || null,
      tags: values.tags || null,
      reachable: null,
      email: values.email || null,
      github_username: values.github_username || null,
    },
  };
}
