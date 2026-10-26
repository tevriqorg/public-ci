import { defineConfig } from '@playwright/test'

export default defineConfig({
  globalSetup: './tests/soak-scenarios.global-setup.js',
  outputDir: '../.tmp/playwright-test-results',
  testDir: './tests',
  testMatch: 'browser.acceptance.spec.js',
  timeout: 30_000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:4173',
    browserName: 'chromium',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 4173',
    url: 'http://127.0.0.1:4173',
    // Only hosted matrix children may reuse the parent Playwright run's Vite
    // server. A direct CLI replay always starts this checkout's own server.
    reuseExistingServer: process.env.P16_SOAK_MATRIX_CHILD === '1',
    timeout: 30_000,
  },
})
