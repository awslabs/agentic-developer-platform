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
  sanitizeReflection,
  computeReflectionConsistency,
  emitRunCompletionEvent,
  isRunCompletionEventEnabled,
  getFilesTouched,
  BuildRunCompletionEventInput,
  RunCompletionEvent,
  RunReflection,
  ReflectionConsistency,
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

// ============================================================================
// Reflection fixtures
// ============================================================================

function makeBaseReflection(overrides?: Partial<RunReflection>): RunReflection {
  return {
    problem: 'RDS connection timeout on fresh EKS deploy',
    approach: 'Checked security groups, found missing inbound rule for port 5432',
    failures: [
      {
        what: 'kubectl apply failed with connection refused',
        why: 'Security group did not allow EKS node → RDS traffic',
        signal: 'Pod CrashLoopBackOff with "connection refused" in logs',
      },
    ],
    recovery: [
      {
        from: 'kubectl apply failed with connection refused',
        fix: 'Added ingress rule for EKS node security group on port 5432',
      },
    ],
    advice: [
      'Always verify security group rules between EKS and RDS before deploying',
      'Check kubectl logs with --previous flag for CrashLoopBackOff pods',
    ],
    confidence: 'high',
    reusable: true,
    ...overrides,
  };
}

// ============================================================================
// sanitizeReflection tests
// ============================================================================

describe('sanitizeReflection', () => {
  it('returns sanitized reflection for clean input', () => {
    const result = sanitizeReflection(makeBaseReflection());
    expect(result).toBeDefined();
    expect(result!.problem).toBe('RDS connection timeout on fresh EKS deploy');
    expect(result!.approach).toContain('security groups');
    expect(result!.failures).toHaveLength(1);
    expect(result!.recovery).toHaveLength(1);
    expect(result!.advice).toHaveLength(2);
    expect(result!.confidence).toBe('high');
    expect(result!.reusable).toBe(true);
  });

  it('returns undefined when problem contains a secret', () => {
    const result = sanitizeReflection(makeBaseReflection({
      problem: 'Used key AKIAIOSFODNN7EXAMPLE to connect',
    }));
    expect(result).toBeUndefined();
  });

  it('returns undefined when approach contains a secret', () => {
    const result = sanitizeReflection(makeBaseReflection({
      approach: 'Token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij was needed',
    }));
    expect(result).toBeUndefined();
  });

  it('filters out failures with secrets in any sub-field', () => {
    const result = sanitizeReflection(makeBaseReflection({
      failures: [
        { what: 'Normal failure', why: 'Normal cause', signal: 'Normal signal' },
        { what: 'Token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij expired', why: 'expired', signal: 'auth error' },
      ],
    }));
    expect(result).toBeDefined();
    expect(result!.failures).toHaveLength(1);
    expect(result!.failures[0].what).toBe('Normal failure');
  });

  it('filters out recovery entries with secrets', () => {
    const result = sanitizeReflection(makeBaseReflection({
      recovery: [
        { from: 'Normal', fix: 'Normal fix' },
        { from: 'Normal', fix: 'Set sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 as env' },
      ],
    }));
    expect(result).toBeDefined();
    expect(result!.recovery).toHaveLength(1);
  });

  it('filters out advice containing secrets', () => {
    const result = sanitizeReflection(makeBaseReflection({
      advice: [
        'Normal advice',
        'Use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij for auth',
      ],
    }));
    expect(result).toBeDefined();
    expect(result!.advice).toHaveLength(1);
    expect(result!.advice[0]).toBe('Normal advice');
  });

  it('caps failures at 10 entries', () => {
    const manyFailures = Array.from({ length: 15 }, (_, i) => ({
      what: `Failure ${i}`, why: `Cause ${i}`, signal: `Signal ${i}`,
    }));
    const result = sanitizeReflection(makeBaseReflection({ failures: manyFailures }));
    expect(result).toBeDefined();
    expect(result!.failures).toHaveLength(10);
  });

  it('caps recovery at 10 entries', () => {
    const manyRecovery = Array.from({ length: 15 }, (_, i) => ({
      from: `Failure ${i}`, fix: `Fix ${i}`,
    }));
    const result = sanitizeReflection(makeBaseReflection({ recovery: manyRecovery }));
    expect(result).toBeDefined();
    expect(result!.recovery).toHaveLength(10);
  });

  it('caps advice at 10 entries', () => {
    const manyAdvice = Array.from({ length: 15 }, (_, i) => `Advice ${i}`);
    const result = sanitizeReflection(makeBaseReflection({ advice: manyAdvice }));
    expect(result).toBeDefined();
    expect(result!.advice).toHaveLength(10);
  });

  it('caps string fields at 300 characters', () => {
    const result = sanitizeReflection(makeBaseReflection({
      problem: 'X'.repeat(500),
      approach: 'Y'.repeat(500),
    }));
    expect(result).toBeDefined();
    expect(result!.problem.length).toBeLessThanOrEqual(300);
    expect(result!.approach.length).toBeLessThanOrEqual(300);
  });

  it('defaults invalid confidence to low', () => {
    const result = sanitizeReflection(makeBaseReflection({
      confidence: 'invalid' as any,
    }));
    expect(result).toBeDefined();
    expect(result!.confidence).toBe('low');
  });

  it('coerces reusable to boolean', () => {
    const result = sanitizeReflection(makeBaseReflection({
      reusable: 'yes' as any,
    }));
    expect(result).toBeDefined();
    expect(result!.reusable).toBe(true);

    const result2 = sanitizeReflection(makeBaseReflection({
      reusable: '' as any,
    }));
    expect(result2).toBeDefined();
    expect(result2!.reusable).toBe(false);
  });

  it('handles empty arrays gracefully', () => {
    const result = sanitizeReflection(makeBaseReflection({
      failures: [],
      recovery: [],
      advice: [],
    }));
    expect(result).toBeDefined();
    expect(result!.failures).toEqual([]);
    expect(result!.recovery).toEqual([]);
    expect(result!.advice).toEqual([]);
  });
});

