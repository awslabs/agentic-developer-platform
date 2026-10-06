const { TestEnvironment } = require('jest-environment-node');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');

/** Keep mocked refreshes away from the credentials of the agent running Jest. */
module.exports = class TokenIsolatedEnvironment extends TestEnvironment {
  async setup() {
    await super.setup();
    // Runs before test imports (tokenFile captures this variable at import time).
    // Always replace an inherited path: it may point to the worker's live token.
    // Jest creates an environment per suite, including with --runInBand.
    this.tokenDirectory = mkdtempSync(join(tmpdir(), 'adp-agent-jest-token-'));
    this.global.process.env.ADP_TOKEN_FILE = join(this.tokenDirectory, 'github-token');
  }

  async teardown() {
    try {
      await super.teardown();
    } finally {
      // Outside the test sandbox: fs mocks and process.env resets cannot redirect
      // cleanup to a runtime credential or leave our original fixture behind.
      if (this.tokenDirectory) rmSync(this.tokenDirectory, { recursive: true, force: true });
    }
  }
};
