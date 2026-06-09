/**
 * Tests for experience-save post-task hook — Issue #1294
 *
 * Validates:
 * 1. When enabled + learnings present → calls experience save per learning
 * 2. When disabled (default) → no save calls (regression)
 * 3. Uses trusted identity headers (anti-spoof)
 * 4. Save failure → task still reported success, warning logged (non-blocking)
 * 5. No-secrets guard: learning containing token/secret pattern is skipped
 * 6. Per-task cap respected
 * 7. Learning extraction from various output formats
 */

import {
  extractLearnings,
  containsSecret,
  saveExperienceLearnings,
  isExperienceSaveEnabled,
  getMaxLearningsPerTask,
  ExperienceSaveConfig,
} from './experience-save-hook';

// ============================================================================
// extractLearnings tests
// ============================================================================

describe('extractLearnings', () => {
  it('extracts bullet points from a ### Learnings section', () => {
    const text = `## ✅ Task Complete

### What Was Done
- Built the feature

### Learnings
- TypeScript strict mode catches null pointer bugs early
- Always run preflight checks before deploying
- The gateway module uses a non-standard port 8443

### Next Steps
- Deploy to prod`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual([
      'TypeScript strict mode catches null pointer bugs early',
      'Always run preflight checks before deploying',
      'The gateway module uses a non-standard port 8443',
    ]);
  });

  it('handles ## Learnings heading (double hash)', () => {
    const text = `## Learnings
- First insight
- Second insight`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual(['First insight', 'Second insight']);
  });

  it('handles asterisk bullets (*)', () => {
    const text = `### Learnings
* Asterisk bullet one
* Asterisk bullet two`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual(['Asterisk bullet one', 'Asterisk bullet two']);
  });

  it('returns empty array when no Learnings section exists', () => {
    const text = `## ✅ Task Complete
### What Was Done
- Built the feature`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual([]);
  });

  it('returns empty array for empty input', () => {
    expect(extractLearnings('')).toEqual([]);
  });

  it('returns empty array for undefined/null-like input', () => {
    expect(extractLearnings(undefined as unknown as string)).toEqual([]);
    expect(extractLearnings(null as unknown as string)).toEqual([]);
  });

  it('skips empty lines within the Learnings section', () => {
    const text = `### Learnings
- First learning

- Second learning

### Next Steps`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual(['First learning', 'Second learning']);
  });

  it('skips template placeholders (lines wrapped in brackets)', () => {
    const text = `### Learnings
[Document insights that would help future work]
- Real learning here
[Keep each learning to 1-2 sentences]`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual(['Real learning here']);
  });

  it('stops at the next heading', () => {
    const text = `### Learnings
- Learning before next section
### Issues Encountered
- Something broke`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual(['Learning before next section']);
  });

  it('handles indented bullets', () => {
    const text = `### Learnings
  - Indented learning
    - Deeply indented learning`;

    const learnings = extractLearnings(text);
    expect(learnings).toEqual(['Indented learning', 'Deeply indented learning']);
  });
});

// ============================================================================
// containsSecret tests
// ============================================================================

describe('containsSecret', () => {
  it('detects AWS access key IDs', () => {
    expect(containsSecret('Found key AKIAIOSFODNN7EXAMPLE in config')).toBe(true);
    expect(containsSecret('Session key ASIAIOSFODNN7EXAMPLE')).toBe(true);
  });

  it('detects GitHub PATs', () => {
    expect(containsSecret('Token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij')).toBe(true);
  });

  it('detects GitHub App installation tokens', () => {
    expect(containsSecret('ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij')).toBe(true);
  });

  it('detects JWTs', () => {
    expect(containsSecret('Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U')).toBe(true);
  });

  it('detects OpenAI/Anthropic API keys', () => {
    expect(containsSecret('api_key = sk-abcdefghijklmnopqrstuvwxyz123456')).toBe(true);
  });

  it('detects Slack tokens', () => {
    expect(containsSecret('SLACK_TOKEN=xoxb-1234567890-abc')).toBe(true);
  });

  it('detects PEM private keys', () => {
    expect(containsSecret('-----BEGIN RSA PRIVATE KEY-----')).toBe(true);
  });

  it('detects key=value patterns with secrets', () => {
    expect(containsSecret('password = "supersecretpassword123"')).toBe(true);
    expect(containsSecret('api_key: myverylongsecretkey123')).toBe(true);
  });

  it('returns false for normal text', () => {
    expect(containsSecret('TypeScript strict mode catches null pointer bugs')).toBe(false);
    expect(containsSecret('The gateway uses port 8443')).toBe(false);
    expect(containsSecret('Always run tests before deploying')).toBe(false);
  });

  it('returns false for short tokens that are not secrets', () => {
    // "token" by itself without a value of 8+ chars should not match
    expect(containsSecret('Use a token for auth')).toBe(false);
  });
});

