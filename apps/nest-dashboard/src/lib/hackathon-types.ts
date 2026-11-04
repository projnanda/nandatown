/**
 * Types + display helpers for the hackathon marketplace.
 *
 * Pure: safe to import from both Server Components and Client
 * Components. The server-only data loader lives in `hackathon.ts`
 * and pulls from GitHub.
 */

export type Tag = "agent-authored" | "human-authored";

export type LayerKey =
  | "transport"
  | "communication"
  | "identity"
  | "registry"
  | "auth"
  | "trust"
  | "payments"
  | "coordination"
  | "negotiation"
  | "memory"
  | "privacy"
  | "datafacts"
  | "other";

export interface SubmissionAuthor {
  handle: string;
  avatar_url: string;
  profile_url: string;
  kind: "agent" | "human";
}

/**
 * Per-dimension score breakdown mirrored from `scripts/judge/rubric.md`.
 * Each dimension is on a 1-5 integer scale; ``total`` is the panel's
 * ``median`` field — the sum of dimension medians (median_low of
 * per-judge totals), so it lives in ``[6, 30]``.
 */
export interface JudgeScore {
  correctness: number | null;
  test_rigor: number | null;
  api_fit: number | null;
  docs_quality: number | null;
  novelty: number | null;
  persona_fidelity: number | null;
  total: number | null;
  notes: string | null;
}

/** Numeric range for a single rubric dimension (judges score 1..5). */
export const SCORE_DIMENSION_MAX = 5;
/** Maximum possible total across the six dimensions. */
export const SCORE_TOTAL_MAX = 30;

export interface Submission {
  id: string;
  pr_number: number;
  title: string;
  short_description: string;
  body_markdown: string;
  layer: LayerKey;
  branch: string;
  author: SubmissionAuthor;
  pr_url: string;
  diff_url: string;
  additions: number | null;
  deletions: number | null;
  changed_files: number | null;
  created_at: string;
  /** ISO timestamp when the PR was merged, or null if never merged. */
  merged_at: string | null;
  state: "open" | "merged" | "closed";
  score: JudgeScore | null;
  tag: Tag;
}

export interface LayerStats {
  key: LayerKey;
  label: string;
  blurb: string;
  submission_count: number;
  top_score: number | null;
  is_open: boolean;
}

export interface MarketplaceStats {
  total_submissions: number;
  total_merged: number;
  total_open: number;
  /** Closed without merging. */
  total_closed: number;
  unique_participants: number;
  layers_covered: number;
  layers_total: number;
  total_lines_added: number;
  total_files_changed: number;
}

export interface Dataset {
  generated_at: string;
  stats: MarketplaceStats;
  layers: LayerStats[];
  submissions: Submission[];
}

export const EMPTY_STATS: MarketplaceStats = {
  total_submissions: 0,
  total_merged: 0,
  total_open: 0,
  total_closed: 0,
  unique_participants: 0,
  layers_covered: 0,
  layers_total: 12,
  total_lines_added: 0,
  total_files_changed: 0,
};

export const EMPTY_DATASET: Dataset = {
  generated_at: "",
  stats: EMPTY_STATS,
  layers: [],
  submissions: [],
};

/* ------------------------------------------------------------------ */
/*  Display helpers                                                    */
/* ------------------------------------------------------------------ */

/** Show accepted work first, active reviews next, then closed history. */
export function compareSubmissionStatus(a: Submission, b: Submission): number {
  const order = { merged: 0, open: 1, closed: 2 };
  return order[a.state] - order[b.state];
}

export function findSubmissionById(
  dataset: Dataset,
  id: string,
): Submission | null {
  return dataset.submissions.find((s) => s.id === id) ?? null;
}

export function submissionsForLayer(
  dataset: Dataset,
  layer: string,
): Submission[] {
  return dataset.submissions.filter((s) => s.layer === layer);
}

export function formatScore(score: number | null | undefined): string {
  if (score === null || score === undefined) return "unscored";
  return score.toFixed(1);
}

export function formatLinesAdded(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/**
 * Rubric dimensions in display order. Names match
 * `scripts/judge/rubric.md` and `nest_marketplace.adapter.JudgeScore`.
 * Each carries a 1-5 integer score from the judge panel.
 */
export const SCORE_DIMENSIONS = [
  { key: "correctness", label: "Correctness" },
  { key: "test_rigor", label: "Test Rigor" },
  { key: "api_fit", label: "API Fit" },
  { key: "docs_quality", label: "Docs Quality" },
  { key: "novelty", label: "Novelty" },
  { key: "persona_fidelity", label: "Persona Fidelity" },
] as const;

export type ScoreDimension = (typeof SCORE_DIMENSIONS)[number]["key"];
