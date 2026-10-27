"use client";

import { useState } from "react";

import { ApiError, api, type PackDetail, type PackDocument } from "@/lib/api";
import { INDEX_STATUS, KIND } from "@/lib/format";
import { type PageRange, rangeError, withSustainabilityPart } from "@/lib/upload";

export function DocumentsTable({
  pack,
  canEdit,
  onChanged,
}: {
  pack: PackDetail;
  canEdit: boolean;
  onChanged: () => void;
}) {
  const [deleting, setDeleting] = useState<PackDocument | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  if (pack.documents.length === 0) {
    return <p className="text-slate-600">No files yet.</p>;
  }
  const runReason = pack.active_run_id ? "A check is running on this pack." : undefined;

  return (
    <>
      <table className="w-full rounded border border-slate-200 bg-white text-sm">
        <thead className="bg-slate-100 text-left text-slate-600">
          <tr>
            <th className="px-4 py-2 font-medium">File</th>
            <th className="px-4 py-2 font-medium">Type</th>
            <th className="px-4 py-2 font-medium">Pages</th>
            <th className="px-4 py-2 font-medium">Sustainability pages</th>
            <th className="px-4 py-2 font-medium">Status</th>
            <th className="px-4 py-2 font-medium">Units</th>
            {canEdit && <th className="px-4 py-2" />}
          </tr>
        </thead>
        <tbody>
          {pack.documents.map((doc) => {
            const part = doc.parts.find((p) => p.kind === "sustainability");
            return (
              <tr key={doc.id} data-testid="document-row" className="border-t border-slate-200 align-top">
                <td className="px-4 py-2 font-medium">{doc.filename}</td>
                <td className="px-4 py-2">{KIND[doc.kind]}</td>
                <td className="px-4 py-2">{doc.page_count}</td>
                <td className="px-4 py-2">
                  {editing === doc.id ? (
                    <PartsEditor
                      pack={pack}
                      doc={doc}
                      onDone={(saved) => {
                        setEditing(null);
                        if (saved) onChanged();
                      }}
                    />
                  ) : doc.kind === "sustainability" ? (
                    "Whole file"
                  ) : part ? (
                    `${part.first_pdf_page}–${part.last_pdf_page}`
                  ) : (
                    "—"
                  )}
                </td>
                <td
                  className={`px-4 py-2 ${doc.index_status === "failed" ? "font-medium text-red-700" : ""}`}
                >
                  {INDEX_STATUS[doc.index_status]}
                </td>
                <td className="px-4 py-2">{doc.index_status === "done" ? doc.unit_count : "—"}</td>
                {canEdit && (
                  <td className="space-x-3 whitespace-nowrap px-4 py-2 text-right">
                    {doc.kind !== "sustainability" && editing !== doc.id && (
                      <button className="underline" onClick={() => setEditing(doc.id)}>
                        Edit pages
                      </button>
                    )}
                    <button
                      aria-label={`Delete ${doc.filename}`}
                      title={runReason}
                      disabled={runReason !== undefined}
                      className="text-red-700 underline disabled:cursor-not-allowed disabled:text-slate-400"
                      onClick={() => setDeleting(doc)}
                    >
                      Delete
                    </button>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      {deleting && (
        <DeleteDialog
          pack={pack}
          doc={deleting}
          onClose={(deleted) => {
            setDeleting(null);
            if (deleted) onChanged();
          }}
        />
      )}
    </>
  );
}

function PartsEditor({
  pack,
  doc,
  onDone,
}: {
  pack: PackDetail;
  doc: PackDocument;
  onDone: (saved: boolean) => void;
}) {
  const part = doc.parts.find((p) => p.kind === "sustainability");
  const [range, setRange] = useState<PageRange>({
    from: part ? String(part.first_pdf_page) : "",
    to: part ? String(part.last_pdf_page) : "",
  });
  const [failure, setFailure] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const problem =
    rangeError(range) ??
    (Number(range.to) > doc.page_count ? `This file has ${doc.page_count} pages.` : null);

  async function save() {
    setSaving(true);
    setFailure(null);
    try {
      await api(`/packs/${pack.id}/documents/${doc.id}/parts`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(withSustainabilityPart(doc.parts, range)),
      });
      onDone(true);
    } catch (error) {
      setFailure(
        error instanceof ApiError && error.code === "invalid_parts"
          ? "Page range is outside this PDF or overlaps another part."
          : "Couldn't save the page range. Try again.",
      );
      setSaving(false);
    }
  }

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1">
        <input
          aria-label={`First sustainability page of ${doc.filename}`}
          inputMode="numeric"
          className="w-14 rounded border border-slate-300 px-1 py-0.5"
          value={range.from}
          onChange={(event) => setRange({ ...range, from: event.target.value })}
        />
        <span>to</span>
        <input
          aria-label={`Last sustainability page of ${doc.filename}`}
          inputMode="numeric"
          className="w-14 rounded border border-slate-300 px-1 py-0.5"
          value={range.to}
          onChange={(event) => setRange({ ...range, to: event.target.value })}
        />
      </div>
      {(problem || failure) && <p className="text-red-700">{problem ?? failure}</p>}
      <div className="space-x-2">
        <button className="underline" disabled={saving || problem !== null} onClick={save}>
          Save
        </button>
        <button className="text-slate-500 underline" onClick={() => onDone(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function DeleteDialog({
  pack,
  doc,
  onClose,
}: {
  pack: PackDetail;
  doc: PackDocument;
  onClose: (deleted: boolean) => void;
}) {
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function remove() {
    setBusy(true);
    try {
      await api(`/packs/${pack.id}/documents/${doc.id}`, { method: "DELETE" });
      onClose(true);
    } catch (error) {
      setFailure(
        error instanceof ApiError && error.code === "run_active"
          ? "A check is running on this pack."
          : "Couldn't delete the file. Try again.",
      );
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-slate-900/40 p-4">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="delete-title"
        className="w-full max-w-sm space-y-4 rounded-lg bg-white p-6 shadow-xl"
      >
        <h2 id="delete-title" className="font-semibold">
          Delete {doc.filename}?
        </h2>
        <p className="text-sm text-slate-700">Delete this file? Past reports keep their evidence text.</p>
        {failure && <p className="text-sm text-red-700">{failure}</p>}
        <div className="flex justify-end gap-2">
          <button className="rounded px-4 py-2 text-sm" onClick={() => onClose(false)}>
            Cancel
          </button>
          <button
            disabled={busy}
            onClick={remove}
            className="rounded bg-red-700 px-4 py-2 text-sm font-medium text-white disabled:bg-slate-300"
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}
