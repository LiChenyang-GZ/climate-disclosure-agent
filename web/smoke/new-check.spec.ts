import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

// Local only, against the real API and worker (D81, D83). Start both on the
// cda_ui_smoke database. The happy path needs CDA_FAKE_LLM=1 on the worker;
// "rejected key" needs the API and worker with the fake off and an invalid key.
const DATA = path.resolve(__dirname, "../../data/synthetic/wattlebrook");

async function parsedPack(page: Page): Promise<string> {
  const company = `Wattlebrook Check ${Date.now()}`;
  await page.goto("/");
  await page.getByRole("button", { name: "New pack" }).click();
  const dialog = page.getByRole("dialog", { name: "New pack" });
  await dialog.getByLabel("Company").fill(company);
  await dialog.getByLabel("Reporting year (FY)").fill("2026");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: company })).toBeVisible();
  await page
    .getByLabel("Choose files")
    .setInputFiles([path.join(DATA, "wattlebrook-annual-report-2026.pdf")]);
  await page.getByLabel("First sustainability page of wattlebrook-annual-report-2026.pdf").fill("18");
  await page.getByLabel("Last sustainability page of wattlebrook-annual-report-2026.pdf").fill("31");
  await page.getByRole("button", { name: "Upload 1 file" }).click();
  await expect(page.getByTestId("document-row")).toHaveCount(1, { timeout: 120_000 });
  await page.getByRole("button", { name: "Parse" }).click();
  await expect(page.getByTestId("pack-status")).toHaveText("Parsed", { timeout: 240_000 });
  return company;
}

async function startCheck(page: Page, checks: string[], budget = "0.05") {
  await page.getByRole("button", { name: "New check" }).click();
  const dialog = page.getByRole("dialog", { name: "New check" });
  await dialog.getByLabel("All (15)").uncheck();
  for (const id of checks) await dialog.getByLabel(new RegExp(`^${id} `)).check();
  await dialog.getByLabel("Budget cap (USD)", { exact: true }).fill(budget);
  await dialog.getByRole("button", { name: "Start check" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
}

test("fake model: start a check from the pack page and watch it finish", async ({ page }) => {
  await parsedPack(page);
  await startCheck(page, ["G1", "M1", "M3"]);
  const rows = page.getByTestId("check-row");
  await expect(rows).toHaveCount(3);
  await expect(page.getByText(/Step \d · keyword_search/).first()).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("run-status")).toHaveText(/Final|Awaiting review/, {
    timeout: 120_000,
  });
  await expect(page.getByText("Check complete.")).toBeVisible();
  await expect(rows.filter({ hasText: /Queued|Searching/ })).toHaveCount(0);
  await expect(page.getByTestId("cost")).toHaveText(/^\$0\.\d\d \/ \$0\.05$/);

  // Once the run has ended, the pack accepts a new check.
  await page.getByRole("link", { name: "Back to pack" }).click();
  await expect(page.getByRole("button", { name: "New check" })).toBeEnabled();
});

test("rejected key: the run fails with the API key message", async ({ page }) => {
  await parsedPack(page);
  await startCheck(page, ["G1"], "0.02");
  await expect(page.getByTestId("run-status")).toHaveText("Failed", { timeout: 120_000 });
  await expect(
    page.getByRole("alert").filter({ hasText: "OpenAI rejected the API key. Check OPENAI_API_KEY." }),
  ).toBeVisible();
});
