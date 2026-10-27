import { expect, test, type Page, type Route } from "@playwright/test";

import { TEAMS, mockApi, pack, packDetail, type Handler } from "./mock-api";

test.describe("Packs page (PRD 6.2, 9.1 #1)", () => {
  test("empty state for sarah offers New pack", async ({ page }) => {
    await mockApi(page, { "GET /packs": () => ({ json: [] }) });
    await page.goto("/");
    await expect(page.getByText("No packs yet.")).toBeVisible();
    await expect(page.getByRole("button", { name: "New pack" })).toBeVisible();
  });

  test("alex without packs sees 'not assigned', never 'no packs', and no New pack", async ({
    page,
    context,
  }) => {
    await context.addCookies([{ name: "cda_user", value: "alex", url: "http://127.0.0.1:3100" }]);
    await mockApi(page, { "GET /packs": () => ({ json: [] }) });
    await page.goto("/");
    await expect(page.getByText("No packs assigned to you yet.")).toBeVisible();
    await expect(page.getByText("No packs yet.")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "New pack" })).toHaveCount(0);
  });

  test("shows skeleton rows while loading", async ({ page }) => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    await mockApi(page, { "GET /packs": () => ({ json: [pack()], delay: gate }) });
    await page.goto("/");
    await expect(page.getByTestId("pack-skeleton").first()).toBeVisible();
    await expect(page.getByTestId("pack-row")).toHaveCount(0);
    release();
    await expect(page.getByTestId("pack-row")).toHaveCount(1);
    await expect(page.getByTestId("pack-skeleton")).toHaveCount(0);
  });

  test("load failure shows Retry and no empty state; Retry recovers", async ({ page }) => {
    let fail = true;
    await mockApi(page, {
      "GET /packs": () =>
        fail ? { status: 500, json: { error: "internal_error" } } : { json: [pack()] },
    });
    await page.goto("/");
    await expect(page.getByText("Couldn't load packs.")).toBeVisible();
    await expect(page.getByText("No packs yet.")).toHaveCount(0);
    fail = false;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByTestId("pack-row")).toHaveCount(1);
    await expect(page.getByText("Couldn't load packs.")).toHaveCount(0);
  });

  test("rows show company, year, files, team and latest-run summary", async ({ page }) => {
    await mockApi(page, {
      "GET /packs": () => ({
        json: [
          pack({
            latest_run: {
              id: "run-1",
              status: "waiting_review",
              created_at: "2026-10-27T01:00:00Z",
              counts: { evidence_found: 11, inconsistent: 1, needs_review: 1 },
            },
          }),
          pack({
            id: "pack-2",
            company: "Harbour Bank",
            team_name: "Financial Services",
            document_count: 0,
          }),
        ],
      }),
    });
    await page.goto("/");
    const rows = page.getByTestId("pack-row");
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText("Wattlebrook Energy");
    await expect(rows.nth(0)).toContainText("FY2026");
    await expect(rows.nth(0)).toContainText("Energy & Resources");
    await expect(rows.nth(0)).toContainText("11 found · 1 inconsistent · 1 needs review");
    await expect(rows.nth(1)).toContainText("Financial Services");
    await expect(rows.nth(1)).toContainText("No checks yet");
  });

  test("switching to alex sets the cookie and reloads data", async ({ page, context }) => {
    const sarahPacks = [
      pack(),
      pack({ id: "p2", company: "Harbour Bank", team_name: "Financial Services" }),
      pack({ id: "p3", company: "Coastal Insurance" }),
    ];
    await mockApi(page, {
      "GET /packs": (user) => ({
        json: user === "alex" ? [sarahPacks[0], sarahPacks[2]] : sarahPacks,
      }),
    });
    await page.goto("/");
    await expect(page.getByTestId("pack-row")).toHaveCount(3);
    await page.getByLabel("Demo user").selectOption("alex");
    await expect(page.getByTestId("pack-row")).toHaveCount(2);
    await expect(page.getByText("Harbour Bank")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "New pack" })).toHaveCount(0);
    const cookie = (await context.cookies()).find((c) => c.name === "cda_user");
    expect(cookie?.value).toBe("alex");
  });

  test("switching user on a pack the new user cannot see returns to Packs", async ({ page }) => {
    await mockApi(page, {
      "GET /packs": () => ({ json: [] }),
      "GET /packs/p2": (user) =>
        user === "alex"
          ? { status: 404, json: { error: "not_found", message: "Resource not found" } }
          : { json: packDetail({ id: "p2", company: "Harbour Bank" }) },
    });
    await page.goto("/packs/p2");
    await expect(page.getByRole("heading", { name: "Harbour Bank" })).toBeVisible();
    await page.getByLabel("Demo user").selectOption("alex");
    await expect(page).toHaveURL(/127\.0\.0\.1:3100\/$/);
    await expect(page.getByText("You don't have access to that page as alex.")).toBeVisible();
  });
});

