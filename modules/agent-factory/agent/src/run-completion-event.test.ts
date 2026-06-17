/**
 * Tests for run-completion event — Issue #1580
 *
 * Validates:
 * 1. buildRunCompletionEvent produces valid schema from synthetic inputs
 * 2. deriveOutcome maps signals to outcome enum correctly (never self-report)
 * 3. Secret scrubbing removes planted secrets from narrative/errors
 * 4. Size caps are enforced (files_touched, errors, narrative)
 * 5. emitRunCompletionEvent is non-blocking (swallows errors)
 * 6. Feature flag gating
 * 7. getFilesTouched handles failures gracefully
 */

import {
  buildRunCompletionEvent,
  deriveOutcome,
  scrubField,
  emitRunCompletionEvent,
  isRunCompletionEventEnabled,
  getFilesTouched,
  BuildRunCompletionEventInput,
  RunCompletionEvent,
} from './run-completion-event';

// ============================================================================
// Test fixtures
// ============================================================================

function makeBaseInput(overrides?: Partial<BuildRunCompletionEventInput>): BuildRunCompletionEventInput {
  return {
    runId: 'agent-developer-issue-1580-1718640000000',
    persona: 'developer',
    ownerSub: '44086498-2091-70e1-bd3a-12c6104c3ebb',
    tenantId: 'org-acme-123',
    issueNumber: 1580,
    issueTitle: 'Design: structured run-completion event',
    repo: 'aws-e/adp',
    component: 'agent-factory',
    agentSucceeded: true,
    turns: 14,
    durationMs: 412000,
    filesTouched: ['modules/agent-factory/agent/src/run-completion-event.ts'],
    skillsUsed: ['code-review'],
    pr: { number: 268, state: 'open' },
    agentOutput: `## Task Complete

### Learnings
- TypeScript strict mode catches null pointer bugs early
- Always run preflight checks before deploying`,
    ...overrides,
  };
}

// ============================================================================
// deriveOutcome tests
// ============================================================================

describe('deriveOutcome', () => {
  it('returns success when agent succeeded and no beads issues', () => {
    expect(deriveOutcome({ agentSucceeded: true })).toBe('success');
  });

  it('returns success when agent succeeded and beads status is complete', () => {
    expect(deriveOutcome({ agentSucceeded: true, beadsStatus: 'complete' })).toBe('success');
  });

  it('returns failure when agent did not succeed', () => {
    expect(deriveOutcome({ agentSucceeded: false })).toBe('failure');
  });

  it('returns failure even when beads says complete but agent failed', () => {
    expect(deriveOutcome({ agentSucceeded: false, beadsStatus: 'complete' })).toBe('failure');
  });

  it('returns partial when agent succeeded but beads is blocked', () => {
    expect(deriveOutcome({ agentSucceeded: true, beadsStatus: 'blocked' })).toBe('partial');
  });

  it('returns partial when agent succeeded but beads is failed', () => {
    expect(deriveOutcome({ agentSucceeded: true, beadsStatus: 'failed' })).toBe('partial');
  });
});

// ============================================================================
// scrubField tests
// ============================================================================

describe('scrubField', () => {
  it('returns normal text unchanged', () => {
    expect(scrubField('Normal text about gateway port 8443')).toBe('Normal text about gateway port 8443');
  });

  it('redacts text containing AWS access keys', () => {
    expect(scrubField('Key was AKIAIOSFODNN7EXAMPLE in config')).toBe('[REDACTED — contains secret pattern]');
  });

  it('redacts text containing GitHub tokens', () => {
    expect(scrubField('Token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij')).toBe('[REDACTED — contains secret pattern]');
  });

  it('redacts text containing JWTs', () => {
    const jwt = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U';
    expect(scrubField(`Bearer ${jwt}`)).toBe('[REDACTED — contains secret pattern]');
  });

  it('returns empty string unchanged', () => {
    expect(scrubField('')).toBe('');
  });
});

// ============================================================================
// buildRunCompletionEvent tests
// ============================================================================

