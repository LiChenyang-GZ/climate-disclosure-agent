export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly body: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

/** Calls the FastAPI backend through the Next.js proxy and returns parsed JSON. */
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, { cache: "no-store", ...init });
  } catch {
    throw new ApiError(0, "network", "Couldn't reach the server.");
  }
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    throw new ApiError(
      response.status,
      typeof body.error === "string" ? body.error : "http_error",
      typeof body.message === "string" ? body.message : response.statusText,
      body,
    );
  }
  return body as T;
}

export type Role = "lead" | "analyst";

export interface Me {
  handle: string;
  role: Role;
  display_name: string;
}

export interface Team {
  id: string;
  name: string;
  members: { handle: string; display_name: string }[];
}

export type FindingStatus =
  | "evidence_found"
  | "partial_evidence"
  | "no_evidence_found"
  | "inconsistent"
  | "needs_review"
  | "not_checked"
  | "error";

export type RunStatus = "queued" | "running" | "waiting_review" | "final" | "cancelled" | "failed";

export type PackStatus =
  | "empty"
  | "uploaded"
  | "parsing"
  | "parsed"
  | "parsed_with_issues"
  | "parse_failed";

export interface PackRow {
  id: string;
  company: string;
  reporting_year: number;
  team_id: string;
  team_name: string;
  document_count: number;
  created_at: string;
  status: PackStatus;
  latest_run: {
    id: string;
    status: RunStatus;
    created_at: string;
    counts: Partial<Record<FindingStatus, number>>;
  } | null;
}

export type DocumentKind = "sustainability" | "annual" | "other";
export type IndexStatus = "pending" | "indexing" | "done" | "failed";

export interface Part {
  kind: DocumentKind;
  first_pdf_page: number;
  last_pdf_page: number;
  label?: string | null;
}

export interface PackDocument {
  id: string;
  filename: string;
  kind: DocumentKind;
  page_count: number;
  unit_count: number;
  index_status: IndexStatus;
  parts: Part[];
}

export interface PackDetail {
  id: string;
  company: string;
  reporting_year: number;
  team_id: string;
  status: PackStatus;
  team: Team;
  limits: { max_upload_mb: number; max_documents: number };
  active_run_id: string | null;
  documents: PackDocument[];
}
