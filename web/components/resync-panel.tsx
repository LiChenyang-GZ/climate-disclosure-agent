"use client";

import { useEffect, useState } from "react";

import { ApiError, api, type SyncResult, type SyncStatus } from "@/lib/api";

type Load = { state: "loading" } | { state: "error" } | { state: "ok"; data: SyncStatus };

/**
 * Compares imported files with SharePoint (X2). A changed file is imported again as a new
 * document and the old one is retired; a file removed in SharePoint is only marked.
 */
export function ResyncPanel({
  packId,
  onChanged,
  onExpired,
}: {
  packId: string;
  onChanged: () => void;
  onExpired: () => void;
}) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<SyncResult[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api<SyncStatus>(`/packs/${packId}/sync`)
      .then((data) => live && setLoad({ state: "ok", data }))
      .catch((error: unknown) => {
        if (!live) return;
        if (error instanceof ApiError && error.code === "microsoft_expired") onExpired();
        else setLoad({ state: "error" });
      });
    return () => {
      live = false;
    };
  }, [packId, onExpired]);

  async function update(ids: string[]) {
    setBusy(true);
    setFailure(null);
    try {
      const results = await api<SyncResult[]>(`/packs/${packId}/sync`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ document_ids: ids }),
      });
      setDone(results);
      onChanged();
    } catch (error) {
      if (error instanceof ApiError && error.code === "microsoft_expired") {
        onExpired();
        return;
      }
      setFailure(
        error instanceof ApiError && error.code === "run_active"
          ? "A check is running on this pack. Update after it finishes."
          : error instanceof ApiError && error.code === "pack_parsing"
            ? "Wait for parsing to finish, then update again."
            : "Couldn't update the files. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (load.state === "loading") {
    return <p className="text-sm text-slate-600">Checking SharePoint for changes…</p>;
  }
  if (load.state === "error") {
    return (
      <p role="alert" className="text-sm text-red-800">
        Couldn&apos;t check SharePoint for changes. Try again.
      </p>
    );
  }
  const { changed, removed, failed } = load.data;
  const updated = done?.filter((r) => r.status === "updated").length ?? 0;
  const notUpdated = done?.filter((r) => r.status !== "updated" && r.status !== "unchanged") ?? [];

  return (
    <div data-testid="resync" className="space-y-2 text-sm">
      {changed.length === 0 && removed.length === 0 && failed.length === 0 && (
        <p role="status">Everything is up to date.</p>
      )}
      {changed.length > 0 && done === null && (
        <div className="space-y-2">
          <p>
            {changed.length} file{changed.length === 1 ? "" : "s"} changed in SharePoint:
          </p>
          <ul className="list-inside list-disc">
            {changed.map((entry) => (
              <li key={entry.document_id}>
                {entry.filename} <span className="text-slate-500">({entry.path})</span>
              </li>
            ))}
          </ul>
          <button
            disabled={busy}
            onClick={() => update(changed.map((c) => c.document_id))}
            className="rounded bg-emerald-700 px-3 py-1 font-medium text-white disabled:bg-slate-300"
          >
            {busy ? "Updating…" : "Update"}
          </button>
        </div>
      )}
      {done && (
        <p role="status">
          Updated {updated} file{updated === 1 ? "" : "s"}. Parse the pack to read the new
          versions; past reports keep their evidence.
          {notUpdated.length > 0 && ` ${notUpdated.length} couldn't be updated.`}
        </p>
      )}
      {removed.length > 0 && (
        <ul className="space-y-1">
          {removed.map((entry) => (
            <li key={entry.document_id} data-testid="removed">
              {entry.filename}:{" "}
              <span className="font-medium text-amber-800">Removed in SharePoint</span>
              <span className="text-slate-500"> (kept in this pack)</span>
            </li>
          ))}
        </ul>
      )}
      {failed.length > 0 && (
        <ul className="space-y-1">
          {failed.map((entry) => (
            <li key={entry.document_id} className="text-red-800">
              {entry.filename}: {entry.message ?? "Couldn't check this file"}
            </li>
          ))}
        </ul>
      )}
      {failure && (
        <p role="alert" className="text-red-800">
          {failure}
        </p>
      )}
    </div>
  );
}
