import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: '.', testMatch: 'explanations.spec.ts', workers: 1, retries: 0,
  use: { baseURL: 'http://127.0.0.1:5178', headless: true },
  webServer: { command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 5178', url: 'http://127.0.0.1:5178', reuseExistingServer: true },
});