// ============================================================================
// computeReflectionConsistency tests
// ============================================================================

describe('computeReflectionConsistency', () => {
  it('returns consistent when reflection matches objective outcome', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ confidence: 'medium' }),
      outcome: 'success',
      turns: 14,
    });
    expect(result.outcome_mismatch).toBe(false);
    expect(result.underreported_failures).toBe(false);
    expect(result.verdict).toBe('consistent');
  });

  it('returns consistent when failures reported and outcome is failure', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ confidence: 'low' }),
      outcome: 'failure',
      turns: 50,
    });
    // Has failures reported → not underreported
    expect(result.underreported_failures).toBe(false);
    expect(result.verdict).toBe('consistent');
  });

  it('detects outcome_mismatch: no failures + high confidence but outcome is failure', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'high' }),
      outcome: 'failure',
      turns: 10,
    });
    expect(result.outcome_mismatch).toBe(true);
    expect(result.verdict).toBe('optimistic');
  });

  it('detects outcome_mismatch: no failures + high confidence but outcome is partial', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'high' }),
      outcome: 'partial',
      turns: 10,
    });
    expect(result.outcome_mismatch).toBe(true);
    expect(result.verdict).toBe('optimistic');
  });

  it('does NOT flag outcome_mismatch when failures are reported (agent is honest)', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ confidence: 'high' }),
      outcome: 'failure',
      turns: 10,
    });
    // Has failures → impliesSuccess is false
    expect(result.outcome_mismatch).toBe(false);
  });

  it('does NOT flag outcome_mismatch when confidence is not high', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'medium' }),
      outcome: 'failure',
      turns: 10,
    });
    expect(result.outcome_mismatch).toBe(false);
  });

  it('detects underreported_failures: no failures but high turn count', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'medium' }),
      outcome: 'success',
      turns: 45, // > 40 threshold
    });
    expect(result.underreported_failures).toBe(true);
    expect(result.verdict).toBe('optimistic');
  });

  it('detects underreported_failures: no failures but tests failed', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'medium' }),
      outcome: 'success',
      turns: 10,
      testsFailed: 3,
    });
    expect(result.underreported_failures).toBe(true);
    expect(result.verdict).toBe('optimistic');
  });

  it('does NOT flag underreported when failures ARE reported', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection(), // has 1 failure
      outcome: 'success',
      turns: 50,
      testsFailed: 2,
    });
    expect(result.underreported_failures).toBe(false);
  });

  it('returns unreliable when both outcome_mismatch AND underreported_failures', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'high' }),
      outcome: 'failure',
      turns: 50, // high turns + outcome failure + no failures + high confidence
    });
    expect(result.outcome_mismatch).toBe(true);
    expect(result.underreported_failures).toBe(true);
    expect(result.verdict).toBe('unreliable');
  });

  it('handles zero testsFailed (no signal)', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'medium' }),
      outcome: 'success',
      turns: 10,
      testsFailed: 0,
    });
    expect(result.underreported_failures).toBe(false);
    expect(result.verdict).toBe('consistent');
  });

  it('handles undefined testsFailed (no test info)', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'medium' }),
      outcome: 'success',
      turns: 10,
      testsFailed: undefined,
    });
    expect(result.underreported_failures).toBe(false);
  });

  it('boundary: turns exactly at threshold (40) is NOT flagged', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'medium' }),
      outcome: 'success',
      turns: 40,
    });
    expect(result.underreported_failures).toBe(false);
  });

  it('boundary: turns at threshold+1 (41) IS flagged', () => {
    const result = computeReflectionConsistency({
      reflection: makeBaseReflection({ failures: [], confidence: 'medium' }),
      outcome: 'success',
      turns: 41,
    });
    expect(result.underreported_failures).toBe(true);
  });
});

