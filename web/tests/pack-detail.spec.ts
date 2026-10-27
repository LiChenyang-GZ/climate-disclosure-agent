import { expect, test, type Page, type Route } from "@playwright/test";

import { doc, mockApi, packDetail, type Handler } from "./mock-api";

const PDF = Buffer.from("%PDF-1.7\n%test\n");

function file(name: string, buffer: Buffer = PDF) {
  return { name, mimeType: name.endsWith(".pdf") ? "application/pdf" : "text/plain", buffer };
}

async function openPack(page: Page, detail: Record<string, unknown>, extra: Record<string, Handler> = {}) {
  await mockApi(page, { "GET /packs/pack-1": () => ({ json: packDetail(detail) }), ...extra });
  await page.goto("/packs/pack-1");
  await expect(page.getByRole("heading", { name: "Wattlebrook Energy" })).toBeVisible();
}

const EMPTY = { status: "empty", documents: [] };

test.describe("Upload (PRD 6.3, 9.1 #2)", () => {
  test("guesses the type from the file name and lets the user change it", async ({ page }) => {
    await openPack(page, EMPTY);
    await page
      .getByLabel("Choose files")
      .setInputFiles([
        file("wattlebrook-sustainability-2026.pdf"),
        file("Annual_Report_2026.pdf"),
        file("supplement.pdf"),
      ]);
    await expect(page.getByLabel("Type of wattlebrook-sustainability-2026.pdf")).toHaveValue(
      "sustainability",
    );
    await expect(page.getByLabel("Type of Annual_Report_2026.pdf")).toHaveValue("annual");
    const other = page.getByLabel("Type of supplement.pdf");
    await expect(other).toHaveValue("other");
    await other.selectOption("annual");
    await expect(other).toHaveValue("annual");
    await expect(page.getByRole("button", { name: "Upload 3 files" })).toBeEnabled();
  });

  test("blocks unsupported and too-large files; the rest still upload", async ({ page }) => {
    const sent: string[] = [];
    await openPack(page, { ...EMPTY, limits: { max_upload_mb: 1, max_documents: 20 } }, {
      "POST /packs/pack-1/documents": (_u, route: Route) => {
        sent.push(route.request().postData() ?? "");
        return { status: 201, json: doc() };
      },
    });
    await page
      .getByLabel("Choose files")
      .setInputFiles([
        file("notes.docx"),
        file("readme.txt"),
        file("huge.pdf", Buffer.alloc(1024 * 1024 + 1)),
        file("annual-report.pdf"),
      ]);
    const items = page.getByTestId("upload-item");
    await expect(items.filter({ hasText: "notes.docx" })).toContainText("Unsupported format");
    await expect(items.filter({ hasText: "readme.txt" })).toContainText("Unsupported format");
    await expect(items.filter({ hasText: "huge.pdf" })).toContainText("File too large (max 1 MB)");
    await page.getByRole("button", { name: "Upload 1 file" }).click();
    await expect(items.filter({ hasText: "annual-report.pdf" })).toHaveCount(0);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain('filename="annual-report.pdf"');
    await expect(items.filter({ hasText: "notes.docx" })).toBeVisible();
  });

  test("refuses files over the 20-file limit", async ({ page }) => {
    const docs = Array.from({ length: 19 }, (_, i) => doc({ id: `d${i}`, filename: `f${i}.pdf` }));
    await openPack(page, { documents: docs });
    await page.getByLabel("Choose files").setInputFiles([file("a.pdf"), file("b.pdf")]);
    const items = page.getByTestId("upload-item");
    await expect(items.filter({ hasText: "b.pdf" })).toContainText("A pack can hold up to 20 files");
    await expect(page.getByRole("button", { name: "Upload 1 file" })).toBeVisible();
  });

  test("a duplicate is skipped with a notice", async ({ page }) => {
    await openPack(page, EMPTY, {
      "POST /packs/pack-1/documents": () => ({
        status: 409,
        json: { error: "duplicate_document", message: "Document already exists in pack" },
      }),
    });
    await page.getByLabel("Choose files").setInputFiles([file("annual-report.pdf")]);
    await page.getByRole("button", { name: "Upload 1 file" }).click();
    await expect(page.getByText("Skipped 1 duplicate file")).toBeVisible();
    await expect(page.getByTestId("upload-item")).toHaveCount(0);
  });

  test("files the server cannot parse are marked with the reason", async ({ page }) => {
    const codes: Record<string, string> = {
      "scanned.pdf": "no_text_layer",
      "locked.pdf": "encrypted",
      "broken.pdf": "invalid_pdf",
    };
    await openPack(page, EMPTY, {
      "POST /packs/pack-1/documents": (_u, route: Route) => {
        const body = route.request().postData() ?? "";
        const name = Object.keys(codes).find((n) => body.includes(`filename="${n}"`)) ?? "";
        return { status: 422, json: { error: codes[name], message: "PDF could not be parsed" } };
      },
    });
    await page
      .getByLabel("Choose files")
      .setInputFiles(Object.keys(codes).map((name) => file(name)));
    await page.getByRole("button", { name: "Upload 3 files" }).click();
    const items = page.getByTestId("upload-item");
    await expect(items.filter({ hasText: "scanned.pdf" })).toContainText(
      "No text layer (scanned PDFs aren't supported)",
    );
    await expect(items.filter({ hasText: "locked.pdf" })).toContainText("Password protected");
    await expect(items.filter({ hasText: "broken.pdf" })).toContainText("Couldn't read this file");
    await expect(items.getByRole("button", { name: "Retry" })).toHaveCount(0);
  });

  test("a network failure offers Retry on that file only", async ({ page }) => {
    let attempts = 0;
    let uploaded = false;
    await mockApi(page, {
      "GET /packs/pack-1": () => ({
        json: packDetail(uploaded ? { status: "uploaded", documents: [doc({ filename: "b.pdf", index_status: "pending" })] } : EMPTY),
      }),
      "POST /packs/pack-1/documents": (_u, route: Route) => {
        const body = route.request().postData() ?? "";
        if (body.includes('filename="a.pdf"') && attempts++ === 0) return { abort: true };
        uploaded = true;
        return { status: 201, json: doc() };
      },
    });
    await page.goto("/packs/pack-1");
    await page.getByLabel("Choose files").setInputFiles([file("a.pdf"), file("b.pdf")]);
    await page.getByRole("button", { name: "Upload 2 files" }).click();
    const failed = page.getByTestId("upload-item").filter({ hasText: "a.pdf" });
    await expect(failed).toContainText("Upload failed");
    await expect(page.getByTestId("upload-item")).toHaveCount(1);
    await expect(page.getByTestId("document-row")).toContainText("b.pdf");
    await failed.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByTestId("upload-item")).toHaveCount(0);
  });

  test("an annual report's sustainability page range is sent as a part", async ({ page }) => {
    let body = "";
    await openPack(page, EMPTY, {
      "POST /packs/pack-1/documents": (_u, route: Route) => {
        body = route.request().postData() ?? "";
        return { status: 201, json: doc() };
      },
    });
    await page.getByLabel("Choose files").setInputFiles([file("annual-report.pdf")]);
    const first = page.getByLabel("First sustainability page of annual-report.pdf");
    const last = page.getByLabel("Last sustainability page of annual-report.pdf");
    const upload = page.getByRole("button", { name: "Upload 1 file" });
    await first.fill("31");
    await last.fill("18");
    await expect(page.getByText("The first page must not be after the last page.")).toBeVisible();
    await expect(upload).toBeDisabled();
    await first.fill("18");
    await last.fill("31");
    await upload.click();
    await expect(page.getByTestId("upload-item")).toHaveCount(0);
    expect(body).toContain('name="kind"\r\n\r\nannual');
    expect(body).toContain('"kind":"sustainability","first_pdf_page":18,"last_pdf_page":31');
  });
});

