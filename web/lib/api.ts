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

/** Where an imported file came from (X2); null for uploads. */
export interface DocumentSource {
  provider: "microsoft";
  drive_id: string;
  item_id: string;
  path: string;
  etag: string | null;
  web_url: string | null;
}

export interface PackDocument {
  id: string;
  filename: string;
  kind: DocumentKind;
  page_count: number;
  unit_count: number;
  index_status: IndexStatus;
  parts: Part[];
  source?: DocumentSource | null;
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
  salesforce_account: SalesforceAccount | null;
}

/** A Salesforce Account a pack is linked to (D85). */
export interface SalesforceAccount {
  id: string;
  name: string;
}

/** GET /integrations/salesforce; secrets never leave the API. */
export interface SalesforceStatus {
  configured: boolean;
  instance_url: string | null;
  edition: string | null;
}

export type Method = "A" | "B" | "C";

export interface RunOptions {
  checks: { id: string; title: string }[];
  models: { key: string; model: string }[];
  methods: Method[];
  estimate_per_check_usd: Record<Method, Record<string, [number, number]>>;
  max_budget_usd: number;
}

export interface RunDetail {
  id: string;
  pack_id: string;
  method: Method | "LC";
  model_key: string;
  status: RunStatus;
  budget_usd: string;
  spent_usd: string;
  tokens_in: number;
  tokens_out: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  error_message: string | null;
  check_ids: string[] | null;
  skipped_documents: { id: string; filename: string }[];
}

export interface FindingRow {
  id: string | null;
  check_id: string;
  title: string;
  status: FindingStatus;
  needs_review_reason: string | null;
}

// OneDrive / SharePoint (X2, D84).
export interface MicrosoftStatus {
  configured: boolean;
  connected: boolean;
  account: string | null;
}

export interface DeviceCode {
  user_code: string;
  verification_uri: string;
  expires_in: number;
  interval: number;
}

export type PollState = "pending" | "connected" | "cancelled" | "expired";

export interface BrowseSite {
  id: string;
  name: string;
  kind: "onedrive" | "sharepoint";
}

export interface BrowseDrive {
  id: string;
  name: string;
}

export interface BrowseFolder {
  drive_id: string;
  item_id: string;
  name: string;
}

export interface BrowseFile extends BrowseFolder {
  path: string;
  size: number | null;
}

export interface BrowseListing {
  sites: BrowseSite[];
  drives: BrowseDrive[];
  folders: BrowseFolder[];
  files: BrowseFile[];
}

export interface ImportResult {
  item_id: string;
  status: "imported" | "duplicate" | "denied" | "failed" | "rejected";
  error?: string;
  message?: string;
  document?: PackDocument;
}

export interface SyncEntry {
  document_id: string;
  filename: string;
  path: string;
  error?: string;
  message?: string;
}

export interface SyncStatus {
  changed: SyncEntry[];
  removed: SyncEntry[];
  failed: SyncEntry[];
  unchanged: number;
}

export interface SyncResult {
  document_id: string;
  status: "updated" | "unchanged" | "removed" | "denied" | "failed" | "rejected" | "duplicate";
  error?: string;
  message?: string;
}
