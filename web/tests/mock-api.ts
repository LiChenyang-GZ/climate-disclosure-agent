import type { Page, Route } from "@playwright/test";

export type Reply = { status?: number; json?: unknown; delay?: Promise<void> };
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
    created_at: "2026-10-27T00:00:00Z",
    latest_run: null,
    ...overrides,
  };
}

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
    const handler =
      handlers[key] ?? (key === "GET /me" ? () => ({ json: ME[user] }) : undefined);
    if (!handler) {
      await route.fulfill({ status: 599, json: { error: "unmocked", message: key } });
      return;
    }
    const reply = await handler(user, route);
    if (reply.delay) await reply.delay;
    await route.fulfill({ status: reply.status ?? 200, json: reply.json ?? {} });
  });
  return calls;
}
