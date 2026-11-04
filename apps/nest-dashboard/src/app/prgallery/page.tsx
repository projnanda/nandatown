/**
 * /prgallery — the PR Gallery.
 *
 * A live view of every pull request on projnanda/nandatown,
 * synced straight from GitHub: the twelve protocol layers plus Other,
 * followed by the full PR feed with merged submissions pinned first.
 * Signed GitHub webhooks mark the shared snapshot for a budgeted refresh.
 */

import Link from "next/link";
import { compareSubmissionStatus, loadDataset } from "@/lib/hackathon";
import { EmptyState, SubmissionCard } from "@/components/hackathon-card";

// Render from the shared GitHub snapshot at request time.
export const dynamic = "force-dynamic";

export const metadata = {
  title: "Protocols + Plugins (PRs)",
  description:
    "Every pull request on projnanda/nandatown, live from GitHub: lifetime totals and open, merged, and closed contributions across twelve protocol layers plus Other.",
  alternates: { canonical: "/prgallery" },
};

export default async function PRGalleryPage() {
  const data = await loadDataset();
  const submissions = data.submissions
    .slice()
    .sort((a, b) => {
      // Merged first, then newest activity.
      const statusOrder = compareSubmissionStatus(a, b);
      if (statusOrder) return statusOrder;
      const at = (a.state === "merged" && a.merged_at) || a.created_at;
      const bt = (b.state === "merged" && b.merged_at) || b.created_at;
      return bt.localeCompare(at);
    });

  return (
    <div className="bg-cream-100">
      {/* Header */}
      <section className="paper-texture border-b border-cream-400/70">
        <div className="mx-auto max-w-[1240px] px-6 sm:px-10 pt-16 pb-12">

          <div className="grid gap-12 lg:grid-cols-[1.4fr_1fr] lg:items-end">
            <h1 className="font-display animate-fade-in stagger-1 text-[clamp(2.4rem,5.4vw,4.2rem)] leading-[1.04] tracking-tight text-ink-900">
              Protocols +
              <br />
              <span className="italic text-ink-700">plugins</span>.
            </h1>
            <p className="animate-fade-in stagger-2 text-[1.05rem] leading-[1.6] text-ink-500 max-w-md">
              Every pull request on projnanda/nandatown, synced from GitHub.
              Explore open, merged, and closed contributions across twelve
              protocol layers, with work beyond the stack collected under Other.
            </p>
          </div>

          <div className="mt-10 flex flex-wrap gap-x-10 gap-y-4 animate-fade-in stagger-3">
            <div>
              <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-300">
                Lifetime PRs
              </span>
              <p className="mt-2 font-display text-[2rem] leading-none text-ink-900 tabular-nums">
                {data.generated_at ? data.stats.total_submissions : "—"}
              </p>
            </div>
            <div>
              <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-300">
                Merged
              </span>
              <p className="mt-2 font-display text-[2rem] leading-none text-ink-900 tabular-nums">
                {data.generated_at ? data.stats.total_merged : "—"}
              </p>
            </div>
            <div>
              <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-300">Open</span>
              <p className="mt-2 font-display text-[2rem] leading-none text-ink-900 tabular-nums">
                {data.generated_at ? data.stats.total_open : "—"}
              </p>
            </div>
            <div>
              <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-300">Closed, unmerged</span>
              <p className="mt-2 font-display text-[2rem] leading-none text-ink-900 tabular-nums">
                {data.generated_at ? data.stats.total_closed : "—"}
              </p>
            </div>
            <div>
              <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-300">
                Layers covered
              </span>
              <p className="mt-2 font-display text-[2rem] leading-none text-ink-900 tabular-nums">
                {data.generated_at ? `${data.stats.layers_covered}/${data.stats.layers_total}` : "—"}
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* Layer grid: the twelve layers + Other */}
      <section className="border-b border-cream-400/70">
        <div className="mx-auto max-w-[1240px] px-6 sm:px-10 py-12">
          <div className="grid gap-5 md:grid-cols-2 lg:grid-cols-3">
            {data.layers.map((layer, idx) => (
              <Link
                key={layer.key}
                href={`/prgallery/layers/${layer.key}`}
                className="group block rounded-2xl border border-cream-400/70 bg-cream-50 p-7 transition-colors hover:bg-cream-200/60"
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-ink-300 tabular-nums">
                    {layer.key === "other" ? "＋" : String(idx + 1).padStart(2, "0")}
                  </span>
                  {layer.is_open ? (
                    <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-[0.18em] border border-dashed border-cream-400 text-ink-400">
                      open
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-[0.18em] bg-rust-bg text-rust border border-rust-soft/60">
                      {layer.submission_count}{" "}
                      {layer.submission_count === 1 ? "PR" : "PRs"}
                    </span>
                  )}
                </div>

                <h3 className="mt-5 font-display text-[1.6rem] leading-[1.15] text-ink-900 group-hover:text-ink-700">
                  {layer.label}
                </h3>
                <p className="mt-2 text-[0.92rem] leading-[1.55] text-ink-500">
                  {layer.blurb}
                </p>
              </Link>
            ))}
          </div>
        </div>
      </section>

      {/* Live PR feed */}
      <section className="bg-cream-50">
        <div className="mx-auto max-w-[1240px] px-6 sm:px-10 py-14">
          <div className="flex items-end justify-between gap-6 mb-8">
            <div>
              <p className="eyebrow">All pull requests</p>
              <h2 className="mt-4 font-display text-[2rem] leading-[1.1] text-ink-900">
                Merged work,<br />
                <span className="italic text-ink-700">open reviews</span> and history.
              </h2>
            </div>
          </div>

          {submissions.length === 0 ? (
            <EmptyState
              title={data.generated_at ? "No submissions yet." : "GitHub data is temporarily unavailable."}
              body={data.generated_at ? "Pull requests will appear here as they are submitted." : "Please try again shortly. Lifetime totals will return when GitHub is reachable."}
            />
          ) : (
            <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
              {submissions.map((sub) => (
                <SubmissionCard key={sub.id} submission={sub} />
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
