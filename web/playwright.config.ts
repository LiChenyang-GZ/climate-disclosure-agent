import { defineConfig, devices } from "@playwright/test";

// The UI tests mock /api/* in the browser (D81). The production server still
// needs an upstream for the proxy test, which starts its own stub on STUB_PORT.
const PORT = 3100;
const STUB_PORT = 3199;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npm run build && npx next start -p ${PORT} -H 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
    env: { CDA_API_URL: `http://127.0.0.1:${STUB_PORT}` },
  },
});
