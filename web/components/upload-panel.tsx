"use client";

import { useRef, useState } from "react";

import type { DocumentKind, PackDetail } from "@/lib/api";
import { KIND, guessKind } from "@/lib/format";
import {
  type PageRange,
  rangeError,
  uploadDocument,
  uploadErrorMessage,
  withSustainabilityPart,
} from "@/lib/upload";

type ItemState = "ready" | "rejected" | "uploading" | "reading" | "failed" | "error";

interface Item {
  key: number;
  file: File;
  kind: DocumentKind;
  range: PageRange;
  state: ItemState;
  message?: string;
  progress: number;
}

let nextKey = 0;

export function UploadPanel({ pack, onUploaded }: { pack: PackDetail; onUploaded: () => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [busy, setBusy] = useState(false);
  const [skipped, setSkipped] = useState(0);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const { max_upload_mb: maxMb, max_documents: maxDocs } = pack.limits;
  const parsing = pack.status === "parsing";

  function update(key: number, change: Partial<Item>) {
    setItems((all) => all.map((item) => (item.key === key ? { ...item, ...change } : item)));
  }

  function add(files: FileList | File[]) {
    setSkipped(0);
    setItems((current) => {
      let room = maxDocs - pack.documents.length - current.filter((i) => i.state !== "rejected").length;
      const added = Array.from(files).map((file): Item => {
        const item: Item = {
          key: nextKey++,
          file,
          kind: guessKind(file.name),
          range: { from: "", to: "" },
          state: "ready",
          progress: 0,
        };
        if (!file.name.toLowerCase().endsWith(".pdf")) {
          return { ...item, state: "rejected", message: "Unsupported format" };
        }
        if (file.size > maxMb * 1024 * 1024) {
          return { ...item, state: "rejected", message: `File too large (max ${maxMb} MB)` };
        }
        if (room <= 0) {
          return { ...item, state: "rejected", message: `A pack can hold up to ${maxDocs} files` };
        }
        room -= 1;
        return item;
      });
      return [...current, ...added];
    });
  }

  async function send(targets: Item[]) {
    setBusy(true);
    let duplicates = 0;
    let uploaded = 0;
    for (const item of targets) {
      update(item.key, { state: "uploading", progress: 0, message: undefined });
      const result = await uploadDocument(
        pack.id,
        item.file,
        item.kind,
        withSustainabilityPart([], item.range),
        (fraction) =>
          update(item.key, fraction >= 1 ? { state: "reading", progress: 1 } : { progress: fraction }),
      );
      if (result.status === 201) {
        uploaded += 1;
        setItems((all) => all.filter((i) => i.key !== item.key));
      } else if (result.status === 409 && result.body.error === "duplicate_document") {
        duplicates += 1;
        setItems((all) => all.filter((i) => i.key !== item.key));
      } else if (result.status === 0 || result.status >= 500) {
        update(item.key, { state: "failed", message: "Upload failed" });
      } else {
        update(item.key, { state: "error", message: uploadErrorMessage(result, maxMb) });
      }
    }
    setSkipped(duplicates);
    setBusy(false);
    if (uploaded > 0) onUploaded();
  }

  const ready = items.filter((i) => i.state === "ready");
  const invalid = ready.some((i) => i.kind !== "sustainability" && rangeError(i.range) !== null);

  return (
    <section aria-label="Upload files" className="rounded border border-slate-200 bg-white p-4">
      <div
        data-testid="drop-zone"
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!parsing) add(event.dataTransfer.files);
        }}
        className={`flex flex-col items-center gap-2 rounded border-2 border-dashed p-6 text-sm text-slate-600 ${
          dragging ? "border-emerald-600 bg-emerald-50" : "border-slate-300"
        }`}
      >
        <p>Drop PDF reports here (up to {maxMb} MB each, {maxDocs} files per pack)</p>
        <button
          type="button"
          disabled={parsing}
          className="rounded border border-slate-300 px-3 py-1 disabled:opacity-50"
          onClick={() => input.current?.click()}
        >
          Choose files
        </button>
        <input
          ref={input}
          type="file"
          multiple
          aria-label="Choose files"
          className="hidden"
          onChange={(event) => {
            if (event.target.files) add(event.target.files);
            event.target.value = "";
          }}
        />
        {parsing && <p>Wait for parsing to finish before uploading more files.</p>}
      </div>

      {skipped > 0 && (
        <p role="status" className="mt-3 text-sm text-slate-700">
          Skipped {skipped} duplicate file{skipped === 1 ? "" : "s"}
        </p>
      )}

      {items.length > 0 && (
        <ul className="mt-4 space-y-2">
          {items.map((item) => (
            <UploadRow
              key={item.key}
              item={item}
              busy={busy}
              onChange={(change) => update(item.key, change)}
              onRemove={() => setItems((all) => all.filter((i) => i.key !== item.key))}
              onRetry={() => send([item])}
            />
          ))}
        </ul>
      )}

      {ready.length > 0 && (
        <div className="mt-4 flex justify-end">
          <button
            disabled={busy || invalid || parsing}
            onClick={() => send(ready)}
            className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {busy ? "Uploading…" : `Upload ${ready.length} file${ready.length === 1 ? "" : "s"}`}
          </button>
        </div>
      )}
    </section>
  );
}

