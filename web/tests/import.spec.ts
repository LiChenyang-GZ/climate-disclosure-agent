import { expect, test, type Page, type Route } from "@playwright/test";

import { doc, mockApi, packDetail, type Handler } from "./mock-api";

// OneDrive / SharePoint import (X2, D84). Graph is behind the API, so only /api/* is mocked.
const CONNECTED = { configured: true, connected: true, account: "sarah.chen@wattlebrook.example" };
const DISCONNECTED = { configured: true, connected: false, account: null };
const CODE = {
  user_code: "FAKE7Q2K",
  verification_uri: "https://microsoft.com/devicelogin",
  expires_in: 900,
  interval: 1,
};
const DRIVE = "b!sustainability-documents";
const SITE = "wattlebrook.sharepoint.com,1111,2222";
const EMPTY = { sites: [], drives: [], folders: [], files: [] };

function file(item_id: string, name: string) {
  return { drive_id: DRIVE, item_id, name, path: `/Drafts/${name}`, size: 2048, last_modified: null };
}

function imported(overrides: Record<string, unknown> = {}) {
  return doc({
    id: "doc-sp",
    filename: "climate-report-2026.pdf",
    kind: "sustainability",
    parts: [],
    source: {
      provider: "microsoft",
      drive_id: DRIVE,
      item_id: "01CLIMATEREPORTPDF",
      path: "/Shared Documents/Drafts/climate-report-2026.pdf",
      etag: '"{1},1"',
      web_url: null,
    },
    ...overrides,
  });
}

async function openPack(page: Page, handlers: Record<string, Handler>, detail: Record<string, unknown> = {}) {
  const calls = await mockApi(page, {
    "GET /packs/pack-1": () => ({ json: packDetail(detail) }),
    ...handlers,
  });
  await page.goto("/packs/pack-1");
  await expect(page.getByRole("heading", { name: "Wattlebrook Energy" })).toBeVisible();
  return calls;
}

function polls(...states: Record<string, unknown>[]): Handler {
  let i = 0;
  return () => ({ json: states[Math.min(i++, states.length - 1)] });
}

test.describe("Connect Microsoft", () => {
  test("shows the code and link, polls until connected", async ({ page }) => {
    const calls = await openPack(page, {
      "GET /integrations/microsoft": () => ({ json: DISCONNECTED }),
      "POST /integrations/microsoft/device-code": () => ({ json: CODE }),
      "POST /integrations/microsoft/poll": polls(
        { state: "pending" },
        { state: "connected", account: "sarah.chen@wattlebrook.example" },
      ),
    });
    await page.getByRole("button", { name: "Connect Microsoft" }).click();
    await expect(page.getByTestId("device-code")).toHaveText("FAKE7Q2K");
    const link = page.getByRole("link", { name: "https://microsoft.com/devicelogin" });
    await expect(link).toHaveAttribute("href", "https://microsoft.com/devicelogin");
    await expect(page.getByText("Connected as sarah.chen@wattlebrook.example")).toBeVisible();
    await expect(page.getByTestId("device-code")).toHaveCount(0);
    expect(calls.filter((c) => c === "POST /integrations/microsoft/poll")).toHaveLength(2);
    await expect(page.getByRole("button", { name: "Import from SharePoint" })).toBeEnabled();
  });

  test("cancelled sign-in", async ({ page }) => {
    await openPack(page, {
      "GET /integrations/microsoft": () => ({ json: DISCONNECTED }),
      "POST /integrations/microsoft/device-code": () => ({ json: CODE }),
      "POST /integrations/microsoft/poll": polls({ state: "cancelled" }),
    });
    await page.getByRole("button", { name: "Connect Microsoft" }).click();
    await expect(page.getByText("Microsoft connection was cancelled.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect Microsoft" })).toBeVisible();
  });

  test("expired code, then Reconnect starts again", async ({ page }) => {
    let starts = 0;
    await openPack(page, {
      "GET /integrations/microsoft": () => ({ json: DISCONNECTED }),
      "POST /integrations/microsoft/device-code": () => {
        starts += 1;
        return { json: CODE };
      },
      "POST /integrations/microsoft/poll": () =>
        starts === 1 ? { json: { state: "expired" } } : { json: { state: "pending" } },
    });
    await page.getByRole("button", { name: "Connect Microsoft" }).click();
    await expect(page.getByText("Your Microsoft connection expired. Reconnect.")).toBeVisible();
    await page.getByRole("button", { name: "Reconnect" }).click();
    await expect(page.getByTestId("device-code")).toHaveText("FAKE7Q2K");
    expect(starts).toBe(2);
  });

  test("an expired connection found while browsing asks to reconnect", async ({ page }) => {
    await openPack(page, {
      "GET /integrations/microsoft": () => ({ json: CONNECTED }),
      "GET /integrations/microsoft/browse": () => ({
        status: 409,
        json: { error: "microsoft_expired", message: "Your Microsoft connection expired. Reconnect." },
      }),
    });
    await page.getByRole("button", { name: "Import from SharePoint" }).click();
    await expect(page.getByText("Your Microsoft connection expired. Reconnect.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Reconnect" })).toBeVisible();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  });

  test("not set up: no connect button", async ({ page }) => {
    await openPack(page, {});
    await expect(page.getByText(/Importing from OneDrive or SharePoint isn't set up/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Connect Microsoft" })).toHaveCount(0);
  });
});

