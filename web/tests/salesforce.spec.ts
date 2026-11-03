import { expect, test, type BrowserContext, type Page } from "@playwright/test";

import { SF_OFF, mockApi, packDetail, type Handler } from "./mock-api";

const SF_ON = {
  configured: true,
  instance_url: "https://wattlebrook-dev-ed.develop.my.salesforce.com",
  edition: null,
};
const ACCOUNTS = [
  { id: "0015j00000WattAAAQ", name: "Wattlebrook Energy Ltd" },
  { id: "0015j00000WatsAAAQ", name: "Wattlebrook Holdings" },
];

async function asAlex(context: BrowserContext) {
  await context.addCookies([
    { name: "cda_user", value: "alex", url: `http://127.0.0.1:${process.env.PW_PORT ?? 3100}` },
  ]);
}

async function openSettings(page: Page, extra: Record<string, Handler> = {}) {
  const calls = await mockApi(page, extra);
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Connect Salesforce" })).toBeVisible();
  return calls;
}

async function fill(page: Page) {
  await page.getByLabel("Instance URL").fill(SF_ON.instance_url);
  await page.getByLabel("Client ID").fill("3MVG9-consumer-key");
  await page.getByLabel("Client secret").fill("not-a-real-secret");
}

test.describe("Settings: Connect Salesforce (D85)", () => {
  test("success shows the edition", async ({ page }) => {
    let body: unknown = null;
    await openSettings(page, {
      "POST /integrations/salesforce/test": (_user, route) => {
        body = route.request().postDataJSON();
        return { json: { connected: true, edition: "Developer Edition" } };
      },
    });
    await page.getByRole("link", { name: "Settings" }).waitFor();
    await expect(page.getByTestId("salesforce-state")).toHaveText("Salesforce: not connected.");
    const button = page.getByRole("button", { name: "Test connection" });
    await expect(button).toBeDisabled();
    await fill(page);
    await button.click();
    await expect(
      page.getByRole("form", { name: "Connect Salesforce" }).getByRole("status"),
    ).toHaveText("Connected to Salesforce (Developer Edition).");
    expect(body).toEqual({
      instance_url: SF_ON.instance_url,
      client_id: "3MVG9-consumer-key",
      client_secret: "not-a-real-secret",
    });
    // The secret is not kept in the form after a successful test.
    await expect(page.getByLabel("Client secret")).toHaveValue("");
  });

  test("failure keeps the form", async ({ page }) => {
    await openSettings(page, {
      "POST /integrations/salesforce/test": () => ({
        status: 422,
        json: { error: "salesforce_rejected", message: "Salesforce rejected the credentials." },
      }),
    });
    await fill(page);
    await page.getByRole("button", { name: "Test connection" }).click();
    const form = page.getByRole("form", { name: "Connect Salesforce" });
    await expect(form.getByRole("alert")).toHaveText("Salesforce rejected the credentials.");
    await expect(page.getByLabel("Instance URL")).toHaveValue(SF_ON.instance_url);
    await expect(page.getByLabel("Client ID")).toHaveValue("3MVG9-consumer-key");
    await expect(page.getByLabel("Client secret")).toHaveValue("not-a-real-secret");
    await expect(page.getByRole("button", { name: "Test connection" })).toBeEnabled();
  });

  test("prefills the instance URL when already configured", async ({ page }) => {
    await openSettings(page, { "GET /integrations/salesforce": () => ({ json: SF_ON }) });
    await expect(page.getByLabel("Instance URL")).toHaveValue(SF_ON.instance_url);
    await expect(page.getByTestId("salesforce-state")).toHaveText(
      `Salesforce: connected to ${SF_ON.instance_url}.`,
    );
  });
});

/** A pack whose link state lives in the mock, so PUT / DELETE show up on reload. */
async function openPack(page: Page, extra: Record<string, Handler> = {}) {
  const state: { account: (typeof ACCOUNTS)[number] | null; put: unknown } = {
    account: null,
    put: null,
  };
  const calls = await mockApi(page, {
    "GET /packs/pack-1": () => ({ json: packDetail({ salesforce_account: state.account }) }),
    "GET /integrations/salesforce": () => ({ json: SF_ON }),
    "GET /integrations/salesforce/accounts": (_user, route) => {
      const q = new URL(route.request().url()).searchParams.get("q") ?? "";
      return { json: q.includes("Nobody") ? [] : ACCOUNTS };
    },
    "PUT /packs/pack-1/salesforce-account": (_user, route) => {
      state.put = route.request().postDataJSON();
      state.account = ACCOUNTS[0];
      return { json: packDetail({ salesforce_account: state.account }) };
    },
    "DELETE /packs/pack-1/salesforce-account": () => {
      state.account = null;
      return { json: packDetail() };
    },
    ...extra,
  });
  await page.goto("/packs/pack-1");
  await expect(page.getByRole("heading", { name: "Wattlebrook Energy" })).toBeVisible();
  return { calls, state };
}

