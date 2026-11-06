import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'l5-security.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 45_000,
  use: { baseURL: 'http://127.0.0.1:5187', headless: true, browserName: 'chromium' },
  webServer: {
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 5187 --strictPort',
    url: 'http://127.0.0.1:5187',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