test("import dialog: browse, empty folder, per-file results", async ({ page }) => {
  const sent: unknown[] = [];
  const browse: Handler = (_user, route: Route) => {
    const url = new URL(route.request().url());
    const site = url.searchParams.get("site_id");
    const item = url.searchParams.get("item_id");
    if (site === SITE) {
      return { json: { ...EMPTY, drives: [{ id: DRIVE, name: "Documents" }] } };
    }
    if (url.searchParams.get("drive_id") === DRIVE && item === null) {
      return {
        json: {
          ...EMPTY,
          folders: [{ drive_id: DRIVE, item_id: "01OLD", name: "Old versions" }],
          files: [
            file("01PDF", "climate-report-2026.pdf"),
            file("01SECRET", "board-only.pdf"),
            file("01COPY", "copy-of-annual.pdf"),
            file("01XLSX", "emissions.xlsx"),
          ],
        },
      };
    }
    if (item === "01OLD") return { json: EMPTY };
    return {
      json: {
        ...EMPTY,
        sites: [
          { id: "me", name: "My files", kind: "onedrive" },
          { id: SITE, name: "Sustainability Reporting", kind: "sharepoint" },
        ],
      },
    };
  };
  await openPack(page, {
    "GET /integrations/microsoft": () => ({ json: CONNECTED }),
    "GET /integrations/microsoft/browse": browse,
    "POST /packs/pack-1/import": (_user, route: Route) => {
      sent.push(route.request().postDataJSON());
      return {
        json: [
          { item_id: "01PDF", status: "imported", document: imported() },
          { item_id: "01SECRET", status: "denied", error: "access_denied", message: "Access denied in SharePoint" },
          { item_id: "01COPY", status: "duplicate", error: "duplicate_document", message: "Document already exists in pack" },
          { item_id: "01XLSX", status: "rejected", error: "invalid_file", message: "File could not be parsed" },
        ],
      };
    },
  });
  await page.getByRole("button", { name: "Import from SharePoint" }).click();
  const dialog = page.getByRole("dialog", { name: "Import from SharePoint" });
  await dialog.getByRole("button", { name: "Sustainability Reporting" }).click();
  await dialog.getByRole("button", { name: "Documents" }).click();
  await dialog.getByRole("button", { name: "Old versions" }).click();
  await expect(dialog.getByText("No supported files in this folder.")).toBeVisible();
  await dialog.getByRole("navigation", { name: "Location" }).getByRole("button", { name: "Documents" }).click();
  await expect(dialog.getByTestId("import-file")).toHaveCount(4);
  await expect(dialog.getByText("No supported files in this folder.")).toHaveCount(0);

  for (const name of ["climate-report-2026.pdf", "board-only.pdf", "copy-of-annual.pdf", "emissions.xlsx"]) {
    await dialog.getByLabel(name).check();
  }
  await expect(dialog.getByLabel("Type of climate-report-2026.pdf")).toHaveValue("sustainability");
  await dialog.getByLabel("Type of copy-of-annual.pdf").selectOption("annual");
  await dialog.getByRole("button", { name: "Import 4 files" }).click();

  await expect(dialog.getByText("Imported 1 file.")).toBeVisible();
  await expect(dialog.getByText("Skipped 1 duplicate file")).toBeVisible();
  const problems = dialog.getByTestId("import-problems");
  await expect(problems).toContainText("board-only.pdf: Access denied in SharePoint");
  await expect(problems).toContainText("emissions.xlsx: Couldn't read this file");
  expect(sent).toEqual([
    {
      items: [
        { drive_id: DRIVE, item_id: "01PDF", kind: "sustainability" },
        { drive_id: DRIVE, item_id: "01SECRET", kind: "other" },
        { drive_id: DRIVE, item_id: "01COPY", kind: "annual" },
        { drive_id: DRIVE, item_id: "01XLSX", kind: "other" },
      ],
    },
  ]);
  await expect(dialog.getByRole("button", { name: "Import 0 files" })).toBeDisabled();
});