test.describe("Parse (PRD 6.3, 9.1 #3)", () => {
  test("Parse disables itself, polls and ends Parsed with unit counts", async ({ page }) => {
    let phase: "pending" | "indexing" | "done" = "pending";
    let polls = 0;
    await mockApi(page, {
      "GET /packs/pack-1": () => {
        if (phase === "indexing" && ++polls >= 2) phase = "done";
        const status = { pending: "uploaded", indexing: "parsing", done: "parsed" }[phase];
        return { json: packDetail({ status, documents: [doc({ index_status: phase })] }) };
      },
      "POST /packs/pack-1/parse": () => {
        phase = "indexing";
        return { status: 202, json: { status: "indexing" } };
      },
    });
    await page.goto("/packs/pack-1");
    const row = page.getByTestId("document-row");
    await expect(row).toContainText("Not parsed");
    await expect(page.getByTestId("pack-status")).toHaveText("Uploaded");
    await page.getByRole("button", { name: "Parse" }).click();
    await expect(page.getByRole("button", { name: "Parsing…" })).toBeDisabled();
    await expect(page.getByTestId("pack-status")).toHaveText("Parsing");
    await expect(page.getByTestId("pack-status")).toHaveText("Parsed", { timeout: 10_000 });
    await expect(row).toContainText("Parsed");
    await expect(row).toContainText("412");
    await expect(page.getByRole("button", { name: "Parse" })).toBeDisabled();
  });

  test("a failed file makes the pack 'Parsed with issues'", async ({ page }) => {
    await openPack(page, {
      status: "parsed_with_issues",
      documents: [doc(), doc({ id: "d2", filename: "supplement.pdf", kind: "other", parts: [], index_status: "failed" })],
    });
    await expect(page.getByTestId("pack-status")).toHaveText("Parsed with issues");
    await expect(page.getByTestId("document-row").filter({ hasText: "supplement.pdf" })).toContainText(
      "Parse failed",
    );
    await expect(page.getByText("1 file couldn't be parsed. Parse again to retry.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Parse" })).toBeEnabled();
  });

  test("warns when the pack has no sustainability report", async ({ page }) => {
    const warning = "No sustainability report in this pack. Most checks will find no evidence.";
    await openPack(page, { documents: [doc({ parts: [] })] });
    await expect(page.getByText(warning)).toBeVisible();
  });

  test("no warning when an annual report has a sustainability part", async ({ page }) => {
    await openPack(page, {});
    await expect(page.getByTestId("document-row")).toContainText("18–31");
    await expect(page.getByText(/No sustainability report in this pack/)).toHaveCount(0);
  });
});