describe('buildRunCompletionEvent', () => {
  it('produces a valid event with all required fields', () => {
    const event = buildRunCompletionEvent(makeBaseInput());

    expect(event.schema_version).toBe('1');
    expect(event.run_id).toBe('agent-developer-issue-1580-1718640000000');
    expect(event.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(event.persona).toBe('developer');
    expect(event.owner_sub).toBe('44086498-2091-70e1-bd3a-12c6104c3ebb');
    expect(event.tenant_id).toBe('org-acme-123');
    expect(event.issue.number).toBe(1580);
    expect(event.issue.title).toBe('Design: structured run-completion event');
    expect(event.issue.repo).toBe('aws-e/adp');
    expect(event.issue.component).toBe('agent-factory');
    expect(event.outcome).toBe('success');
    expect(event.turns).toBe(14);
    expect(event.duration_ms).toBe(412000);
    expect(event.files_touched).toEqual(['modules/agent-factory/agent/src/run-completion-event.ts']);
    expect(event.skills_used).toEqual(['code-review']);
    expect(event.pr).toEqual({ number: 268, state: 'open' });
  });

  it('extracts agent_narrative from ### Learnings section', () => {
    const event = buildRunCompletionEvent(makeBaseInput());

    expect(event.agent_narrative).toContain('TypeScript strict mode catches null pointer bugs early');
    expect(event.agent_narrative).toContain('Always run preflight checks before deploying');
  });

  it('omits agent_narrative when no Learnings section exists', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      agentOutput: '## Task Complete\n\n### What Was Done\n- Built the feature',
    }));

    expect(event.agent_narrative).toBeUndefined();
  });

  it('omits agent_narrative when agent output is empty', () => {
    const event = buildRunCompletionEvent(makeBaseInput({ agentOutput: '' }));
    expect(event.agent_narrative).toBeUndefined();
  });

  it('omits agent_narrative when agent output is undefined', () => {
    const event = buildRunCompletionEvent(makeBaseInput({ agentOutput: undefined }));
    expect(event.agent_narrative).toBeUndefined();
  });

  it('derives outcome from agentSucceeded, not from prose', () => {
    // Even if agent says "success" in prose, outcome comes from boolean
    const failEvent = buildRunCompletionEvent(makeBaseInput({
      agentSucceeded: false,
      agentOutput: '### Learnings\n- Everything succeeded perfectly',
    }));
    expect(failEvent.outcome).toBe('failure');
  });

  it('includes beads info when task ID is present', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      beadsTaskId: 'task-abc-123',
      beadsStatus: 'complete',
    }));

    expect(event.beads).toEqual({ task_id: 'task-abc-123', status: 'complete' });
  });

  it('omits beads info when task ID is not present', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      beadsTaskId: undefined,
    }));

    expect(event.beads).toBeUndefined();
  });

  it('includes test results when provided', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      tests: { ran: true, passed: 95, failed: 1 },
    }));

    expect(event.tests).toEqual({ ran: true, passed: 95, failed: 1 });
  });

  it('omits optional identity fields when not provided', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      ownerSub: undefined,
      tenantId: undefined,
    }));

    expect(event.owner_sub).toBeUndefined();
    expect(event.tenant_id).toBeUndefined();
  });

  it('defaults to developer persona for unknown values', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      persona: 'unknown-persona',
    }));

    expect(event.persona).toBe('developer');
  });

  it('generates a run_id when not provided', () => {
    const event = buildRunCompletionEvent(makeBaseInput({ runId: undefined }));
    expect(event.run_id).toMatch(/^agent-developer-issue-1580-/);
  });

  describe('size caps', () => {
    it('caps files_touched at 50 entries', () => {
      const manyFiles = Array.from({ length: 100 }, (_, i) => `src/file-${i}.ts`);
      const event = buildRunCompletionEvent(makeBaseInput({ filesTouched: manyFiles }));
      expect(event.files_touched.length).toBeLessThanOrEqual(50);
    });

    it('caps errors_encountered at 5 entries', () => {
      const manyErrors = Array.from({ length: 10 }, (_, i) => ({
        summary: `Error number ${i}`,
        resolved: i % 2 === 0,
      }));
      const event = buildRunCompletionEvent(makeBaseInput({ errorsEncountered: manyErrors }));
      expect(event.errors_encountered.length).toBeLessThanOrEqual(5);
    });

    it('caps error summary length at 200 characters', () => {
      const longError = { summary: 'A'.repeat(500), resolved: true };
      const event = buildRunCompletionEvent(makeBaseInput({ errorsEncountered: [longError] }));
      expect(event.errors_encountered[0].summary.length).toBeLessThanOrEqual(200);
    });

    it('caps agent_narrative at 2000 characters', () => {
      const manyLearnings = Array.from(
        { length: 100 },
        (_, i) => `- Learning ${i}: ${'x'.repeat(50)}`,
      ).join('\n');
      const event = buildRunCompletionEvent(makeBaseInput({
        agentOutput: `### Learnings\n${manyLearnings}`,
      }));

      if (event.agent_narrative) {
        expect(event.agent_narrative.length).toBeLessThanOrEqual(2000);
      }
    });

    it('caps run_id at 128 characters', () => {
      const longId = 'x'.repeat(200);
      const event = buildRunCompletionEvent(makeBaseInput({ runId: longId }));
      expect(event.run_id.length).toBeLessThanOrEqual(128);
    });
  });

  describe('secret scrubbing', () => {
    it('omits agent_narrative containing secrets', () => {
      const event = buildRunCompletionEvent(makeBaseInput({
        agentOutput: `### Learnings
- The key AKIAIOSFODNN7EXAMPLE was needed for access`,
      }));

      expect(event.agent_narrative).toBeUndefined();
    });

    it('filters out file paths containing secret patterns', () => {
      const event = buildRunCompletionEvent(makeBaseInput({
        filesTouched: [
          'src/normal-file.ts',
          'config with ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij',
        ],
      }));

      expect(event.files_touched).toEqual(['src/normal-file.ts']);
    });

    it('filters out error summaries containing secrets', () => {
      const event = buildRunCompletionEvent(makeBaseInput({
        errorsEncountered: [
          { summary: 'Normal error about timeout', resolved: true },
          { summary: 'Token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij expired', resolved: false },
        ],
      }));

      expect(event.errors_encountered.length).toBe(1);
      expect(event.errors_encountered[0].summary).toBe('Normal error about timeout');
    });
  });

  describe('edge cases', () => {
    it('handles negative turns gracefully (clamps to 0)', () => {
      const event = buildRunCompletionEvent(makeBaseInput({ turns: -5 }));
      expect(event.turns).toBe(0);
    });

    it('handles negative duration gracefully (clamps to 0)', () => {
      const event = buildRunCompletionEvent(makeBaseInput({ durationMs: -100 }));
      expect(event.duration_ms).toBe(0);
    });

    it('handles empty arrays for files and skills', () => {
      const event = buildRunCompletionEvent(makeBaseInput({
        filesTouched: [],
        skillsUsed: [],
      }));

      expect(event.files_touched).toEqual([]);
      expect(event.skills_used).toEqual([]);
    });

    it('handles missing optional fields gracefully', () => {
      const minimalInput: BuildRunCompletionEventInput = {
        persona: 'architect',
        issueNumber: 42,
        issueTitle: 'Test issue',
        repo: 'test/repo',
        agentSucceeded: true,
        turns: 3,
        durationMs: 10000,
        filesTouched: [],
        skillsUsed: [],
      };

      const event = buildRunCompletionEvent(minimalInput);

      expect(event.schema_version).toBe('1');
      expect(event.persona).toBe('architect');
      expect(event.issue.number).toBe(42);
      expect(event.outcome).toBe('success');
      expect(event.turns).toBe(3);
      expect(event.duration_ms).toBe(10000);
      expect(event.owner_sub).toBeUndefined();
      expect(event.tenant_id).toBeUndefined();
      expect(event.pr).toBeUndefined();
      expect(event.beads).toBeUndefined();
      expect(event.tests).toBeUndefined();
      expect(event.agent_narrative).toBeUndefined();
    });

    it('produces valid JSON under 10KB for typical input', () => {
      const event = buildRunCompletionEvent(makeBaseInput());
      const json = JSON.stringify(event);
      expect(json.length).toBeLessThan(10240); // 10 KB
    });
  });
});

