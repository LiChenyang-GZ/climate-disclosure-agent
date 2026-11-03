import { expect, test, type Page } from "@playwright/test";

import { RUN_OPTIONS, doc, mockApi, packDetail, runDetail, type Handler } from "./mock-api";

const ISSUES = {
  status: "parsed_with_issues",
  documents: [
    doc(),
    doc({ id: "doc-2", filename: "broken-supplement.pdf", kind: "other", index_status: "failed", parts: [] }),
  ],
};

async function openPack(
  page: Page,
  detail: Record<string, unknown> = {},
  extra: Record<string, Handler> = {},
) {
  const calls = await mockApi(page, {
    "GET /packs/pack-1": () => ({ json: packDetail(detail) }),
    "GET /run-options": () => ({ json: RUN_OPTIONS }),
    "GET /runs/run-1": () => ({ json: runDetail({ status: "queued", started_at: null }) }),
    "GET /runs/run-1/findings": () => ({ json: [] }),
    "GET /runs/run-1/events": () => ({ body: "" }),
    ...extra,
  });
  await page.goto("/packs/pack-1");
  await expect(page.getByRole("heading", { name: "Wattlebrook Energy" })).toBeVisible();
  return calls;
}

async function openDialog(page: Page) {
  await page.getByRole("button", { name: "New check" }).click();
  const dialog = page.getByRole("dialog", { name: "New check" });
  await expect(dialog.getByLabel("Model", { exact: true })).toHaveValue("luna");
  return dialog;
}

test.describe("New check button (PRD 6.3)", () => {
  for (const status of ["empty", "uploaded", "parsing", "parse_failed"]) {
    test(`disabled on ${status} with the reason`, async ({ page }) => {
      await openPack(page, { status });
      const button = page.getByRole("button", { name: "New check" });
      await expect(button).toBeDisabled();
      await expect(button).toHaveAttribute(
        "title",
        status === "parsing" ? "Wait for parsing to finish" : "Parse the pack first",
      );
    });
  }

  test("disabled while a check is running, with a link to it", async ({ page }) => {
    await openPack(page, { active_run_id: "run-1" });
    await expect(page.getByRole("button", { name: "New check" })).toHaveAttribute(
      "title",
      "A check is already running on this pack",
    );
    await page.getByRole("link", { name: "View progress" }).click();
    await expect(page).toHaveURL(/\/runs\/run-1$/);
  });

  test("Parsed with issues: says what will be skipped before the dialog", async ({ page }) => {
    await openPack(page, ISSUES);
    await page.getByRole("button", { name: "New check" }).click();
    await expect(page.getByRole("alertdialog")).toHaveText(
      /1 file couldn't be parsed and will be skipped\./,
    );
    await expect(page.getByRole("dialog", { name: "New check" })).toHaveCount(0);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByRole("dialog", { name: "New check" })).toBeVisible();
  });

  test("alex in the team can start a check", async ({ page, context }) => {
    await context.addCookies([{ name: "cda_user", value: "alex", url: "http://127.0.0.1:3100" }]);
    await openPack(page);
    await expect(page.getByRole("button", { name: "Parse" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "New check" })).toBeEnabled();
  });
});

