// Enforce the per-scenario browser wrapper contract. Playwright's process exit
// status alone can hide an accidentally skipped or unselected tagged test.
export default class SoakExecutionReporter {
  constructor() {
    this.tag = process.env.P16_SOAK_EXPECTED_PLAYWRIGHT_TAG || ''
    this.selectedCount = 0
    this.outcomes = []
  }

  onBegin(_config, suite) {
    this.selectedCount = suite.allTests().filter((test) => test.title.includes(this.tag)).length
  }

  onTestEnd(test, result) {
    if (test.title.includes(this.tag)) this.outcomes.push(result.status)
  }

  onEnd(result) {
    if (!/^@S0(24|25)$/.test(this.tag) || this.selectedCount !== 1 ||
        this.outcomes.length !== 1 || this.outcomes[0] !== 'passed' || result.status !== 'passed') {
      console.error(`P16_SOAK_PLAYWRIGHT_EXECUTION_INVALID tag=${this.tag || 'missing'} selected=${this.selectedCount} outcomes=${this.outcomes.join(',') || 'none'} status=${result.status}`)
      return { status: 'failed' }
    }
    return undefined
  }
}
