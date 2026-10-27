import { defineConfig, devices } from "@playwright/test";

// Smoke tests against a real API and database (D81). Local only, not in CI:
// start the API and `next start`, then
//   SMOKE_URL=http://127.0.0.1:3101 npx playwright test -c playwright.smoke.config.ts
export default defineConfig({
  testDir: "./smoke",
  timeout: 300_000,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.SMOKE_URL ?? "http://127.0.0.1:3000",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
