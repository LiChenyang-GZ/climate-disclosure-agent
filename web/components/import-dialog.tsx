"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  api,
  type BrowseFile,
  type BrowseListing,
  type DocumentKind,
  type ImportResult,
  type PackDetail,
} from "@/lib/api";
import { KIND, guessKind } from "@/lib/format";
import { uploadErrorMessage } from "@/lib/upload";

interface Place {
  label: string;
  level: "root" | "site" | "folder";
  query: string;
}

interface Choice {
  file: BrowseFile;
  kind: DocumentKind;
}

type Listing = { state: "loading" } | { state: "error"; message: string } | { state: "ok"; data: BrowseListing };

const ROOT: Place = { label: "Sites", level: "root", query: "" };

function key(file: { drive_id: string; item_id: string }): string {
  return `${file.drive_id}/${file.item_id}`;
}

function query(params: Record<string, string>): string {
  return `?${new URLSearchParams(params).toString()}`;
}

/** Message for one file the API did not import (X2); codes match uploads where they overlap. */
function resultMessage(result: ImportResult, maxMb: number): string {
  if (result.status === "denied") return "Access denied in SharePoint";
  if (result.status === "failed") return result.message ?? "Import failed";
  return uploadErrorMessage({ status: 0, body: { error: result.error } }, maxMb);
}