function UploadRow({
  item,
  busy,
  onChange,
  onRemove,
  onRetry,
}: {
  item: Item;
  busy: boolean;
  onChange: (change: Partial<Item>) => void;
  onRemove: () => void;
  onRetry: () => void;
}) {
  const problem = item.kind !== "sustainability" ? rangeError(item.range) : null;
  const bad = item.state === "rejected" || item.state === "error" || item.state === "failed";
  return (
    <li
      data-testid="upload-item"
      className={`rounded border p-3 text-sm ${bad ? "border-red-300 bg-red-50" : "border-slate-200"}`}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-medium">{item.file.name}</span>
        {item.state === "ready" && (
          <select
            aria-label={`Type of ${item.file.name}`}
            value={item.kind}
            onChange={(event) => onChange({ kind: event.target.value as DocumentKind })}
            className="rounded border border-slate-300 px-2 py-1"
          >
            {(Object.keys(KIND) as DocumentKind[]).map((kind) => (
              <option key={kind} value={kind}>
                {KIND[kind]}
              </option>
            ))}
          </select>
        )}
        {item.state === "uploading" && (
          <progress aria-label={`Uploading ${item.file.name}`} max={1} value={item.progress} />
        )}
        {item.state === "reading" && <span className="text-slate-600">Reading PDF…</span>}
        {bad && <span className="text-red-800">{item.message}</span>}
        {item.state === "failed" && (
          <button className="underline" disabled={busy} onClick={onRetry}>
            Retry
          </button>
        )}
        {(item.state === "ready" || bad) && (
          <button className="ml-auto text-slate-500 underline" disabled={busy} onClick={onRemove}>
            Remove
          </button>
        )}
      </div>
      {item.state === "ready" && item.kind !== "sustainability" && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-slate-600">
          <span>Sustainability report inside this file, PDF pages</span>
          <input
            aria-label={`First sustainability page of ${item.file.name}`}
            inputMode="numeric"
            className="w-16 rounded border border-slate-300 px-2 py-1"
            value={item.range.from}
            onChange={(event) => onChange({ range: { ...item.range, from: event.target.value } })}
          />
          <span>to</span>
          <input
            aria-label={`Last sustainability page of ${item.file.name}`}
            inputMode="numeric"
            className="w-16 rounded border border-slate-300 px-2 py-1"
            value={item.range.to}
            onChange={(event) => onChange({ range: { ...item.range, to: event.target.value } })}
          />
          <span className="text-slate-500">(optional)</span>
          {problem && <span className="text-red-700">{problem}</span>}
        </div>
      )}
    </li>
  );
}
