import type { Page, Route } from "@playwright/test";

export type Reply = {
  status?: number;
  json?: unknown;
  /** Raw body, e.g. a text/event-stream for SSE. */
  body?: string;
  contentType?: string;
  delay?: Promise<void>;
  abort?: boolean;
};
export type Handler = (user: string, route: Route) => Reply | Promise<Reply>;

export const ME: Record<string, unknown> = {
  sarah: { handle: "sarah", role: "lead", display_name: "Sarah Chen" },
  alex: { handle: "alex", role: "analyst", display_name: "Alex Nguyen" },
};

export const TEAMS = [
  {
    id: "team-er",
    name: "Energy & Resources",
    members: [
      { handle: "sarah", display_name: "Sarah Chen" },
      { handle: "alex", display_name: "Alex Nguyen" },
    ],
  },
  { id: "team-fs", name: "Financial Services", members: [{ handle: "sarah", display_name: "Sarah Chen" }] },
];

export function pack(overrides: Record<string, unknown> = {}) {
  return {
    id: "pack-1",
    company: "Wattlebrook Energy",
    reporting_year: 2026,
    team_id: "team-er",
    team_name: "Energy & Resources",
    document_count: 2,
    status: "parsed",
    created_at: "2026-10-27T00:00:00Z",
    latest_run: null,
    ...overrides,
  };
}

export function doc(overrides: Record<string, unknown> = {}) {
  return {
    id: "doc-1",
    filename: "wattlebrook-annual-report-2026.pdf",
    kind: "annual",
    page_count: 39,
    unit_count: 412,
    index_status: "done",
    parts: [{ kind: "sustainability", first_pdf_page: 18, last_pdf_page: 31, label: null }],
    ...overrides,
  };
}

export function packDetail(overrides: Record<string, unknown> = {}) {
  return {
    ...pack(),
    status: "parsed",
    team: TEAMS[0],
    limits: { max_upload_mb: 50, max_documents: 20 },
    active_run_id: null,
    documents: [doc()],
    latest_runs: [],
    salesforce_account: null,
    ...overrides,
  };
}

/** Microsoft import is not set up unless a test says otherwise (X2). */
export const MICROSOFT_OFF = { configured: false, connected: false, account: null };

/** Salesforce is not connected unless a test says so (D85). */
export const SF_OFF = { configured: false, instance_url: null, edition: null };

const DEFAULTS: Record<string, (user: string) => Handler> = {
  "GET /me": (user) => () => ({ json: ME[user] }),
  "GET /integrations/microsoft": () => () => ({ json: MICROSOFT_OFF }),
  "GET /integrations/salesforce": () => () => ({ json: SF_OFF }),
};

/**
 * Routes every /api/* call to `handlers` keyed by "METHOD /path". The current
 * demo user is read from the cda_user cookie, as the real proxy does.
 */
export async function mockApi(page: Page, handlers: Record<string, Handler>) {
  const calls: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname.replace(/^\/api/, "")}`;
    calls.push(key);
    const cookies = await page.context().cookies();
    const user = cookies.find((c) => c.name === "cda_user")?.value ?? "sarah";
    const handler = handlers[key] ?? DEFAULTS[key]?.(user);
    if (!handler) {
      await route.fulfill({ status: 599, json: { error: "unmocked", message: key } });
      return;
    }
    const reply = await handler(user, route);
    if (reply.delay) await reply.delay;
    if (reply.abort) {
      await route.abort("failed");
      return;
    }
    if (reply.body !== undefined) {
      await route.fulfill({
        status: reply.status ?? 200,
        body: reply.body,
        contentType: reply.contentType ?? "text/event-stream",
      });
      return;
    }
    await route.fulfill({ status: reply.status ?? 200, json: reply.json ?? {} });
  });
  return calls;
}

export const CHECKS = [
  ["G1", "Board oversight of climate-related risks and opportunities"],
  ["G2", "Management's role in climate-related risks and opportunities"],
  ["S1", "Climate-related risks and opportunities and time horizons"],
  ["S2", "Effects on the business model and value chain"],
  ["S3", "Strategy and decision-making, including any transition plan"],
  ["S4", "Current and anticipated financial effects"],
  ["S5", "Scenario analysis with a 1.5°C and a ≥2.5°C scenario"],
  ["R1", "Risk management process and integration"],
  ["M1", "Scope 1, Scope 2 and Scope 3 greenhouse gas emissions"],
  ["M2", "Cross-industry metrics"],
  ["M3", "Climate targets, including regulatory targets, and progress"],
  ["A1", "No impermissible disclaimer"],
  ["A2", "Weather-related financial effects in the annual report reflected as climate risks"],
  ["A3", "Cross-references specify document and section and point to entity-published documents"],
  ["A4", "Mandatory disclosures clearly identifiable"],
].map(([id, title]) => ({ id, title }));

export const RUN_OPTIONS = {
  checks: CHECKS,
  models: [
    { key: "luna", model: "gpt-6-luna" },
    { key: "sol", model: "gpt-6.1-sol" },
  ],
  methods: ["A", "B", "C"],
  estimate_per_check_usd: {
    A: { luna: [0.0048, 0.035], sol: [0.095, 0.7] },
    B: { luna: [0.0012, 0.003], sol: [0.024, 0.06] },
    C: { luna: [0.0048, 0.035], sol: [0.095, 0.7] },
  },
  max_budget_usd: 10,
};

export function runDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: "run-1",
    pack_id: "pack-1",
    method: "A",
    model_key: "luna",
    status: "running",
    budget_usd: "2.000000",
    spent_usd: "0.120000",
    tokens_in: 40000,
    tokens_out: 2000,
    created_at: "2026-11-03T00:00:00Z",
    started_at: new Date(Date.now() - 65_000).toISOString(),
    finished_at: null,
    error: null,
    error_message: null,
    check_ids: null,
    skipped_documents: [],
    ...overrides,
  };
}

/** GET /runs/{id}/findings rows: unfinished checks have id null and status not_checked. */
export function findingRows(done: Record<string, string | [string, string]> = {}) {
  return CHECKS.map((c) => {
    const value = done[c.id];
    const [status, reason] = Array.isArray(value) ? value : [value, null];
    return {
      id: value ? `f-${c.id}` : null,
      check_id: c.id,
      title: c.title,
      status: value ? status : "not_checked",
      needs_review_reason: value ? reason : null,
    };
  });
}

/** An SSE body: [type, check_id, payload] per event, ids from 1. */
export function sse(events: [string, string | null, Record<string, unknown>][], first = 1): string {
  return events
    .map(
      ([type, checkId, payload], i) =>
        `id: ${first + i}
event: ${type}
data: ${JSON.stringify({ ...payload, check_id: checkId })}

`,
    )
    .join("");
}