test.describe("New pack dialog (PRD 6.2)", () => {
  async function open(page: Page, extra: Record<string, Handler> = {}) {
    await mockApi(page, {
      "GET /packs": () => ({ json: [] }),
      "GET /teams": () => ({ json: TEAMS }),
      ...extra,
    });
    await page.goto("/");
    await page.getByRole("button", { name: "New pack" }).click();
    return page.getByRole("dialog", { name: "New pack" });
  }

  test("validates company and year; Create stays disabled", async ({ page }) => {
    const dialog = await open(page);
    const create = dialog.getByRole("button", { name: "Create" });
    await expect(create).toBeDisabled();
    const company = dialog.getByLabel("Company");
    await company.fill("x");
    await company.fill("");
    await expect(dialog.getByText("Company name is required.")).toBeVisible();
    await company.fill("A".repeat(81));
    await expect(dialog.getByText("Use at most 80 characters.")).toBeVisible();
    await company.fill("Wattlebrook Energy");
    const year = dialog.getByLabel("Reporting year (FY)");
    await year.fill("2026");
    await year.fill("");
    await expect(dialog.getByText("Reporting year is required.")).toBeVisible();
    await expect(create).toBeDisabled();
    await year.fill("1999");
    await expect(dialog.getByText("Enter a year between 2000 and 2100.")).toBeVisible();
    await year.fill("2026");
    await expect(create).toBeEnabled();
    await expect(dialog.getByTestId("team-members")).toHaveText(
      "Members: Sarah Chen, Alex Nguyen",
    );
    await dialog.getByLabel("Team").selectOption({ label: "Financial Services" });
    await expect(dialog.getByTestId("team-members")).toHaveText("Members: Sarah Chen");
  });

  test("success opens the new pack", async ({ page }) => {
    let body: unknown;
    const dialog = await open(page, {
      "POST /packs": (_user: string, route: Route) => {
        body = route.request().postDataJSON();
        return { status: 201, json: { id: "new-pack" } };
      },
      "GET /packs/new-pack": () => ({
        json: packDetail({ id: "new-pack", status: "empty", documents: [] }),
      }),
    });
    await dialog.getByLabel("Company").fill("  Wattlebrook Energy ");
    await dialog.getByLabel("Reporting year (FY)").fill("2026");
    await dialog.getByRole("button", { name: "Create" }).click();
    await expect(page).toHaveURL(/\/packs\/new-pack$/);
    await expect(page.getByText("No files yet.")).toBeVisible();
    expect(body).toEqual({
      company: "Wattlebrook Energy",
      reporting_year: 2026,
      team_id: "team-er",
    });
  });

  test("duplicate company and year links to the existing pack", async ({ page }) => {
    const dialog = await open(page, {
      "POST /packs": () => ({
        status: 409,
        json: { error: "duplicate_pack", message: "exists", pack_id: "old-pack" },
      }),
    });
    await dialog.getByLabel("Company").fill("Wattlebrook Energy");
    await dialog.getByLabel("Reporting year (FY)").fill("2026");
    await dialog.getByRole("button", { name: "Create" }).click();
    await expect(
      dialog.getByText("A pack for this company and year already exists."),
    ).toBeVisible();
    await expect(dialog.getByRole("link", { name: "Open it" })).toHaveAttribute(
      "href",
      "/packs/old-pack",
    );
    await expect(dialog).toBeVisible();
  });

  test("server error keeps the dialog and the inputs", async ({ page }) => {
    const dialog = await open(page, {
      "POST /packs": () => ({ status: 500, json: { error: "internal_error", message: "x" } }),
    });
    await dialog.getByLabel("Company").fill("Wattlebrook Energy");
    await dialog.getByLabel("Reporting year (FY)").fill("2026");
    await dialog.getByRole("button", { name: "Create" }).click();
    await expect(dialog.getByText("Couldn't create the pack. Try again.")).toBeVisible();
    await expect(dialog.getByLabel("Company")).toHaveValue("Wattlebrook Energy");
    await expect(dialog.getByLabel("Reporting year (FY)")).toHaveValue("2026");
    await expect(dialog.getByRole("button", { name: "Create" })).toBeEnabled();
  });
});
