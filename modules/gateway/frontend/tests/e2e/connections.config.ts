import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'connections.spec.ts',
  grep: /atlassian_/,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  use: { browserName: 'chromium', headless: true },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5173 --strictPort',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