// ============================================================================
// buildRunCompletionEvent — reflection integration tests
// ============================================================================

describe('buildRunCompletionEvent with reflection', () => {
  it('includes reflection and consistency when reflection is provided', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      reflection: makeBaseReflection(),
    }));

    expect(event.reflection).toBeDefined();
    expect(event.reflection!.problem).toBe('RDS connection timeout on fresh EKS deploy');
    expect(event.consistency).toBeDefined();
    expect(event.consistency!.verdict).toBe('consistent');
  });

  it('omits reflection when not provided', () => {
    const event = buildRunCompletionEvent(makeBaseInput({ reflection: undefined }));
    expect(event.reflection).toBeUndefined();
    expect(event.consistency).toBeUndefined();
  });

  it('omits reflection when core fields contain secrets', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      reflection: makeBaseReflection({
        problem: 'Used AKIAIOSFODNN7EXAMPLE to access S3',
      }),
    }));
    expect(event.reflection).toBeUndefined();
    expect(event.consistency).toBeUndefined();
  });

  it('computes optimistic verdict for inconsistent reflection', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      agentSucceeded: true,
      turns: 50,  // high turns
      reflection: makeBaseReflection({
        failures: [],       // claims no failures
        confidence: 'medium',
      }),
    }));

    expect(event.consistency).toBeDefined();
    expect(event.consistency!.underreported_failures).toBe(true);
    expect(event.consistency!.verdict).toBe('optimistic');
  });

  it('computes unreliable verdict for highly inconsistent reflection', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      agentSucceeded: false,  // actual failure
      turns: 50,              // high turns
      reflection: makeBaseReflection({
        failures: [],        // claims no failures
        confidence: 'high',  // claims high confidence
      }),
    }));

    expect(event.consistency).toBeDefined();
    expect(event.consistency!.outcome_mismatch).toBe(true);
    expect(event.consistency!.underreported_failures).toBe(true);
    expect(event.consistency!.verdict).toBe('unreliable');
  });

  it('uses test failure signal for consistency check', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      agentSucceeded: true,
      turns: 10,
      tests: { ran: true, passed: 90, failed: 5 },
      reflection: makeBaseReflection({
        failures: [],
        confidence: 'medium',
      }),
    }));

    expect(event.consistency).toBeDefined();
    expect(event.consistency!.underreported_failures).toBe(true);
    expect(event.consistency!.verdict).toBe('optimistic');
  });

  it('event with reflection stays under 10KB', () => {
    const event = buildRunCompletionEvent(makeBaseInput({
      reflection: makeBaseReflection(),
    }));
    const json = JSON.stringify(event);
    expect(json.length).toBeLessThan(10240);
  });
});