test.describe("Documents (PRD 6.3)", () => {
  test("delete asks for confirmation and removes the row", async ({ page }) => {
    let deleted = false;
    await mockApi(page, {
      "GET /packs/pack-1": () => ({ json: packDetail(deleted ? EMPTY : {}) }),
      "DELETE /packs/pack-1/documents/doc-1": () => {
        deleted = true;
        return { json: { status: "deleted" } };
      },
    });
    await page.goto("/packs/pack-1");
    await page.getByRole("button", { name: "Delete wattlebrook-annual-report-2026.pdf" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("Delete this file? Past reports keep their evidence text.");
    await dialog.getByRole("button", { name: "Delete" }).click();
    await expect(page.getByText("No files yet.")).toBeVisible();
  });

  test("delete is disabled while a check is running", async ({ page }) => {
    await openPack(page, { active_run_id: "run-1" });
    const button = page.getByRole("button", { name: "Delete wattlebrook-annual-report-2026.pdf" });
    await expect(button).toBeDisabled();
    await expect(button).toHaveAttribute("title", "A check is running on this pack.");
  });

  test("editing the page range sends PUT and shows server errors", async ({ page }) => {
    const sent: unknown[] = [];
    let reject = true;
    await openPack(page, {}, {
      "PUT /packs/pack-1/documents/doc-1/parts": (_u, route: Route) => {
        sent.push(route.request().postDataJSON());
        return reject
          ? { status: 422, json: { error: "invalid_parts", message: "Invalid document parts" } }
          : { json: doc() };
      },
    });
    await page.getByRole("button", { name: "Edit pages" }).click();
    const last = page.getByLabel("Last sustainability page of wattlebrook-annual-report-2026.pdf");
    await last.fill("40");
    await expect(page.getByText("This file has 39 pages.")).toBeVisible();
    await last.fill("35");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Page range is outside this PDF or overlaps another part.")).toBeVisible();
    await expect(last).toHaveValue("35");
    reject = false;
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByRole("button", { name: "Edit pages" })).toBeVisible();
    expect(sent[1]).toEqual([
      { kind: "sustainability", first_pdf_page: 18, last_pdf_page: 35, label: "Sustainability report" },
    ]);
  });

  test("alex sees files and status but no upload, parse, edit or delete", async ({ page, context }) => {
    await context.addCookies([{ name: "cda_user", value: "alex", url: "http://127.0.0.1:3100" }]);
    await openPack(page, { status: "uploaded", documents: [doc({ index_status: "pending" })] });
    await expect(page.getByTestId("document-row")).toContainText("Not parsed");
    await expect(page.getByTestId("pack-status")).toHaveText("Uploaded");
    for (const name of ["Parse", "Choose files", "Edit pages", "Delete wattlebrook-annual-report-2026.pdf"]) {
      await expect(page.getByRole("button", { name })).toHaveCount(0);
    }
    await expect(page.getByLabel("Choose files")).toHaveCount(0);
  });
});
