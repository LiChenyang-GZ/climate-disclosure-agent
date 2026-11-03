import path from "node:path";

import { expect, test } from "@playwright/test";

// Local only (X1): a Word and an Excel file next to the Wattlebrook PDF, then a
// fake-model check (CDA_FAKE_LLM=1 on API and worker). SMOKE_OFFICE_DIR holds
// wattlebrook-transition-plan.docx and wattlebrook-emissions-data.xlsx.
const DATA = path.resolve(__dirname, "../../data/synthetic/wattlebrook");
const OFFICE = process.env.SMOKE_OFFICE_DIR;

test("Word and Excel files upload, parse and are searched by a check", async ({ page }) => {
  test.skip(!OFFICE, "SMOKE_OFFICE_DIR is not set");
  const company = `Wattlebrook Office ${Date.now()}`;
  await page.goto("/");
  await page.getByRole("button", { name: "New pack" }).click();
  const dialog = page.getByRole("dialog", { name: "New pack" });
  await dialog.getByLabel("Company").fill(company);
  await dialog.getByLabel("Reporting year (FY)").fill("2026");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(page.getByRole("heading", { name: company })).toBeVisible();

  await page
    .getByLabel("Choose files")
    .setInputFiles([
      path.join(DATA, "wattlebrook-climate-data-supplement-2026.pdf"),
      path.join(OFFICE!, "wattlebrook-transition-plan.docx"),
      path.join(OFFICE!, "wattlebrook-emissions-data.xlsx"),
    ]);
  await page.getByRole("button", { name: "Upload 3 files" }).click();
  const rows = page.getByTestId("document-row");
  await expect(rows).toHaveCount(3, { timeout: 120_000 });
  await expect(rows.filter({ hasText: "transition-plan.docx" })).toContainText("2 sections");
  await expect(rows.filter({ hasText: "emissions-data.xlsx" })).toContainText("1 sheet");

  await page.getByRole("button", { name: "Parse" }).click();
  await expect(page.getByTestId("pack-status")).toHaveText("Parsed", { timeout: 240_000 });

  await page.getByRole("button", { name: "New check" }).click();
  const check = page.getByRole("dialog", { name: "New check" });
  await check.getByLabel("All (15)").uncheck();
  await check.getByLabel(/^M1 /).check();
  await check.getByLabel(/^M3 /).check();
  await check.getByRole("button", { name: "Start check" }).click();
  await expect(page.getByTestId("run-status")).toHaveText(/Final|Awaiting review/, {
    timeout: 120_000,
  });
});
