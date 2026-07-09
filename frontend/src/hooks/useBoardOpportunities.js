// Adapter: read the Supabase ``jobs`` table (via ``/api/jobs``) and
// reshape the response to the shape ``CompanyCard`` expects on the
// Scanner page.
//
// Why this exists
// ===============
//
// The boards tab used to call ``useScannerOpportunities('boards', …)``,
// which POSTed to ``/api/scan/boards`` and triggered a LIVE scrape of
// every Ashby / Greenhouse / Lever company in the JSON lists
// (~10K org fetches per page load after ``BOARDS_LIMIT=0``). On the
// deployed Render backend that means a 100+ second response on every
// page open and a fresh ``httpx`` storm in the logs.
//
// With this hook, the boards tab reads from the ``jobs`` table that the
// GHA ``boards-scan`` cron has already populated. Opening the boards
// tab is a single Postgres read; the scrape happens on a schedule the
// operator controls (hourly on the active tier, daily on the dormant
// tier). The four other tabs (``funding``/``ngos``/``remote``/``oss``)
// keep their existing live-scrape behavior — this hook is intentionally
// ``boards``-only.
import { useMemo } from 'react';
import { useJobs } from './useJobs';

export function useBoardOpportunities() {
  const { data, isLoading } = useJobs({ status: 'in_review', page_size: 50 });

  return useMemo(() => {
    const jobs = data?.jobs || [];
    const opportunities = jobs.map((job) => ({
      id: job.id,
      title: job.title,
      url: job.url,
      organization: job.company_name,
      source: job.ats_type,
      // CompanyCard switches on ``category`` for the OSS extras panel +
      // the "Generate Outreach" button visibility; ``boards`` is the
      // only one that takes the latter path.
      category: 'boards',
      // Pass the raw 0–1 score from ``/api/jobs``; CompanyCard applies the
      // * 100 in its JSX (``<ScoreBadge score={opportunity.score * 100} />``)
      // so no extra transform needed here.
      score: job.ai_fit_score ?? 0,
      // The job's LLM-generated reasoning is the closest thing the
      // Supabase row has to a "description" — useful in the CompanyCard
      // preview block.
      description: job.ai_fit_reasoning,
      // No published_at column on the ``jobs`` table; reuse the
      // review_deadline ISO string so ``timeAgo(...)`` in CompanyCard
      // renders a relative date.
      published: job.review_deadline,
    }));
    return {
      data: {
        opportunities,
        count: opportunities.length,
        // ``/api/jobs`` has no time-window concept; ``"—"`` is a stand-in
        // that keeps the existing Dashboard copy ("X matches in the
        // last Yh") syntactically valid without lying about a specific
        // number — the row-level created_at drives "when" in practice.
        delta_hours: '—',
      },
      isLoading,
    };
  }, [data, isLoading]);
}
