"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useUser } from "@/components/user-context";
import type { PackDetail } from "@/lib/api";
import { fy } from "@/lib/format";
import { useLoad } from "@/lib/use-load";

export default function PackPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { handle, setFlash } = useUser();
  const pack = useLoad<PackDetail>(`/packs/${id}`, handle);

  // The API answers 404 for packs outside the user's teams (PRD 6.1).
  const denied = pack.state === "error" && pack.error.status === 404;
  useEffect(() => {
    if (denied) {
      setFlash(`You don't have access to that page as ${handle}.`);
      router.replace("/");
    }
  }, [denied, handle, router, setFlash]);

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
  return (
    <section>
      <h1 className="text-2xl font-semibold">{pack.data.company}</h1>
      <p className="mt-1 text-slate-600">{fy(pack.data.reporting_year)}</p>
      <p className="mt-6 text-slate-600">
        {pack.data.documents.length === 0
          ? "No files yet."
          : `${pack.data.documents.length} file(s)`}
      </p>
    </section>
  );
}
