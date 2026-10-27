"use client";

import Link from "next/link";
import { useState } from "react";

import { NewPackDialog } from "@/components/new-pack-dialog";
import { useUser } from "@/components/user-context";
import type { PackRow } from "@/lib/api";
import { PACK_STATUS, fy, runSummary } from "@/lib/format";
import { useLoad } from "@/lib/use-load";

export default function PacksPage() {
  const { handle, me, meFailed, reloadMe } = useUser();
  const packs = useLoad<PackRow[]>("/packs", handle);
  const [creating, setCreating] = useState(false);
  const isLead = me?.role === "lead";
  const failed = packs.state === "error" || meFailed;

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Packs</h1>
        {isLead && (
          <button
            className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-800"
            onClick={() => setCreating(true)}
          >
            New pack
          </button>
        )}
      </div>

      {!failed && (packs.state === "loading" || me === null) && <Skeleton />}

      {failed && (
        <div role="alert" className="rounded border border-red-200 bg-red-50 p-4 text-red-900">
          <p>Couldn&apos;t load packs.</p>
          <button className="mt-2 underline" onClick={() => {
              if (meFailed) reloadMe();
              packs.reload();
            }}>
            Retry
          </button>
        </div>
      )}

      {packs.state === "ok" && me !== null && packs.data.length === 0 && (
        <div className="rounded border border-dashed border-slate-300 bg-white p-10 text-center">
          <p className="text-slate-600">
            {isLead ? "No packs yet." : "No packs assigned to you yet."}
          </p>
        </div>
      )}

      {packs.state === "ok" && me !== null && packs.data.length > 0 && (
        <table className="w-full overflow-hidden rounded border border-slate-200 bg-white text-sm">
          <thead className="bg-slate-100 text-left text-slate-600">
            <tr>
              <th className="px-4 py-2 font-medium">Company</th>
              <th className="px-4 py-2 font-medium">Year</th>
              <th className="px-4 py-2 font-medium">Files</th>
              <th className="px-4 py-2 font-medium">Status</th>
              <th className="px-4 py-2 font-medium">Team</th>
              <th className="px-4 py-2 font-medium">Latest check</th>
            </tr>
          </thead>
          <tbody>
            {packs.data.map((pack) => (
              <tr key={pack.id} data-testid="pack-row" className="border-t border-slate-200">
                <td className="px-4 py-2">
                  <Link href={`/packs/${pack.id}`} className="font-medium text-emerald-800 hover:underline">
                    {pack.company}
                  </Link>
                </td>
                <td className="px-4 py-2">{fy(pack.reporting_year)}</td>
                <td className="px-4 py-2">{pack.document_count}</td>
                <td className="px-4 py-2">{PACK_STATUS[pack.status]}</td>
                <td className="px-4 py-2">{pack.team_name}</td>
                <td className="px-4 py-2 text-slate-700">{runSummary(pack.latest_run)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {creating && <NewPackDialog onClose={() => setCreating(false)} />}
    </section>
  );
}

function Skeleton() {
  return (
    <div aria-busy="true" aria-label="Loading packs" className="space-y-2">
      {[0, 1, 2].map((i) => (
        <div key={i} data-testid="pack-skeleton" className="h-10 animate-pulse rounded bg-slate-200" />
      ))}
    </div>
  );
}
