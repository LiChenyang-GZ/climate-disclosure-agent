"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { ApiError, api, type Method, type RunDetail, type RunOptions } from "@/lib/api";
import { METHOD, usd } from "@/lib/format";

type Failure =
  | { kind: "key" }
  | { kind: "active"; runId: string | null }
  | { kind: "network" }
  | { kind: "server" }
  | null;

const MODEL_NAMES: Record<string, string> = { luna: "gpt-6-luna (cheaper)", sol: "gpt-6.1-sol (stronger)" };

export function budgetError(value: string, max: number): string | null {
  const budget = Number(value);
  if (value.trim() === "" || !Number.isFinite(budget) || budget <= 0) {
    return "Enter a budget above $0.";
  }
  if (budget > max) return `The budget can be at most ${usd(max)}.`;
  return null;
}

function newKey(): string {
  return crypto.randomUUID();
}

export function NewCheckDialog({ packId, onClose }: { packId: string; onClose: () => void }) {
  const router = useRouter();
  const [options, setOptions] = useState<RunOptions | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [method, setMethod] = useState<Method>("A");
  const [model, setModel] = useState("luna");
  const [selected, setSelected] = useState<string[] | null>(null);
  const [budget, setBudget] = useState("2.00");
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<Failure>(null);
  // One key per request body: a Retry reuses it, so the API returns the same run (D24).
  const [key, setKey] = useState(newKey);
  const inFlight = useRef(false);

  useEffect(() => {
    api<RunOptions>("/run-options")
      .then((value) => {
        setOptions(value);
        setSelected(value.checks.map((c) => c.id));
      })
      .catch(() => setLoadFailed(true));
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function edit<T>(setter: (value: T) => void) {
    return (value: T) => {
      setter(value);
      setKey(newKey());
      setFailure(null);
    };
  }

  const checks = options?.checks ?? [];
  const chosen = selected ?? [];
  const max = options?.max_budget_usd ?? 10;
  const budgetProblem = budgetError(budget, max);
  const canStart = options !== null && chosen.length > 0 && !budgetProblem && !submitting;
  const perCheck = options?.estimate_per_check_usd[method]?.[model];
  const estimate =
    perCheck && chosen.length > 0
      ? `Estimated cost: ${usd(perCheck[0] * chosen.length)}–${usd(
          Math.min(perCheck[1] * chosen.length, budgetProblem ? Infinity : Number(budget)),
        )}`
      : null;

  async function start(event: React.FormEvent) {
    event.preventDefault();
    if (!canStart || inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    setFailure(null);
    try {
      const all = chosen.length === checks.length;
      const run = await api<RunDetail>("/runs", {
        method: "POST",
        headers: { "content-type": "application/json", "idempotency-key": key },
        body: JSON.stringify({
          pack_id: packId,
          method,
          model_key: model,
          budget_usd: Number(budget),
          check_ids: all ? null : checks.map((c) => c.id).filter((id) => chosen.includes(id)),
        }),
      });
      router.push(`/runs/${run.id}`);
    } catch (error) {
      if (error instanceof ApiError && error.code === "llm_key_missing") {
        setFailure({ kind: "key" });
      } else if (error instanceof ApiError && error.code === "run_active") {
        const runId = typeof error.body.run_id === "string" ? error.body.run_id : null;
        setFailure({ kind: "active", runId });
      } else if (
        error instanceof ApiError &&
        (error.status === 0 || error.code === "api_unreachable")
      ) {
        setFailure({ kind: "network" });
      } else {
        setFailure({ kind: "server" });
      }
      setSubmitting(false);
      inFlight.current = false;
    }
  }

  const all = checks.length > 0 && chosen.length === checks.length;

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-slate-900/40 p-4">
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-check-title"
        onSubmit={start}
        className="max-h-full w-full max-w-lg space-y-4 overflow-y-auto rounded-lg bg-white p-6 shadow-xl"
      >
        <h2 id="new-check-title" className="text-lg font-semibold">
          New check
        </h2>

        {failure?.kind === "key" && (
          <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
            OpenAI API key isn&apos;t set. Add OPENAI_API_KEY to .env and restart the backend.
          </p>
        )}
        {failure?.kind === "active" && (
          <p role="alert" className="rounded bg-amber-50 p-2 text-sm text-amber-900">
            A check is already running on this pack.{" "}
            {failure.runId && (
              <Link href={`/runs/${failure.runId}`} className="underline">
                View it
              </Link>
            )}
          </p>
        )}
        {failure?.kind === "network" && (
          <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
            Couldn&apos;t reach the server.{" "}
            <button type="submit" className="underline">
              Retry
            </button>
          </p>
        )}
        {failure?.kind === "server" && (
          <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
            Couldn&apos;t start the check. Try again.
          </p>
        )}
        {loadFailed && (
          <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
            Couldn&apos;t load the check options.
          </p>
        )}

        <div className="text-sm">
          <span className="mb-1 block font-medium text-slate-700">Check set</span>
          <span>AASB S2 + ASIC 2026 review ({checks.length || 15} checks)</span>
        </div>

        <label className="block text-sm">
          <span className="mb-1 block font-medium text-slate-700">Retrieval strategy</span>
          <select
            className="w-full rounded border border-slate-300 px-3 py-2"
            aria-label="Retrieval strategy"
            value={method}
            onChange={(event) => edit(setMethod)(event.target.value as Method)}
          >
            {(options?.methods ?? (["A", "B", "C"] as Method[])).map((m) => (
              <option key={m} value={m}>
                {METHOD[m]}
              </option>
            ))}
          </select>
        </label>

        <label className="block text-sm">
          <span className="mb-1 block font-medium text-slate-700">Model</span>
          <select
            className="w-full rounded border border-slate-300 px-3 py-2"
            aria-label="Model"
            value={model}
            onChange={(event) => edit(setModel)(event.target.value)}
          >
            {(options?.models ?? []).map((m) => (
              <option key={m.key} value={m.key}>
                {MODEL_NAMES[m.key] ?? m.model}
              </option>
            ))}
          </select>
        </label>

        <fieldset className="text-sm">
          <legend className="mb-1 font-medium text-slate-700">Checks</legend>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={all}
              onChange={() => edit(setSelected)(all ? [] : checks.map((c) => c.id))}
            />
            All ({checks.length})
          </label>
          <div className="mt-1 grid max-h-40 grid-cols-1 gap-1 overflow-y-auto rounded border border-slate-200 p-2 sm:grid-cols-2">
            {checks.map((c) => (
              <label key={c.id} className="flex items-start gap-2">
                <input
                  type="checkbox"
                  aria-label={`${c.id} ${c.title}`}
                  checked={chosen.includes(c.id)}
                  onChange={() =>
                    edit(setSelected)(
                      chosen.includes(c.id) ? chosen.filter((id) => id !== c.id) : [...chosen, c.id],
                    )
                  }
                />
                <span>
                  <span className="font-mono">{c.id}</span> {c.title}
                </span>
              </label>
            ))}
          </div>
          {options !== null && chosen.length === 0 && (
            <span className="mt-1 block text-red-700">Pick at least one check.</span>
          )}
        </fieldset>

        <label className="block text-sm">
          <span className="mb-1 block font-medium text-slate-700">Budget cap (USD)</span>
          <input
            inputMode="decimal"
            className="w-full rounded border border-slate-300 px-3 py-2"
            aria-label="Budget cap (USD)"
            value={budget}
            onChange={(event) => edit(setBudget)(event.target.value)}
          />
          {budgetProblem && <span className="mt-1 block text-red-700">{budgetProblem}</span>}
        </label>

        {estimate && (
          <p className="text-sm text-slate-600" data-testid="estimate">
            {estimate}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="rounded px-4 py-2 text-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={!canStart}
            aria-busy={submitting}
            className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {submitting ? (
              <span className="inline-flex items-center gap-2">
                <span className="h-3 w-3 animate-spin rounded-full border-2 border-white border-t-transparent" />
                Starting…
              </span>
            ) : (
              "Start check"
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
