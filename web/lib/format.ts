import type {
  DocumentKind,
  FindingStatus,
  IndexStatus,
  PackRow,
  PackStatus,
  RunStatus,
} from "./api";

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

export const PACK_STATUS: Record<PackStatus, string> = {
  empty: "Empty",
  uploaded: "Uploaded",
  parsing: "Parsing",
  parsed: "Parsed",
  parsed_with_issues: "Parsed with issues",
  parse_failed: "Parse failed",
};

export const INDEX_STATUS: Record<IndexStatus, string> = {
  pending: "Not parsed",
  indexing: "Parsing",
  done: "Parsed",
  failed: "Parse failed",
};

export const KIND: Record<DocumentKind, string> = {
  sustainability: "Sustainability report",
  annual: "Annual report",
  other: "Other",
};

/** Default document type from the file name; the user can change it. */
export function guessKind(filename: string): DocumentKind {
  const name = filename.toLowerCase();
  if (/sustainab|climate|esg/.test(name)) return "sustainability";
  if (/annual/.test(name)) return "annual";
  return "other";
}
