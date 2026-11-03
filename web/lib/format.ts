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

export const RUN_STATUS: Record<RunStatus, string> = {
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

export const FINDING_STATUS: Record<FindingStatus, string> = {
  evidence_found: "Evidence found",
  partial_evidence: "Partial evidence",
  no_evidence_found: "No evidence found",
  inconsistent: "Inconsistent",
  needs_review: "Needs review",
  not_checked: "Not checked",
  error: "Error",
};

export const METHOD: Record<"A" | "B" | "C", string> = {
  A: "Agentic search",
  B: "Classic RAG",
  C: "Agentic + semantic",
};

export function usd(value: number | string): string {
  return `$${Number(value).toFixed(2)}`;
}

/** "4:05" or "1:02:03" from milliseconds. */
export function elapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** Upload formats (PRD 6.3, X1). */
export const ACCEPTED = [".pdf", ".docx", ".xlsx"];

export function accepted(filename: string): boolean {
  const name = filename.toLowerCase();
  return ACCEPTED.some((ext) => name.endsWith(ext));
}

export function isPdf(filename: string): boolean {
  return filename.toLowerCase().endsWith(".pdf");
}

/** Word files are located by section and Excel files by sheet, not by page (X1). */
export function pagesLabel(filename: string, count: number): string {
  const name = filename.toLowerCase();
  if (name.endsWith(".docx")) return `${count} section${count === 1 ? "" : "s"}`;
  if (name.endsWith(".xlsx")) return `${count} sheet${count === 1 ? "" : "s"}`;
  return String(count);
}

/** Source column (X2): "SharePoint · /Drafts/report.pdf" for imported files, else "Upload". */
export function sourceLabel(source: { provider: string; path: string } | null | undefined): string {
  if (!source) return "Upload";
  return `${source.provider === "microsoft" ? "SharePoint" : source.provider} · ${source.path}`;
}
