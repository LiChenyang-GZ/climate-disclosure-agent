import { expect, test, type Page } from "@playwright/test";

import { findingRows, mockApi, runDetail, sse, type Handler } from "./mock-api";

async function openRun(page: Page, handlers: Record<string, Handler>) {
  const calls = await mockApi(page, handlers);
  await page.goto("/runs/run-1");
  await expect(page.getByRole("heading", { name: "Check progress" })).toBeVisible();
  return calls;
}

function row(page: Page, checkId: string) {
  return page.getByTestId("check-row").filter({ hasText: new RegExp(`^${checkId}`) });
}

/** SSE that replays `events` once; reconnects (with Last-Event-ID) get nothing new. */
function stream(events: Parameters<typeof sse>[0]): Handler {
  return (_u, route) => ({
    body: route.request().headers()["last-event-id"] ? "" : sse(events),
  });
}

test.describe("Progress page (PRD 6.5, 9.1 #5)", () => {
  test("rows move from Queued to Searching to a result as events arrive", async ({ page }) => {
    let finished = false;
    await openRun(page, {
      "GET /runs/run-1": () => ({ json: runDetail() }),
      "GET /runs/run-1/findings": () => ({
        json: findingRows(finished ? { G1: "evidence_found" } : {}),
      }),
      "GET /runs/run-1/events": (_u, route) => {
        if (route.request().headers()["last-event-id"]) return { body: "" };
        finished = true;
        return {
          body: sse([
            ["run_started", null, {}],
            ["check_started", "G1", {}],
            ["tool_called", "G1", { tool: "keyword_search", query: "board oversight" }],
            ["check_finished", "G1", { status: "evidence_found" }],
            ["check_started", "S5", {}],
            ["tool_called", "S5", { tool: "list_documents", query: null }],
            ["tool_called", "S5", { tool: "keyword_search", query: "scenario 1.5°c" }],
            ["tool_called", "S5", { tool: "read_unit", query: null }],
          ]),
        };
      },
    });
    await expect(page.getByTestId("check-row")).toHaveCount(15);
    await expect(row(page, "G1")).toContainText("Evidence found");
    await expect(row(page, "S5")).toContainText("Searching");
    await expect(row(page, "S5")).toContainText("Step 3 · read_unit");
    await expect(row(page, "M1")).toContainText("Queued");
    await expect(page.getByTestId("run-status")).toHaveText("Running");
    await expect(page.getByTestId("cost")).toHaveText("$0.12 / $2.00");
    await expect(page.getByRole("progressbar", { name: "Budget used" })).toHaveAttribute(
      "aria-valuenow",
      "0.12",
    );
    await expect(page.getByTestId("elapsed")).toHaveText(/^1:0\d$/);
  });

  test("only the selected checks are listed; skipped files are named", async ({ page }) => {
    await openRun(page, {
      "GET /runs/run-1": () => ({
        json: runDetail({
          check_ids: ["G1", "S1"],
          skipped_documents: [{ id: "doc-2", filename: "broken-supplement.pdf" }],
        }),
      }),
      "GET /runs/run-1/findings": () => ({ json: findingRows() }),
      "GET /runs/run-1/events": stream([]),
    });
    await expect(page.getByTestId("check-row")).toHaveCount(2);
    await expect(page.getByTestId("skipped")).toHaveText("broken-supplement.pdf");
  });

  test("lost connection: banner, polling every 5 s, recovers and fills in", async ({ page }) => {
    let sseCalls = 0;
    let findingsCalls = 0;
    const calls = await openRun(page, {
      "GET /runs/run-1": () => ({ json: runDetail() }),
      "GET /runs/run-1/findings": () => {
        findingsCalls += 1;
        return { json: findingRows(findingsCalls > 1 ? { G1: "partial_evidence" } : {}) };
      },
      "GET /runs/run-1/events": () => {
        sseCalls += 1;
        // First connection drops at once; the browser's reconnect gets the events.
        if (sseCalls === 1) return { abort: true };
        return { body: sse([["check_started", "S1", {}]]) };
      },
    });
    await expect(page.getByText("Live updates paused. Reconnecting…")).toBeVisible();
    // Polling picks up G1 while live updates are down.
    await expect(row(page, "G1")).toContainText("Partial evidence", { timeout: 8000 });
    await expect(row(page, "S1")).toContainText("Searching", { timeout: 8000 });
    expect(calls.filter((c) => c === "GET /runs/run-1/findings").length).toBeGreaterThan(1);
  });

  test("Cancel asks first; then Cancelled and unfinished rows Not checked", async ({ page }) => {
    let cancelled = false;
    await openRun(page, {
      "GET /runs/run-1": () => ({
        json: runDetail(cancelled ? { status: "cancelled", finished_at: new Date().toISOString() } : {}),
      }),
      "GET /runs/run-1/findings": () => ({
        json: findingRows(
          cancelled ? { G1: "evidence_found", S1: ["not_checked", "cancelled"] } : { G1: "evidence_found" },
        ),
      }),
      "GET /runs/run-1/events": stream([]),
      "POST /runs/run-1/cancel": () => {
        cancelled = true;
        return { json: { status: "cancelled" } };
      },
    });
    await page.getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByRole("alertdialog")).toHaveText(/Stop this check\? Finished results are kept\./);
    await page.getByRole("button", { name: "Keep running" }).click();
    expect(cancelled).toBe(false);
    await page.getByRole("button", { name: "Cancel" }).click();
    await page.getByRole("button", { name: "Stop check" }).click();
    await expect(page.getByTestId("run-status")).toHaveText("Cancelled");
    await expect(row(page, "G1")).toContainText("Evidence found");
    await expect(row(page, "S1")).toContainText("Not checked");
    await expect(row(page, "M3")).toContainText("Not checked");
    await expect(page.getByRole("button", { name: "Cancel" })).toHaveCount(0);
  });

  test("Failed: the API key was rejected", async ({ page }) => {
    await openRun(page, {
      "GET /runs/run-1": () => ({
        json: runDetail({
          status: "failed",
          error: "llm_auth",
          error_message: "The OpenAI API key is missing or was rejected.",
          finished_at: new Date().toISOString(),
        }),
      }),
      "GET /runs/run-1/findings": () => ({ json: findingRows() }),
    });
    await expect(page.getByTestId("run-status")).toHaveText("Failed");
    await expect(
      page.getByRole("alert").filter({ hasText: "OpenAI rejected the API key. Check OPENAI_API_KEY." }),
    ).toBeVisible();
    await expect(row(page, "G1")).toContainText("Not checked");
  });

  test("Stopped: budget keeps finished results", async ({ page }) => {
    await openRun(page, {
      "GET /runs/run-1": () => ({
        json: runDetail({ status: "final", spent_usd: "0.150000", budget_usd: "0.150000", finished_at: new Date().toISOString() }),
      }),
      "GET /runs/run-1/findings": () => ({
        json: findingRows({
          G1: "evidence_found",
          G2: ["not_checked", "budget_exhausted"],
          S1: ["not_checked", "budget_exhausted"],
        }),
      }),
    });
    await expect(page.getByTestId("run-status")).toHaveText("Stopped: budget");
    await expect(page.getByText(/reached its budget/)).toBeVisible();
    await expect(row(page, "G1")).toContainText("Evidence found");
    await expect(row(page, "G2")).toContainText("Not checked");
  });

  test("one check errors, the rest carry on", async ({ page }) => {
    await openRun(page, {
      "GET /runs/run-1": () => ({ json: runDetail() }),
      "GET /runs/run-1/findings": () => ({
        json: findingRows({ G1: ["error", "llm_unavailable"], G2: "evidence_found" }),
      }),
      "GET /runs/run-1/events": stream([["check_started", "S1", {}]]),
    });
    await expect(row(page, "G1")).toContainText("⚠ Error");
    await expect(row(page, "G1")).toContainText("The model service is temporarily unavailable.");
    await expect(row(page, "G2")).toContainText("Evidence found");
    await expect(row(page, "S1")).toContainText("Searching");
  });

  test("run finishes live → Check complete with a link to the report", async ({ page }) => {
    let done = false;
    await openRun(page, {
      "GET /runs/run-1": () => ({
        json: runDetail(done ? { status: "final", finished_at: new Date().toISOString() } : {}),
      }),
      "GET /runs/run-1/findings": () => ({
        json: findingRows(done ? Object.fromEntries(["G1", "G2"].map((c) => [c, "evidence_found"])) : {}),
      }),
      "GET /runs/run-1/events": (_u, route) => {
        if (route.request().headers()["last-event-id"]) return { body: "" };
        done = true;
        return { body: sse([["run_final", null, {}]]) };
      },
    });
    await expect(page.getByTestId("run-status")).toHaveText("Final");
    await expect(page.getByText("Check complete.")).toBeVisible();
    await expect(page.getByText("Live updates paused. Reconnecting…")).toHaveCount(0);
    await page.getByRole("link", { name: "Open the report" }).click();
    await expect(page).toHaveURL(/\/runs\/run-1\/report$/);
    await expect(page.getByRole("heading", { name: "Report" })).toBeVisible();
  });

  test("alex outside the team is sent to Packs", async ({ page, context }) => {
    await context.addCookies([{ name: "cda_user", value: "alex", url: "http://127.0.0.1:3100" }]);
    await mockApi(page, {
      "GET /runs/run-1": () => ({ status: 404, json: { error: "not_found", message: "Resource not found" } }),
      "GET /runs/run-1/findings": () => ({ status: 404, json: { error: "not_found", message: "Resource not found" } }),
      "GET /packs": () => ({ json: [] }),
    });
    await page.goto("/runs/run-1");
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByText("You don't have access to that page as alex.")).toBeVisible();
  });
});
