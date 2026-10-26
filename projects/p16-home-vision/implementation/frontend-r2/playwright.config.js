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
    reuseExistingServer: process.env.P16_SOAK_WRAPPER_CHILD === '1',
    timeout: 30_000,
  },
})
