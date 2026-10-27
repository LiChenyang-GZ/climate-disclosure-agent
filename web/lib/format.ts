import type { FindingStatus, PackRow, RunStatus } from "./api";

const SUMMARY: [FindingStatus, string][] = [
  ["evidence_found", "found"],
  ["partial_evidence", "partial"],
  ["no_evidence_found", "no evidence"],
  ["inconsistent", "inconsistent"],
  ["needs_review", "needs review"],
  ["not_checked", "not checked"],
  ["error", "error"],
];

const RUN_STATUS: Record<RunStatus, string> = {
  queued: "Queued",
  running: "Running",
  waiting_review: "Awaiting review",
  final: "Final",
  cancelled: "Cancelled",
  failed: "Failed",
};

export function fy(year: number): string {
  return `FY${year}`;
}

/** "11 found · 1 inconsistent · 1 needs review" for a pack's latest run. */
export function runSummary(run: PackRow["latest_run"]): string {
  if (run === null) return "No checks yet";
  const parts = SUMMARY.filter(([status]) => (run.counts[status] ?? 0) > 0).map(
    ([status, label]) => `${run.counts[status]} ${label}`,
  );
  return parts.length > 0 ? parts.join(" · ") : RUN_STATUS[run.status];
}
