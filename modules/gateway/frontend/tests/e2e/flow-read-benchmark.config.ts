import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'flow-read-benchmark.spec.ts',
  outputDir: process.env.FLOW_READ_ARTIFACT_DIR ?? 'test-results/flow-read-benchmark',
  workers: 1,
  retries: 0,
  timeout: 20 * 60_000,
  use: { headless: true, browserName: 'chromium', trace: 'off', screenshot: 'off' },
  webServer: process.env.FLOW_READ_MOCK === '1' ? {
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 5187 --strictPort',
    url: 'http://127.0.0.1:5187',
    reuseExistingServer: false,
    timeout: 120_000,
  } : undefined,
});