test.describe("New check dialog (PRD 6.4, 9.1 #4)", () => {
  test("defaults and estimate", async ({ page }) => {
    await openPack(page);
    const dialog = await openDialog(page);
    await expect(dialog).toContainText("AASB S2 + ASIC 2026 review (15 checks)");
    await expect(dialog.getByLabel("Retrieval strategy", { exact: true })).toHaveValue("A");
    await expect(dialog.getByLabel("All (15)")).toBeChecked();
    await expect(dialog.getByLabel("Budget cap (USD)")).toHaveValue("2.00");
    // 15 × $0.0048–$0.035 with luna.
    await expect(dialog.getByTestId("estimate")).toHaveText("Estimated cost: $0.07–$0.53");
    await dialog.getByLabel("Model", { exact: true }).selectOption("sol");
    // The high end is capped by the budget.
    await expect(dialog.getByTestId("estimate")).toHaveText("Estimated cost: $1.43–$2.00");
    await dialog.getByLabel("Retrieval strategy", { exact: true }).selectOption("B");
    await expect(dialog.getByTestId("estimate")).toHaveText("Estimated cost: $0.36–$0.90");
  });

  for (const [value, message] of [
    ["0", "Enter a budget above $0."],
    ["-1", "Enter a budget above $0."],
    ["", "Enter a budget above $0."],
    ["11", "The budget can be at most $10.00."],
  ]) {
    test(`budget "${value}" is refused`, async ({ page }) => {
      await openPack(page);
      const dialog = await openDialog(page);
      await dialog.getByLabel("Budget cap (USD)").fill(value);
      await expect(dialog.getByText(message)).toBeVisible();
      await expect(dialog.getByRole("button", { name: "Start check" })).toBeDisabled();
    });
  }

  test("no checks ticked → Start disabled; a subset is sent in check-set order", async ({ page }) => {
    const bodies: unknown[] = [];
    await openPack(page, {}, {
      "POST /runs": (_u, route) => {
        bodies.push(route.request().postDataJSON());
        return { status: 201, json: runDetail({ status: "queued" }) };
      },
    });
    const dialog = await openDialog(page);
    await dialog.getByLabel("All (15)").uncheck();
    await expect(dialog.getByText("Pick at least one check.")).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Start check" })).toBeDisabled();
    await dialog.getByLabel(/^S1 /).check();
    await dialog.getByLabel(/^G1 /).check();
    await expect(dialog.getByTestId("estimate")).toHaveText("Estimated cost: $0.01–$0.07");
    await dialog.getByRole("button", { name: "Start check" }).click();
    await expect(page).toHaveURL(/\/runs\/run-1$/);
    expect(bodies).toEqual([
      { pack_id: "pack-1", method: "A", model_key: "luna", budget_usd: 2, check_ids: ["G1", "S1"] },
    ]);
  });

  test("double click creates one run; success opens the progress page", async ({ page }) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const keys: string[] = [];
    await openPack(page, {}, {
      "POST /runs": (_u, route) => {
        keys.push(route.request().headers()["idempotency-key"]);
        return { status: 201, json: runDetail({ status: "queued" }), delay: gate };
      },
    });
    const dialog = await openDialog(page);
    const start = dialog.getByRole("button", { name: "Start check" });
    await start.dblclick();
    await expect(dialog.getByRole("button", { name: "Starting…" })).toBeDisabled();
    release();
    await expect(page).toHaveURL(/\/runs\/run-1$/);
    expect(keys).toHaveLength(1);
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
  });

  test("network error keeps the form; Retry reuses the Idempotency-Key", async ({ page }) => {
    const keys: string[] = [];
    await openPack(page, {}, {
      "POST /runs": (_u, route) => {
        keys.push(route.request().headers()["idempotency-key"]);
        return keys.length === 1 ? { abort: true } : { status: 201, json: runDetail() };
      },
    });
    const dialog = await openDialog(page);
    await dialog.getByLabel("Budget cap (USD)").fill("1.50");
    await dialog.getByRole("button", { name: "Start check" }).click();
    await expect(dialog.getByRole("alert")).toHaveText(/Couldn't reach the server\./);
    await expect(dialog.getByLabel("Budget cap (USD)")).toHaveValue("1.50");
    await dialog.getByRole("button", { name: "Retry" }).click();
    await expect(page).toHaveURL(/\/runs\/run-1$/);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  test("API key not set → message, no navigation", async ({ page }) => {
    await openPack(page, {}, {
      "POST /runs": () => ({
        status: 409,
        json: { error: "llm_key_missing", message: "OpenAI API key isn't set" },
      }),
    });
    const dialog = await openDialog(page);
    await dialog.getByRole("button", { name: "Start check" }).click();
    await expect(dialog.getByRole("alert")).toHaveText(
      "OpenAI API key isn't set. Add OPENAI_API_KEY to .env and restart the backend.",
    );
    await expect(page).toHaveURL(/\/packs\/pack-1$/);
  });

  test("already running → message with View it", async ({ page }) => {
    await openPack(page, {}, {
      "POST /runs": () => ({
        status: 409,
        json: { error: "run_active", message: "A check is already running", run_id: "run-1" },
      }),
    });
    const dialog = await openDialog(page);
    await dialog.getByRole("button", { name: "Start check" }).click();
    await expect(dialog.getByRole("alert")).toHaveText(/A check is already running on this pack\./);
    await dialog.getByRole("link", { name: "View it" }).click();
    await expect(page).toHaveURL(/\/runs\/run-1$/);
  });
});
