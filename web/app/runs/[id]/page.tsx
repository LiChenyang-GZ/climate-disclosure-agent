"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { useUser } from "@/components/user-context";
import { api, type FindingRow, type RunDetail } from "@/lib/api";
import { METHOD, elapsed, usd } from "@/lib/format";
import {
  ENDED,
  applyEvent,
  rows,
  runError,
  runLabel,
  stoppedByBudget,
  type Live,
} from "@/lib/run-progress";
import { useLoad } from "@/lib/use-load";

const POLL_MS = 5000;
const EVENTS = [
  "run_started",
  "check_started",
  "tool_called",
  "check_finished",
  "run_waiting_review",
  "run_final",
  "run_failed",
  "run_cancelled",
];
const TERMINAL = new Set(["run_final", "run_failed", "run_cancelled"]);

const STATE_STYLE = {
  queued: "text-slate-500",
  searching: "text-sky-800",
  done: "text-slate-900",
};

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { handle, setFlash } = useUser();
  const run = useLoad<RunDetail>(`/runs/${id}`, handle);
  const findings = useLoad<FindingRow[]>(`/runs/${id}/findings`, handle);
  const [live, setLive] = useState<Live>({});
  const [paused, setPaused] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [confirming, setConfirming] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const denied = run.state === "error" && run.error.status === 404;
  useEffect(() => {
    if (denied) {
      setFlash(`You don't have access to that page as ${handle}.`);
      router.replace("/");
    }
  }, [denied, handle, router, setFlash]);

  const reloadRun = run.reload;
  const reloadFindings = findings.reload;
  const refresh = useCallback(() => {
    reloadRun();
    reloadFindings();
  }, [reloadRun, reloadFindings]);

  const status = run.state === "ok" ? run.data.status : null;
  const ended = status !== null && ENDED.has(status);

  // Live updates through the proxy (D80). EventSource reconnects by itself and
  // resumes from Last-Event-ID; while it is down the page polls every 5 s.
  useEffect(() => {
    if (status === null || ended) return;
    const source = new EventSource(`/api/runs/${id}/events`);
    source.onopen = () => setPaused(false);
    source.onerror = () => setPaused(true);
    for (const type of EVENTS) {
      source.addEventListener(type, (event) => {
        let payload: unknown = {};
        let checkId: string | null = null;
        try {
          payload = JSON.parse((event as MessageEvent<string>).data);
        } catch {
          payload = {};
        }
        if (payload && typeof payload === "object" && "check_id" in payload) {
          checkId = String((payload as { check_id: unknown }).check_id);
        }
        setLive((current) => applyEvent(current, type, checkId, payload));
        if (type === "check_finished" || type === "run_started" || type === "run_waiting_review") {
          refresh();
        }
        if (TERMINAL.has(type)) {
          source.close();
          setPaused(false);
          refresh();
        }
      });
    }
    return () => source.close();
    // Only reconnect when the run or user changes, not on every status update.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, handle, status === null, ended]);

  useEffect(() => {
    if (!paused || ended) return;
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [paused, ended, refresh]);

  useEffect(() => {
    if (ended) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [ended]);

  if (run.state === "loading" || findings.state === "loading" || denied) {
    return <div aria-busy="true" className="h-24 animate-pulse rounded bg-slate-200" />;
  }
  if (run.state === "error" || findings.state === "error") {
    return (
      <div role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-red-900">
        <p>Couldn&apos;t load this check.</p>
        <button className="mt-2 underline" onClick={refresh}>
          Retry
        </button>
      </div>
    );
  }

  const r = run.data;
  const list = rows(r, findings.data, live);
  const spent = Number(r.spent_usd);
  const budget = Number(r.budget_usd);
  const started = r.started_at ? Date.parse(r.started_at) : null;
  const finished = r.finished_at ? Date.parse(r.finished_at) : null;
  const error = runError(r);
  const cancellable = r.status === "queued" || r.status === "running" || r.status === "waiting_review";
  const complete = r.status === "final" || r.status === "waiting_review";

  async function cancel() {
    setConfirming(false);
    setCancelError(null);
    try {
      await api(`/runs/${id}/cancel`, { method: "POST" });
    } catch {
      setCancelError("Couldn't cancel the check. Try again.");
    }
    refresh();
  }

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Check progress</h1>
          <p className="mt-1 text-slate-600">
            {r.method in METHOD ? METHOD[r.method as keyof typeof METHOD] : r.method} · {r.model_key} ·{" "}
            <Link href={`/packs/${r.pack_id}`} className="underline">
              Back to pack
            </Link>
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span data-testid="run-status" className="rounded bg-slate-200 px-2 py-1 text-sm">
            {runLabel(r, findings.data)}
          </span>
          {cancellable && (
            <button
              onClick={() => setConfirming(true)}
              className="rounded border border-slate-300 px-4 py-2 text-sm"
            >
              Cancel
            </button>
          )}
        </div>
      </div>

      {paused && !ended && (
        <p role="status" className="rounded bg-amber-100 p-3 text-sm text-amber-900">
          Live updates paused. Reconnecting…
        </p>
      )}
      {error && (
        <p role="alert" className="rounded bg-red-50 p-3 text-sm text-red-900">
          {error}
        </p>
      )}
      {stoppedByBudget(r, findings.data) && (
        <p role="status" className="rounded bg-amber-50 p-3 text-sm text-amber-900">
          The check reached its budget. Unfinished checks are marked Not checked; finished results are kept.
        </p>
      )}
      {cancelError && (
        <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
          {cancelError}
        </p>
      )}
      {confirming && (
        <div role="alertdialog" aria-labelledby="cancel-title" className="rounded border border-slate-300 bg-white p-4 text-sm shadow">
          <p id="cancel-title">Stop this check? Finished results are kept.</p>
          <div className="mt-2 flex gap-3">
            <button className="rounded bg-red-700 px-3 py-1 font-medium text-white" onClick={cancel}>
              Stop check
            </button>
            <button className="underline" onClick={() => setConfirming(false)}>
              Keep running
            </button>
          </div>
        </div>
      )}
      {complete && (
        <p role="status" className="rounded bg-emerald-50 p-3 text-sm text-emerald-900">
          Check complete.{" "}
          <Link href={`/runs/${id}/report`} className="font-medium underline">
            Open the report
          </Link>
        </p>
      )}
      {r.status === "cancelled" && (
        <p className="text-sm text-slate-700">
          <Link href={`/runs/${id}/report`} className="underline">
            Open the report
          </Link>{" "}
          for the finished results.
        </p>
      )}

      <dl className="grid grid-cols-2 gap-4 rounded border border-slate-200 p-4 text-sm sm:grid-cols-4">
        <div>
          <dt className="text-slate-500">Elapsed</dt>
          <dd data-testid="elapsed">{started ? elapsed((finished ?? now) - started) : "—"}</dd>
        </div>
        <div>
          <dt className="text-slate-500">Tokens</dt>
          <dd>{(r.tokens_in + r.tokens_out).toLocaleString("en-AU")}</dd>
        </div>
        <div className="col-span-2">
          <dt className="text-slate-500">Spent / budget</dt>
          <dd data-testid="cost">
            {usd(spent)} / {usd(budget)}
          </dd>
          <div
            role="progressbar"
            aria-label="Budget used"
            aria-valuemin={0}
            aria-valuemax={budget}
            aria-valuenow={spent}
            className="mt-1 h-2 rounded bg-slate-200"
          >
            <div
              className="h-2 rounded bg-emerald-600"
              style={{ width: `${budget > 0 ? Math.min(100, (spent / budget) * 100) : 0}%` }}
            />
          </div>
        </div>
      </dl>

      {r.skipped_documents.length > 0 && (
        <div role="note" className="rounded bg-amber-50 p-3 text-sm text-amber-900">
          <p>These files couldn&apos;t be parsed and were not searched:</p>
          <ul className="mt-1 list-disc pl-5" data-testid="skipped">
            {r.skipped_documents.map((d) => (
              <li key={d.id}>{d.filename}</li>
            ))}
          </ul>
        </div>
      )}

      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 text-slate-500">
          <tr>
            <th className="py-2 pr-4">Check</th>
            <th className="py-2 pr-4">Requirement</th>
            <th className="py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {list.map((row) => (
            <tr key={row.checkId} data-testid="check-row" className="border-b border-slate-100 align-top">
              <td className="py-2 pr-4 font-mono">{row.checkId}</td>
              <td className="py-2 pr-4">{row.title}</td>
              <td className={`py-2 ${STATE_STYLE[row.state]}`}>
                <span className={row.status === "error" ? "font-medium text-red-800" : undefined}>
                  {row.status === "error" ? "⚠ " : ""}
                  {row.label}
                </span>
                {row.detail && <span className="block text-xs text-slate-500">{row.detail}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
