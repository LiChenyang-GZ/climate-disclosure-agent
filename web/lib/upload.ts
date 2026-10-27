import type { DocumentKind, Part } from "./api";

export interface UploadResult {
  /** 0 when the request never got a response (network failure). */
  status: number;
  body: Record<string, unknown>;
}

/**
 * Uploads one PDF through the proxy. XMLHttpRequest instead of fetch because
 * fetch cannot report upload progress.
 */
export function uploadDocument(
  packId: string,
  file: File,
  kind: DocumentKind,
  parts: Part[],
  onProgress: (fraction: number) => void,
): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("kind", kind);
  form.append("parts", JSON.stringify(parts));
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/packs/${packId}/documents`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.upload.onload = () => onProgress(1);
    xhr.onload = () => {
      let body: Record<string, unknown> = {};
      try {
        body = JSON.parse(xhr.responseText) as Record<string, unknown>;
      } catch {
        // Non-JSON error page: keep the status only.
      }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => resolve({ status: 0, body: {} });
    xhr.send(form);
  });
}

/** Messages for errors the API returns when it rejects an upload (PRD 6.3). */
export function uploadErrorMessage(result: UploadResult, maxMb: number): string {
  switch (result.body.error) {
    case "no_text_layer":
      return "No text layer (scanned PDFs aren't supported)";
    case "encrypted":
      return "Password protected";
    case "invalid_pdf":
    case "page_timeout":
      return "Couldn't read this file";
    case "too_many_pages":
      return "Too many pages";
    case "upload_too_large":
      return `File too large (max ${maxMb} MB)`;
    case "invalid_parts":
      return "Page range is outside this PDF";
    case "pack_full":
      return "A pack can hold up to 20 files";
    case "pack_parsing":
      return "Wait for parsing to finish, then upload again";
    default:
      return "Upload failed";
  }
}

export interface PageRange {
  from: string;
  to: string;
}

/** null when the range is empty (no part) or valid. */
export function rangeError(range: PageRange): string | null {
  const from = range.from.trim();
  const to = range.to.trim();
  if (from === "" && to === "") return null;
  const a = Number(from);
  const b = Number(to);
  if (!Number.isInteger(a) || !Number.isInteger(b) || a < 1 || b < 1) {
    return "Enter both PDF page numbers.";
  }
  if (a > b) return "The first page must not be after the last page.";
  return null;
}

/** Sustainability part from a page range; keeps the document's other parts. */
export function withSustainabilityPart(existing: Part[], range: PageRange): Part[] {
  const others = existing.filter((p) => p.kind !== "sustainability");
  if (range.from.trim() === "" && range.to.trim() === "") return others;
  return [
    ...others,
    {
      kind: "sustainability",
      first_pdf_page: Number(range.from),
      last_pdf_page: Number(range.to),
      label: "Sustainability report",
    },
  ];
}