// ============================================================================
// emitRunCompletionEvent tests (non-blocking)
// ============================================================================

describe('emitRunCompletionEvent', () => {
  it('logs the event at INFO level', async () => {
    const logFn = jest.fn();
    const event = buildRunCompletionEvent(makeBaseInput());

    await emitRunCompletionEvent(event, logFn);

    expect(logFn).toHaveBeenCalledWith(
      'INFO',
      '[run-completion-event] Event emitted',
      expect.objectContaining({ run_completion_event: event }),
    );
  });

  it('does not throw when log function throws', async () => {
    const throwingLog = jest.fn().mockImplementation(() => {
      throw new Error('Log failed');
    });
    const event = buildRunCompletionEvent(makeBaseInput());

    // Should not throw
    await expect(emitRunCompletionEvent(event, throwingLog)).resolves.toBeUndefined();
  });

  it('does not throw for any error', async () => {
    const logFn = jest.fn().mockImplementationOnce(() => {
      throw new Error('First call fails');
    }).mockImplementation(() => {
      // Second call (WARN) also fails
      throw new Error('Second call also fails');
    });
    const event = buildRunCompletionEvent(makeBaseInput());

    await expect(emitRunCompletionEvent(event, logFn)).resolves.toBeUndefined();
  });
});

// ============================================================================
// isRunCompletionEventEnabled tests
// ============================================================================

describe('isRunCompletionEventEnabled', () => {
  const originalEnv = process.env.RUN_COMPLETION_EVENT_ENABLED;

  afterEach(() => {
    if (originalEnv === undefined) {
      delete process.env.RUN_COMPLETION_EVENT_ENABLED;
    } else {
      process.env.RUN_COMPLETION_EVENT_ENABLED = originalEnv;
    }
  });

  it('returns false by default (env not set)', () => {
    delete process.env.RUN_COMPLETION_EVENT_ENABLED;
    expect(isRunCompletionEventEnabled()).toBe(false);
  });

  it('returns false when set to "false"', () => {
    process.env.RUN_COMPLETION_EVENT_ENABLED = 'false';
    expect(isRunCompletionEventEnabled()).toBe(false);
  });

  it('returns true when set to "true"', () => {
    process.env.RUN_COMPLETION_EVENT_ENABLED = 'true';
    expect(isRunCompletionEventEnabled()).toBe(true);
  });

  it('returns false for any other value', () => {
    process.env.RUN_COMPLETION_EVENT_ENABLED = '1';
    expect(isRunCompletionEventEnabled()).toBe(false);
  });
});

// ============================================================================
// getFilesTouched tests
// ============================================================================

describe('getFilesTouched', () => {
  it('returns empty array when git command fails', async () => {
    // Use a non-existent directory to make git fail
    const result = await getFilesTouched('/nonexistent-dir-12345');
    expect(result).toEqual([]);
  });

  it('never throws', async () => {
    await expect(getFilesTouched('/nonexistent-dir-12345')).resolves.toBeDefined();
  });
});