export function ImportDialog({
  pack,
  onImported,
  onExpired,
  onClose,
}: {
  pack: PackDetail;
  onImported: () => void;
  onExpired: () => void;
  onClose: () => void;
}) {
  const [trail, setTrail] = useState<Place[]>([ROOT]);
  const [listing, setListing] = useState<Listing>({ state: "loading" });
  const [chosen, setChosen] = useState<Record<string, Choice>>({});
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<{ name: string; result: ImportResult }[] | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const place = trail[trail.length - 1];

  useEffect(() => {
    let live = true;
    api<BrowseListing>(`/integrations/microsoft/browse${place.query}`)
      .then((data) => live && setListing({ state: "ok", data }))
      .catch((error: unknown) => {
        if (!live) return;
        if (error instanceof ApiError && error.code === "microsoft_expired") {
          onExpired();
          return;
        }
        setListing({
          state: "error",
          message:
            error instanceof ApiError && error.code === "microsoft_denied"
              ? "Access denied in SharePoint"
              : "Couldn't load this folder. Try again.",
        });
      });
    return () => {
      live = false;
    };
  }, [place.query, onExpired]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function open(next: Place) {
    setListing({ state: "loading" });
    setTrail([...trail, next]);
  }

  function back(index: number) {
    if (index === trail.length - 1) return;
    setListing({ state: "loading" });
    setTrail(trail.slice(0, index + 1));
  }

  function toggle(file: BrowseFile) {
    setChosen((current) => {
      const next = { ...current };
      if (next[key(file)]) delete next[key(file)];
      else next[key(file)] = { file, kind: guessKind(file.name) };
      return next;
    });
  }

  const selected = Object.values(chosen);

  async function importFiles() {
    setBusy(true);
    setFailure(null);
    try {
      const reply = await api<ImportResult[]>(`/packs/${pack.id}/import`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          items: selected.map((c) => ({ drive_id: c.file.drive_id, item_id: c.file.item_id, kind: c.kind })),
        }),
      });
      const names = new Map(selected.map((c) => [c.file.item_id, c.file.name]));
      setResults(reply.map((result) => ({ name: names.get(result.item_id) ?? result.item_id, result })));
      // Keep only the files that can be tried again.
      const retry = new Set(
        reply.filter((r) => r.status === "failed").map((r) => r.item_id),
      );
      setChosen((current) =>
        Object.fromEntries(Object.entries(current).filter(([, c]) => retry.has(c.file.item_id))),
      );
      if (reply.some((r) => r.status === "imported")) onImported();
    } catch (error) {
      if (error instanceof ApiError && error.code === "microsoft_expired") {
        onExpired();
        return;
      }
      setFailure(
        error instanceof ApiError && error.code === "pack_parsing"
          ? "Wait for parsing to finish, then import again."
          : "Couldn't import the files. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  const duplicates = results?.filter((r) => r.result.status === "duplicate").length ?? 0;
  const imported = results?.filter((r) => r.result.status === "imported").length ?? 0;
  const problems = results?.filter((r) => !["imported", "duplicate"].includes(r.result.status)) ?? [];
  const data = listing.state === "ok" ? listing.data : null;

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-slate-900/40 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-title"
        className="flex max-h-full w-full max-w-2xl flex-col gap-4 overflow-y-auto rounded-lg bg-white p-6 shadow-xl"
      >
        <h2 id="import-title" className="text-lg font-semibold">
          Import from SharePoint
        </h2>

        <nav aria-label="Location" className="flex flex-wrap items-center gap-1 text-sm">
          {trail.map((p, index) => (
            <span key={`${index}-${p.query}`} className="flex items-center gap-1">
              {index > 0 && <span className="text-slate-400">/</span>}
              <button
                className={index === trail.length - 1 ? "font-medium" : "underline"}
                aria-current={index === trail.length - 1 ? "location" : undefined}
                onClick={() => back(index)}
              >
                {p.label}
              </button>
            </span>
          ))}
        </nav>

        <div className="min-h-32 rounded border border-slate-200 p-2 text-sm">
          {listing.state === "loading" && (
            <div aria-busy="true" className="h-16 animate-pulse rounded bg-slate-100" />
          )}
          {listing.state === "error" && (
            <p role="alert" className="text-red-800">
              {listing.message}
            </p>
          )}
          {data && (
            <ul className="space-y-1">
              {data.sites.map((site) => (
                <li key={site.id}>
                  <button
                    className="underline"
                    onClick={() =>
                      open({ label: site.name, level: "site", query: query({ site_id: site.id }) })
                    }
                  >
                    {site.name}
                  </button>
                  <span className="ml-2 text-slate-500">
                    {site.kind === "onedrive" ? "OneDrive" : "SharePoint site"}
                  </span>
                </li>
              ))}
              {data.drives.map((drive) => (
                <li key={drive.id}>
                  <button
                    className="underline"
                    onClick={() =>
                      open({ label: drive.name, level: "folder", query: query({ drive_id: drive.id }) })
                    }
                  >
                    {drive.name}
                  </button>
                  <span className="ml-2 text-slate-500">Library</span>
                </li>
              ))}
              {data.folders.map((folder) => (
                <li key={key(folder)}>
                  <button
                    className="underline"
                    onClick={() =>
                      open({
                        label: folder.name,
                        level: "folder",
                        query: query({ drive_id: folder.drive_id, item_id: folder.item_id }),
                      })
                    }
                  >
                    {folder.name}
                  </button>
                  <span className="ml-2 text-slate-500">Folder</span>
                </li>
              ))}
              {data.files.map((file) => {
                const choice = chosen[key(file)];
                return (
                  <li key={key(file)} data-testid="import-file" className="flex flex-wrap items-center gap-2">
                    <label className="flex items-center gap-2">
                      <input type="checkbox" checked={choice !== undefined} onChange={() => toggle(file)} />
                      {file.name}
                    </label>
                    {choice && (
                      <select
                        aria-label={`Type of ${file.name}`}
                        value={choice.kind}
                        onChange={(event) =>
                          setChosen((current) => ({
                            ...current,
                            [key(file)]: { file, kind: event.target.value as DocumentKind },
                          }))
                        }
                        className="rounded border border-slate-300 px-2 py-0.5"
                      >
                        {(Object.keys(KIND) as DocumentKind[]).map((kind) => (
                          <option key={kind} value={kind}>
                            {KIND[kind]}
                          </option>
                        ))}
                      </select>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {data && place.level === "folder" && data.files.length === 0 && (
            <p className="text-slate-600">No supported files in this folder.</p>
          )}
          {data && place.level === "site" && data.drives.length === 0 && (
            <p className="text-slate-600">No document libraries in this site.</p>
          )}
        </div>
        <p className="text-xs text-slate-500">Only PDF, Word (.docx) and Excel (.xlsx) files are shown.</p>

        {results && (
          <div role="status" className="space-y-1 text-sm">
            {imported > 0 && (
              <p>
                Imported {imported} file{imported === 1 ? "" : "s"}.
              </p>
            )}
            {duplicates > 0 && (
              <p>
                Skipped {duplicates} duplicate file{duplicates === 1 ? "" : "s"}
              </p>
            )}
            {problems.length > 0 && (
              <ul data-testid="import-problems" className="space-y-1">
                {problems.map(({ name, result }) => (
                  <li key={result.item_id} className="text-red-800">
                    {name}: {resultMessage(result, pack.limits.max_upload_mb)}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {failure && (
          <p role="alert" className="text-sm text-red-800">
            {failure}
          </p>
        )}

        <div className="flex items-center justify-end gap-2">
          <button className="rounded px-4 py-2 text-sm" onClick={onClose}>
            Close
          </button>
          <button
            disabled={busy || selected.length === 0}
            onClick={importFiles}
            className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {busy
              ? "Importing…"
              : `Import ${selected.length} file${selected.length === 1 ? "" : "s"}`}
          </button>
        </div>
      </div>
    </div>
  );
}
