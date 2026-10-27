"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ApiError, api, type Team } from "@/lib/api";

const MAX_COMPANY = 80;

type Failure = { kind: "duplicate"; packId: string } | { kind: "server" } | null;

export function companyError(value: string): string | null {
  const company = value.trim();
  if (company.length === 0) return "Company name is required.";
  if (company.length > MAX_COMPANY) return `Use at most ${MAX_COMPANY} characters.`;
  return null;
}

export function yearError(value: string): string | null {
  if (value.trim() === "") return "Reporting year is required.";
  const year = Number(value);
  if (!Number.isInteger(year) || year < 2000 || year > 2100) {
    return "Enter a year between 2000 and 2100.";
  }
  return null;
}

export function NewPackDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [company, setCompany] = useState("");
  const [year, setYear] = useState("");
  const [teamId, setTeamId] = useState("");
  const [teams, setTeams] = useState<Team[] | null>(null);
  const [touched, setTouched] = useState({ company: false, year: false });
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<Failure>(null);

  useEffect(() => {
    api<Team[]>("/teams")
      .then((value) => {
        setTeams(value);
        setTeamId((current) => current || (value[0]?.id ?? ""));
      })
      .catch(() => setTeams([]));
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const companyProblem = companyError(company);
  const yearProblem = yearError(year);
  const team = teams?.find((t) => t.id === teamId);
  const canCreate = !companyProblem && !yearProblem && team !== undefined && !submitting;

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!canCreate) return;
    setSubmitting(true);
    setFailure(null);
    try {
      const pack = await api<{ id: string }>("/packs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          company: company.trim(),
          reporting_year: Number(year),
          team_id: teamId,
        }),
      });
      router.push(`/packs/${pack.id}`);
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.code === "duplicate_pack" &&
        typeof error.body.pack_id === "string"
      ) {
        setFailure({ kind: "duplicate", packId: error.body.pack_id });
      } else {
        setFailure({ kind: "server" });
      }
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-slate-900/40 p-4">
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-pack-title"
        onSubmit={create}
        className="w-full max-w-md space-y-4 rounded-lg bg-white p-6 shadow-xl"
      >
        <h2 id="new-pack-title" className="text-lg font-semibold">
          New pack
        </h2>

        {failure?.kind === "server" && (
          <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
            Couldn&apos;t create the pack. Try again.
          </p>
        )}
        {failure?.kind === "duplicate" && (
          <p role="alert" className="rounded bg-amber-50 p-2 text-sm text-amber-900">
            A pack for this company and year already exists.{" "}
            <Link href={`/packs/${failure.packId}`} className="underline">
              Open it
            </Link>
          </p>
        )}

        <Field label="Company" error={touched.company ? companyProblem : null}>
          <input
            className="w-full rounded border border-slate-300 px-3 py-2"
            value={company}
            onChange={(event) => {
              setCompany(event.target.value);
              setTouched((t) => ({ ...t, company: true }));
            }}
            onBlur={() => setTouched((t) => ({ ...t, company: true }))}
          />
        </Field>

        <Field label="Reporting year (FY)" error={touched.year ? yearProblem : null}>
          <input
            inputMode="numeric"
            placeholder="2026"
            className="w-full rounded border border-slate-300 px-3 py-2"
            value={year}
            onChange={(event) => {
              setYear(event.target.value);
              setTouched((t) => ({ ...t, year: true }));
            }}
            onBlur={() => setTouched((t) => ({ ...t, year: true }))}
          />
        </Field>

        <Field label="Team" error={teams !== null && teams.length === 0 ? "Couldn't load teams." : null}>
          <select
            className="w-full rounded border border-slate-300 px-3 py-2"
            value={teamId}
            onChange={(event) => setTeamId(event.target.value)}
          >
            {(teams ?? []).map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </Field>
        {team && (
          <p className="text-sm text-slate-600" data-testid="team-members">
            Members: {team.members.map((m) => m.display_name).join(", ")}
          </p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="rounded px-4 py-2 text-sm" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            disabled={!canCreate}
            className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {submitting ? "Creating…" : "Create"}
          </button>
        </div>
      </form>
    </div>
  );
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error: string | null;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      {children}
      {error && <span className="mt-1 block text-red-700">{error}</span>}
    </label>
  );
}