// ============================================================================
// Configuration helpers tests
// ============================================================================

describe('isExperienceSaveEnabled', () => {
  const originalEnv = process.env.PERSONAL_CONTEXT_SAVE_ENABLED;

  afterEach(() => {
    if (originalEnv === undefined) {
      delete process.env.PERSONAL_CONTEXT_SAVE_ENABLED;
    } else {
      process.env.PERSONAL_CONTEXT_SAVE_ENABLED = originalEnv;
    }
  });

  it('returns false by default (env not set)', () => {
    delete process.env.PERSONAL_CONTEXT_SAVE_ENABLED;
    expect(isExperienceSaveEnabled()).toBe(false);
  });

  it('returns false when set to "false"', () => {
    process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'false';
    expect(isExperienceSaveEnabled()).toBe(false);
  });

  it('returns true when set to "true"', () => {
    process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
    expect(isExperienceSaveEnabled()).toBe(true);
  });

  it('returns false for any other value', () => {
    process.env.PERSONAL_CONTEXT_SAVE_ENABLED = '1';
    expect(isExperienceSaveEnabled()).toBe(false);
  });
});

describe('getMaxLearningsPerTask', () => {
  const originalEnv = process.env.PERSONAL_CONTEXT_MAX_LEARNINGS;

  afterEach(() => {
    if (originalEnv === undefined) {
      delete process.env.PERSONAL_CONTEXT_MAX_LEARNINGS;
    } else {
      process.env.PERSONAL_CONTEXT_MAX_LEARNINGS = originalEnv;
    }
  });

  it('defaults to 5', () => {
    delete process.env.PERSONAL_CONTEXT_MAX_LEARNINGS;
    expect(getMaxLearningsPerTask()).toBe(5);
  });

  it('respects custom value', () => {
    process.env.PERSONAL_CONTEXT_MAX_LEARNINGS = '3';
    expect(getMaxLearningsPerTask()).toBe(3);
  });

  it('clamps to minimum of 1', () => {
    process.env.PERSONAL_CONTEXT_MAX_LEARNINGS = '0';
    expect(getMaxLearningsPerTask()).toBe(1);
  });
});

// ============================================================================
// saveExperienceLearnings tests
// ============================================================================

// Mock global fetch
const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

