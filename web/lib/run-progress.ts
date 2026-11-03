import type { FindingRow, FindingStatus, RunDetail } from "./api";
import { FINDING_STATUS } from "./format";

/** What SSE has told us about one check since the page opened. */
export interface LiveCheck {
  started: boolean;
  steps: number;
  lastTool: string | null;
  finished: FindingStatus | null;
}

export type Live = Record<string, LiveCheck>;

const EMPTY: LiveCheck = { started: false, steps: 0, lastTool: null, finished: null };

/** Applies one run event (worker event types, `src/cda/agent`) to the live state. */
export function applyEvent(live: Live, type: string, checkId: string | null, payload: unknown): Live {
  if (checkId === null) return live;
  const data = (payload ?? {}) as Record<string, unknown>;
  const current = live[checkId] ?? EMPTY;
  if (type === "check_started") return { ...live, [checkId]: { ...current, started: true } };
  if (type === "tool_called") {
    const tool = typeof data.tool === "string" ? data.tool : "tool";
    const query = typeof data.query === "string" && data.query ? `: ${data.query}` : "";
    return {
      ...live,
      [checkId]: { ...current, started: true, steps: current.steps + 1, lastTool: `${tool}${query}` },
    };
  }
  if (type === "check_finished" && typeof data.status === "string") {
    return { ...live, [checkId]: { ...current, finished: data.status as FindingStatus } };
  }
  return live;
}

export const ENDED = new Set(["final", "waiting_review", "cancelled", "failed"]);

/** Stopped: budget is not a stored status: the run ended and some checks ran out of budget. */
export function stoppedByBudget(run: RunDetail, findings: FindingRow[]): boolean {
  return (
    (run.status === "final" || run.status === "waiting_review") &&
    findings.some((f) => f.needs_review_reason === "budget_exhausted")
  );
}

export function runLabel(run: RunDetail, findings: FindingRow[]): string {
  if (stoppedByBudget(run, findings)) return "Stopped: budget";
  return {
    queued: "Queued",
    running: "Running",
    waiting_review: "Awaiting review",
    final: "Final",
    cancelled: "Cancelled",
    failed: "Failed",
  }[run.status];
}

export function runError(run: RunDetail): string | null {
  if (run.status !== "failed") return null;
  if (run.error === "llm_auth") return "OpenAI rejected the API key. Check OPENAI_API_KEY.";
  return run.error_message ?? "The check could not be completed.";
}

const REASONS: Record<string, string> = {
  llm_unavailable: "The model service is temporarily unavailable.",
  llm_rejected: "The model service rejected the request.",
  internal_error: "An internal error stopped this check.",
};

export interface Row {
  checkId: string;
  title: string;
  state: "queued" | "searching" | "done";
  label: string;
  detail: string | null;
  status: FindingStatus | null;
}

/** One row per selected check: a stored finding wins, then live SSE state, then Queued. */
export function rows(run: RunDetail, findings: FindingRow[], live: Live): Row[] {
  const selected = run.check_ids === null ? null : new Set(run.check_ids);
  const ended = ENDED.has(run.status);
  return findings
    .filter((f) => selected === null || selected.has(f.check_id))
    .map((f) => {
      const base = { checkId: f.check_id, title: f.title };
      const l = live[f.check_id] ?? EMPTY;
      const status: FindingStatus | null = f.id !== null ? f.status : l.finished;
      if (status !== null || ended) {
        const s = status ?? "not_checked";
        const detail =
          s === "error" && f.needs_review_reason
            ? (REASONS[f.needs_review_reason] ?? f.needs_review_reason)
            : null;
        return { ...base, state: "done", label: FINDING_STATUS[s], detail, status: s };
      }
      if (l.started) {
        const detail = l.steps > 0 && l.lastTool ? `Step ${l.steps} · ${l.lastTool}` : null;
        return { ...base, state: "searching", label: "Searching", detail, status: null };
      }
      return { ...base, state: "queued", label: "Queued", detail: null, status: null };
    });
}
