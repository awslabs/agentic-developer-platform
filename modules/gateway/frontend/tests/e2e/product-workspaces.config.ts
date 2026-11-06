import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.', testMatch: 'product-workspaces.spec.ts', workers: 1, retries: 0, timeout: 45_000,
  use: { baseURL: 'http://127.0.0.1:5193', headless: true, browserName: 'chromium',
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } },
  webServer: { command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 5193 --strictPort',
    url: 'http://127.0.0.1:5193', reuseExistingServer: false, timeout: 120_000 },
});
