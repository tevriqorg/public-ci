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
    // Scenario wrappers must exercise the Vite process from this source tree.
    // Reusing an unrelated listener could make browser evidence source-ambiguous.
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
