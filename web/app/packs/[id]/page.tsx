"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { DocumentsTable } from "@/components/documents-table";
import { UploadPanel } from "@/components/upload-panel";
import { useUser } from "@/components/user-context";
import { api, type PackDetail } from "@/lib/api";
import { PACK_STATUS, fy } from "@/lib/format";
import { useLoad } from "@/lib/use-load";

const POLL_MS = 2000;

export default function PackPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { handle, me, setFlash } = useUser();
  const pack = useLoad<PackDetail>(`/packs/${id}`, handle);
  const [parseError, setParseError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // The API answers 404 for packs outside the user's teams (PRD 6.1).
  const denied = pack.state === "error" && pack.error.status === 404;
  useEffect(() => {
    if (denied) {
      setFlash(`You don't have access to that page as ${handle}.`);
      router.replace("/");
    }
  }, [denied, handle, router, setFlash]);

  // While parsing, re-read the server's state; a page refresh resumes the same view.
  const parsing = pack.state === "ok" && pack.data.status === "parsing";
  const { reload } = pack;
  useEffect(() => {
    if (!parsing) return;
    const timer = setInterval(reload, POLL_MS);
    return () => clearInterval(timer);
  }, [parsing, reload]);

  if (pack.state === "loading" || denied) {
    return <div aria-busy="true" className="h-24 animate-pulse rounded bg-slate-200" />;
  }
  if (pack.state === "error") {
    return (
      <div role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-red-900">
        <p>Couldn&apos;t load this pack.</p>
        <button className="mt-2 underline" onClick={pack.reload}>
          Retry
        </button>
      </div>
    );
  }

  const data = pack.data;
  const isLead = me?.role === "lead";
  const parseable = data.documents.some(
    (d) => d.index_status === "pending" || d.index_status === "failed",
  );
  const hasSustainability = data.documents.some(
    (d) => d.kind === "sustainability" || d.parts.some((p) => p.kind === "sustainability"),
  );
  const failed = data.documents.filter((d) => d.index_status === "failed").length;

  async function parse() {
    setStarting(true);
    setParseError(null);
    try {
      await api(`/packs/${id}/parse`, { method: "POST" });
      reload();
    } catch {
      setParseError("Couldn't start parsing. Try again.");
    } finally {
      setStarting(false);
    }
  }

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{data.company}</h1>
          <p className="mt-1 text-slate-600">
            {fy(data.reporting_year)} · {data.team.name}:{" "}
            {data.team.members.map((m) => m.display_name).join(", ")}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span data-testid="pack-status" className="rounded bg-slate-200 px-2 py-1 text-sm">
            {PACK_STATUS[data.status]}
          </span>
          {isLead && (
            <button
              onClick={parse}
              disabled={parsing || starting || !parseable}
              title={!parseable && !parsing ? "Every file is already parsed" : undefined}
              className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {parsing ? "Parsing…" : "Parse"}
            </button>
          )}
        </div>
      </div>

      {parseError && (
        <p role="alert" className="rounded bg-red-50 p-2 text-sm text-red-900">
          {parseError}
        </p>
      )}
      {data.documents.length > 0 && !hasSustainability && (
        <p role="status" className="rounded bg-amber-100 p-3 text-sm text-amber-900">
          No sustainability report in this pack. Most checks will find no evidence.
        </p>
      )}
      {data.status === "parsed_with_issues" && (
        <p role="status" className="rounded bg-amber-50 p-3 text-sm text-amber-900">
          {failed} file{failed === 1 ? "" : "s"} couldn&apos;t be parsed. Parse again to retry.
        </p>
      )}

      <DocumentsTable pack={data} canEdit={isLead} onChanged={reload} />
      {isLead && <UploadPanel pack={data} onUploaded={reload} />}
    </section>
  );
}
