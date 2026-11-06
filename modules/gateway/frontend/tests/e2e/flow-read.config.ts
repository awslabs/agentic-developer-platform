import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: ['flow-read.spec.ts', 'flow-read-performance.spec.ts', 'flow-read-observer.spec.ts'],
  workers: 1,
  retries: 0,
  timeout: 30_000,
  use: { baseURL: 'http://127.0.0.1:5186', headless: true, browserName: 'chromium' },
  webServer: {
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 5186 --strictPort',
    url: 'http://127.0.0.1:5186',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