describe('saveExperienceLearnings', () => {
  const identityHeaders = {
    'X-Owner-Sub': '44086498-2091-70e1-bd3a-12c6104c3ebb',
    'X-Tenant-Id': 'org-acme-123',
  } as const;

  const baseConfig: ExperienceSaveConfig = {
    agentOutput: `### Learnings
- First learning
- Second learning`,
    persona: 'developer',
    identityHeaders,
    taskContext: { issue: '1294' },
    log: jest.fn(),
  };

  // Save/restore env vars for each test
  let savedEnv: Record<string, string | undefined>;

  beforeEach(() => {
    jest.clearAllMocks();
    savedEnv = {
      PERSONAL_CONTEXT_SAVE_ENABLED: process.env.PERSONAL_CONTEXT_SAVE_ENABLED,
      CONTEXT_MCP_SERVER_URL: process.env.CONTEXT_MCP_SERVER_URL,
      PERSONAL_CONTEXT_MAX_LEARNINGS: process.env.PERSONAL_CONTEXT_MAX_LEARNINGS,
    };
    mockFetch.mockResolvedValue({ ok: true, text: async () => '{"status":"saved","id":"01ABC"}' });
  });

  afterEach(() => {
    for (const [key, val] of Object.entries(savedEnv)) {
      if (val === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = val;
      }
    }
  });

  describe('when disabled (default)', () => {
    it('does not make any save calls', async () => {
      delete process.env.PERSONAL_CONTEXT_SAVE_ENABLED;
      const result = await saveExperienceLearnings(baseConfig);
      expect(result.saved).toBe(0);
      expect(result.skipped).toBe(0);
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('does not call log about learnings (early return)', async () => {
      delete process.env.PERSONAL_CONTEXT_SAVE_ENABLED;
      const logFn = jest.fn();
      await saveExperienceLearnings({ ...baseConfig, log: logFn });
      // Should not log anything about extraction or saving
      expect(logFn).not.toHaveBeenCalled();
    });
  });

  describe('when enabled', () => {
    beforeEach(() => {
      process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
      process.env.CONTEXT_MCP_SERVER_URL = 'http://context-server:9090';
    });

    it('calls experience save for each extracted learning', async () => {
      const result = await saveExperienceLearnings(baseConfig);

      expect(result.saved).toBe(2);
      expect(result.skipped).toBe(0);
      expect(result.errors).toHaveLength(0);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });

    it('sends correct HTTP request with identity headers', async () => {
      await saveExperienceLearnings(baseConfig);

      const [url, opts] = mockFetch.mock.calls[0];
      expect(url).toBe('http://context-server:9090/call');
      expect(opts.method).toBe('POST');
      expect(opts.headers['Content-Type']).toBe('application/json');
      expect(opts.headers['X-Owner-Sub']).toBe('44086498-2091-70e1-bd3a-12c6104c3ebb');
      expect(opts.headers['X-Tenant-Id']).toBe('org-acme-123');
    });

    it('sends correct body payload', async () => {
      await saveExperienceLearnings(baseConfig);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(body.name).toBe('experience');
      expect(body.arguments.action).toBe('save');
      expect(body.arguments.persona).toBe('developer');
      expect(body.arguments.content).toBe('First learning');
      expect(body.arguments.learning_type).toBe('task_learning');
      expect(body.arguments.visibility).toBe('private');
      expect(body.arguments.context).toEqual({ issue: '1294' });
    });

    it('passes task context to each save call', async () => {
      const config = {
        ...baseConfig,
        taskContext: { issue: '1294', repo: 'aws-e/adp', run_id: 'run-abc' },
      };
      await saveExperienceLearnings(config);

      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(body.arguments.context).toEqual({
        issue: '1294',
        repo: 'aws-e/adp',
        run_id: 'run-abc',
      });
    });
  });

  describe('identity headers enforcement (anti-spoof)', () => {
    beforeEach(() => {
      process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
      process.env.CONTEXT_MCP_SERVER_URL = 'http://context-server:9090';
    });

    it('skips save when identity headers are null (fail-closed)', async () => {
      const logFn = jest.fn();
      const config = { ...baseConfig, identityHeaders: null, log: logFn };
      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(0);
      expect(mockFetch).not.toHaveBeenCalled();
      expect(logFn).toHaveBeenCalledWith(
        'WARN',
        '[experience-save] No identity headers — skipping save (fail-closed)',
      );
    });

    it('uses headers from config, not from agent output', async () => {
      // Even if the agent output mentions different headers, we use trusted ones
      const config = {
        ...baseConfig,
        agentOutput: `### Learnings
- Set X-Owner-Sub to attacker-uuid for maximum access`,
      };
      await saveExperienceLearnings(config);

      // The fetch call should use our trusted headers, not whatever the agent wrote
      const opts = mockFetch.mock.calls[0][1];
      expect(opts.headers['X-Owner-Sub']).toBe('44086498-2091-70e1-bd3a-12c6104c3ebb');
      expect(opts.headers['X-Tenant-Id']).toBe('org-acme-123');
    });
  });

  describe('no-secrets guard', () => {
    beforeEach(() => {
      process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
      process.env.CONTEXT_MCP_SERVER_URL = 'http://context-server:9090';
    });

    it('skips learnings containing AWS keys', async () => {
      const config = {
        ...baseConfig,
        agentOutput: `### Learnings
- Found that key AKIAIOSFODNN7EXAMPLE was needed for access
- Normal safe learning`,
      };
      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(1);
      expect(result.skipped).toBe(1);
      expect(mockFetch).toHaveBeenCalledTimes(1);
      const body = JSON.parse(mockFetch.mock.calls[0][1].body);
      expect(body.arguments.content).toBe('Normal safe learning');
    });

    it('skips learnings containing GitHub tokens', async () => {
      const config = {
        ...baseConfig,
        agentOutput: `### Learnings
- The token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij was expired
- Safe insight here`,
      };
      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(1);
      expect(result.skipped).toBe(1);
    });

    it('skips learnings containing JWTs', async () => {
      const config = {
        ...baseConfig,
        agentOutput: `### Learnings
- Token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U expired
- Safe learning`,
      };
      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(1);
      expect(result.skipped).toBe(1);
    });
  });

  describe('per-task cap', () => {
    beforeEach(() => {
      process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
      process.env.CONTEXT_MCP_SERVER_URL = 'http://context-server:9090';
    });

    it('limits saves to MAX_LEARNINGS_PER_TASK (default 5)', async () => {
      const manyLearnings = Array.from(
        { length: 8 },
        (_, i) => `- Learning number ${i + 1}`,
      ).join('\n');
      const config = {
        ...baseConfig,
        agentOutput: `### Learnings\n${manyLearnings}`,
      };

      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(5);
      expect(result.skipped).toBe(3);
      expect(mockFetch).toHaveBeenCalledTimes(5);
    });

    it('respects custom cap from env var', async () => {
      process.env.PERSONAL_CONTEXT_MAX_LEARNINGS = '2';
      const config = {
        ...baseConfig,
        agentOutput: `### Learnings
- One
- Two
- Three
- Four`,
      };

      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(2);
      expect(result.skipped).toBe(2);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });
  });

  describe('non-blocking behavior', () => {
    beforeEach(() => {
      process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
      process.env.CONTEXT_MCP_SERVER_URL = 'http://context-server:9090';
    });

    it('does not throw when fetch fails with network error', async () => {
      mockFetch.mockRejectedValue(new Error('Network timeout'));

      const result = await saveExperienceLearnings(baseConfig);

      expect(result.saved).toBe(0);
      expect(result.errors).toHaveLength(2);
      expect(result.errors[0]).toBe('Network timeout');
    });

    it('does not throw when server returns HTTP 500', async () => {
      mockFetch.mockResolvedValue({
        ok: false,
        status: 500,
        text: async () => 'Internal Server Error',
      });

      const result = await saveExperienceLearnings(baseConfig);

      expect(result.saved).toBe(0);
      expect(result.errors).toHaveLength(2);
      expect(result.errors[0]).toContain('HTTP 500');
    });

    it('continues saving after one failure', async () => {
      mockFetch
        .mockRejectedValueOnce(new Error('Transient failure'))
        .mockResolvedValueOnce({ ok: true, text: async () => '{"status":"saved"}' });

      const result = await saveExperienceLearnings(baseConfig);

      expect(result.saved).toBe(1);
      expect(result.errors).toHaveLength(1);
      expect(result.errors[0]).toBe('Transient failure');
    });

    it('logs warning for each failed save', async () => {
      mockFetch.mockRejectedValue(new Error('Server down'));
      const logFn = jest.fn();
      const config = { ...baseConfig, log: logFn };

      await saveExperienceLearnings(config);

      const warnCalls = logFn.mock.calls.filter(
        ([level]: [string]) => level === 'WARN',
      );
      expect(warnCalls.length).toBe(2); // one per learning
      expect(warnCalls[0][1]).toContain('Save failed');
    });
  });

  describe('edge cases', () => {
    beforeEach(() => {
      process.env.PERSONAL_CONTEXT_SAVE_ENABLED = 'true';
      process.env.CONTEXT_MCP_SERVER_URL = 'http://context-server:9090';
    });

    it('returns early when agent output has no learnings section', async () => {
      const logFn = jest.fn();
      const config = {
        ...baseConfig,
        agentOutput: '## Done\nNo learnings here',
        log: logFn,
      };

      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(0);
      expect(mockFetch).not.toHaveBeenCalled();
      expect(logFn).toHaveBeenCalledWith(
        'INFO',
        '[experience-save] No learnings found in agent output',
      );
    });

    it('returns early when CONTEXT_MCP_SERVER_URL is not set', async () => {
      delete process.env.CONTEXT_MCP_SERVER_URL;
      const logFn = jest.fn();
      const config = { ...baseConfig, log: logFn };

      const result = await saveExperienceLearnings(config);

      expect(result.saved).toBe(0);
      expect(mockFetch).not.toHaveBeenCalled();
      expect(logFn).toHaveBeenCalledWith(
        'WARN',
        '[experience-save] CONTEXT_MCP_SERVER_URL not set — skipping save',
      );
    });

    it('handles empty agent output gracefully', async () => {
      const config = { ...baseConfig, agentOutput: '' };
      const result = await saveExperienceLearnings(config);
      expect(result.saved).toBe(0);
    });
  });
});