test("Source column for imported and uploaded files, also for alex", async ({ page, context }) => {
  const detail = { documents: [doc(), imported()] };
  await openPack(page, {}, detail);
  const rows = page.getByTestId("document-row");
  await expect(page.getByRole("columnheader", { name: "Source" })).toBeVisible();
  await expect(rows.filter({ hasText: "climate-report-2026.pdf" })).toContainText(
    "SharePoint · /Shared Documents/Drafts/climate-report-2026.pdf",
  );
  await expect(rows.filter({ hasText: "wattlebrook-annual-report-2026.pdf" })).toContainText("Upload");

  await context.addCookies([
    { name: "cda_user", value: "alex", url: `http://127.0.0.1:${process.env.PW_PORT ?? 3100}` },
  ]);
  await page.reload();
  await expect(rows.filter({ hasText: "climate-report-2026.pdf" })).toContainText(
    "SharePoint · /Shared Documents/Drafts/climate-report-2026.pdf",
  );
  await expect(page.getByRole("region", { name: "OneDrive and SharePoint" })).toHaveCount(0);
});

test.describe("Re-sync", () => {
  test("only shown when the pack has imported files", async ({ page }) => {
    await openPack(page, { "GET /integrations/microsoft": () => ({ json: CONNECTED }) });
    await expect(page.getByRole("button", { name: "Import from SharePoint" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Re-sync" })).toHaveCount(0);
  });

  test("everything up to date", async ({ page }) => {
    await openPack(
      page,
      {
        "GET /integrations/microsoft": () => ({ json: CONNECTED }),
        "GET /packs/pack-1/sync": () => ({ json: { changed: [], removed: [], failed: [], unchanged: 1 } }),
      },
      { documents: [imported()] },
    );
    await page.getByRole("button", { name: "Re-sync" }).click();
    await expect(page.getByText("Everything is up to date.")).toBeVisible();
  });

  test("changed files are updated; removed files are only marked", async ({ page }) => {
    const sent: unknown[] = [];
    let synced = false;
    const removed = imported({ id: "doc-gone", filename: "old-targets.pdf" });
    const calls = await openPack(
      page,
      {
        "GET /integrations/microsoft": () => ({ json: CONNECTED }),
        "GET /packs/pack-1": () => ({
          json: packDetail({
            status: synced ? "uploaded" : "parsed",
            documents: synced
              ? [imported({ id: "doc-new", index_status: "pending" }), removed]
              : [imported(), removed],
          }),
        }),
        "GET /packs/pack-1/sync": () => ({
          json: {
            changed: [
              {
                document_id: "doc-sp",
                filename: "climate-report-2026.pdf",
                path: "/Shared Documents/Drafts/climate-report-2026.pdf",
              },
            ],
            removed: [{ document_id: "doc-gone", filename: "old-targets.pdf", path: "/Drafts/old-targets.pdf" }],
            failed: [],
            unchanged: 0,
          },
        }),
        "POST /packs/pack-1/sync": (_user, route: Route) => {
          sent.push(route.request().postDataJSON());
          synced = true;
          return {
            json: [{ document_id: "doc-sp", status: "updated", replaced: "doc-sp", document: imported({ id: "doc-new" }) }],
          };
        },
      },
    );
    await page.getByRole("button", { name: "Re-sync" }).click();
    const panel = page.getByTestId("resync");
    await expect(panel).toContainText("1 file changed in SharePoint");
    await expect(panel.getByTestId("removed")).toContainText("old-targets.pdf: Removed in SharePoint");
    await panel.getByRole("button", { name: "Update" }).click();
    await expect(panel).toContainText("Updated 1 file.");
    expect(sent).toEqual([{ document_ids: ["doc-sp"] }]);
    await expect(page.getByTestId("pack-status")).toHaveText("Uploaded");
    // Removed in SharePoint does not delete anything in the pack.
    await expect(page.getByTestId("document-row").filter({ hasText: "old-targets.pdf" })).toBeVisible();
    expect(calls.some((c) => c.startsWith("DELETE"))).toBe(false);
  });
});
