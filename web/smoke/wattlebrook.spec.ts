import path from "node:path";

import { expect, test } from "@playwright/test";

const DATA = path.resolve(__dirname, "../../data/synthetic/wattlebrook");

test("sarah creates the Wattlebrook pack, uploads, parses; alex can see it", async ({
  page,
  context,
}) => {
  const company = `Wattlebrook Smoke ${Date.now()}`;
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
      path.join(DATA, "wattlebrook-annual-report-2026.pdf"),
      path.join(DATA, "wattlebrook-climate-data-supplement-2026.pdf"),
    ]);
  await page.getByLabel("Type of wattlebrook-climate-data-supplement-2026.pdf").selectOption("other");
  await page.getByLabel("First sustainability page of wattlebrook-annual-report-2026.pdf").fill("18");
  await page.getByLabel("Last sustainability page of wattlebrook-annual-report-2026.pdf").fill("31");
  await page.getByRole("button", { name: "Upload 2 files" }).click();
  const rows = page.getByTestId("document-row");
  await expect(rows).toHaveCount(2, { timeout: 120_000 });
  await expect(rows.filter({ hasText: "annual-report" })).toContainText("18–31");
  await expect(page.getByTestId("pack-status")).toHaveText("Uploaded");

  await page.getByRole("button", { name: "Parse" }).click();
  await expect(page.getByTestId("pack-status")).toHaveText("Parsed", { timeout: 240_000 });
  await expect(rows.filter({ hasText: "Parsed" })).toHaveCount(2);
  await expect(page.getByText(/No sustainability report in this pack/)).toHaveCount(0);

  // Energy & Resources includes alex.
  await page.getByLabel("Demo user").selectOption("alex");
  await expect(page.getByRole("heading", { name: company })).toBeVisible();
  await expect(page.getByRole("button", { name: "Parse" })).toHaveCount(0);
  expect((await context.cookies()).find((c) => c.name === "cda_user")?.value).toBe("alex");
});
