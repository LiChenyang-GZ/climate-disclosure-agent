import { expect, test, type Page, type Route } from "@playwright/test";

import { doc, mockApi, packDetail, type Handler } from "./mock-api";

// Word and Excel uploads (PRD 6.3, X1).
function file(name: string) {
  return { name, mimeType: "application/octet-stream", buffer: Buffer.from("PK\u0003\u0004test") };
}

async function openPack(page: Page, detail: Record<string, unknown>, extra: Record<string, Handler> = {}) {
  await mockApi(page, { "GET /packs/pack-1": () => ({ json: packDetail(detail) }), ...extra });
  await page.goto("/packs/pack-1");
  await expect(page.getByRole("heading", { name: "Wattlebrook Energy" })).toBeVisible();
}

test("accepts .docx and .xlsx without page ranges; .txt is still refused", async ({ page }) => {
  const sent: string[] = [];
  await openPack(page, { status: "empty", documents: [] }, {
    "POST /packs/pack-1/documents": (_u, route: Route) => {
      sent.push(route.request().postData() ?? "");
      return { status: 201, json: doc() };
    },
  });
  await page
    .getByLabel("Choose files")
    .setInputFiles([file("Climate-Plan.DOCX"), file("emissions-data.xlsx"), file("readme.txt")]);
  const items = page.getByTestId("upload-item");
  await expect(items.filter({ hasText: "readme.txt" })).toContainText("Unsupported format");
  await page.getByLabel("Type of emissions-data.xlsx").selectOption("annual");
  await expect(page.getByLabel("First sustainability page of emissions-data.xlsx")).toHaveCount(0);
  await expect(page.getByLabel("First sustainability page of Climate-Plan.DOCX")).toHaveCount(0);
  await page.getByRole("button", { name: "Upload 2 files" }).click();
  await expect.poll(() => sent.length).toBe(2);
  expect(sent.join()).toContain('filename="Climate-Plan.DOCX"');
  expect(sent.join()).toContain('filename="emissions-data.xlsx"');
});

test("server reasons for Office files", async ({ page }) => {
  const codes: Record<string, string> = {
    "locked.docx": "encrypted",
    "broken.xlsx": "invalid_file",
    "blank.docx": "empty_document",
    "huge.xlsx": "too_large",
  };
  await openPack(page, { status: "empty", documents: [] }, {
    "POST /packs/pack-1/documents": (_u, route: Route) => {
      const body = route.request().postData() ?? "";
      const name = Object.keys(codes).find((n) => body.includes(`filename="${n}"`)) ?? "";
      return { status: 422, json: { error: codes[name], message: "File could not be parsed" } };
    },
  });
  await page.getByLabel("Choose files").setInputFiles(Object.keys(codes).map(file));
  await page.getByRole("button", { name: "Upload 4 files" }).click();
  const items = page.getByTestId("upload-item");
  await expect(items.filter({ hasText: "locked.docx" })).toContainText("Password protected");
  await expect(items.filter({ hasText: "broken.xlsx" })).toContainText("Couldn't read this file");
  await expect(items.filter({ hasText: "blank.docx" })).toContainText("No text found in this file");
  await expect(items.filter({ hasText: "huge.xlsx" })).toContainText(
    "Too large to read (max 20,000 cells)",
  );
  await expect(items.filter({ hasText: "Retry" })).toHaveCount(0);
});

test("document table shows sections and sheets; no page editing for Office files", async ({ page }) => {
  await openPack(page, {
    documents: [
      doc(),
      doc({ id: "d2", filename: "plan.docx", kind: "other", page_count: 4, parts: [] }),
      doc({ id: "d3", filename: "data.xlsx", kind: "annual", page_count: 1, parts: [] }),
    ],
  });
  const rows = page.getByTestId("document-row");
  await expect(rows.filter({ hasText: "plan.docx" })).toContainText("4 sections");
  await expect(rows.filter({ hasText: "data.xlsx" })).toContainText("1 sheet");
  await expect(rows.filter({ hasText: "data.xlsx" }).getByRole("button", { name: "Edit pages" })).toHaveCount(0);
  await expect(
    rows.filter({ hasText: "wattlebrook-annual-report-2026.pdf" }).getByRole("button", { name: "Edit pages" }),
  ).toBeVisible();
});