test.describe("Pack: link a Salesforce account (D85)", () => {
  test("search, pick, linked name and Unlink", async ({ page }) => {
    const { calls, state } = await openPack(page);
    await page.getByRole("button", { name: "Link Salesforce account" }).click();
    await page.getByLabel("Search Salesforce accounts").fill("Watt's 100%");
    await page.getByRole("button", { name: "Search" }).click();
    const results = page.getByRole("list", { name: "Matching accounts" });
    await expect(results.getByRole("button")).toHaveText(ACCOUNTS.map((a) => a.name));
    await results.getByRole("button", { name: "Wattlebrook Energy Ltd" }).click();
    await expect(page.getByTestId("salesforce-link")).toContainText(
      "Salesforce account: Wattlebrook Energy Ltd",
    );
    expect(state.put).toEqual(ACCOUNTS[0]);
    await page.getByRole("button", { name: "Unlink" }).click();
    await expect(page.getByRole("button", { name: "Link Salesforce account" })).toBeVisible();
    expect(calls).toContain("DELETE /packs/pack-1/salesforce-account");
  });

  test("no match says to create the account in Salesforce", async ({ page }) => {
    await openPack(page);
    await page.getByRole("button", { name: "Link Salesforce account" }).click();
    await page.getByLabel("Search Salesforce accounts").fill("Nobody");
    await page.getByRole("button", { name: "Search" }).click();
    await expect(page.getByText("No matching account. Create it in Salesforce first.")).toBeVisible();
  });

  test("search errors are shown", async ({ page }) => {
    await openPack(page, {
      "GET /integrations/salesforce/accounts": () => ({
        status: 503,
        json: { error: "salesforce_limit", message: "Salesforce API limit reached. Try again later." },
      }),
    });
    await page.getByRole("button", { name: "Link Salesforce account" }).click();
    await page.getByLabel("Search Salesforce accounts").fill("Watt");
    await page.getByRole("button", { name: "Search" }).click();
    await expect(page.getByTestId("salesforce-link").getByRole("alert")).toHaveText(
      "Salesforce API limit reached. Try again later.",
    );
  });

  test("not configured: no link, points to Settings", async ({ page }) => {
    await openPack(page, { "GET /integrations/salesforce": () => ({ json: SF_OFF }) });
    await expect(page.getByTestId("salesforce-link")).toHaveText(
      "Connect Salesforce in Settings first",
    );
    await expect(page.getByRole("button", { name: "Link Salesforce account" })).toHaveCount(0);
    await page.getByTestId("salesforce-link").getByRole("link", { name: "Settings" }).click();
    await expect(page).toHaveURL(/\/settings$/);
  });
});

test.describe("alex (analyst)", () => {
  test("no Settings link and no link button", async ({ page, context }) => {
    await asAlex(context);
    await openPack(page);
    await expect(page.getByRole("combobox", { name: "Demo user" })).toHaveValue("alex");
    await expect(page.getByRole("link", { name: "Settings" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Link Salesforce account" })).toHaveCount(0);
    await expect(page.getByText("Connect Salesforce in")).toHaveCount(0);
  });

  test("sees a linked account without Unlink", async ({ page, context }) => {
    await asAlex(context);
    await mockApi(page, {
      "GET /packs/pack-1": () => ({ json: packDetail({ salesforce_account: ACCOUNTS[0] }) }),
    });
    await page.goto("/packs/pack-1");
    await expect(page.getByTestId("salesforce-link")).toHaveText(
      "Salesforce account: Wattlebrook Energy Ltd",
    );
    await expect(page.getByRole("button", { name: "Unlink" })).toHaveCount(0);
  });

  test("the settings page says only a lead can change settings", async ({ page, context }) => {
    await asAlex(context);
    await mockApi(page, {});
    await page.goto("/settings");
    await expect(page.getByText("Only a team lead can change settings.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Test connection" })).toHaveCount(0);
  });
});
