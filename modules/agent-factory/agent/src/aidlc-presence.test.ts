/**
 * Unit tests for AIDLC Presence — synthetic HUMAN_TURN on gate resume.
 * (Issue #3232, EPIC #3158 — hardening wave, Decision 3.)
 *
 * Covers:
 * - Event shape matches fixture template
 * - Author propagation from trigger comment
 * - Version gate: known version → writes; unknown → skips with log
 * - Non-AIDLC no-op path
 * - No pending gate → no-op
 * - Missing trigger comment → no-op
 * - Audit shard append vs create
 * - extractGateAnswerComment logic
 */

import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import {
  mintSyntheticPresence,
  checkVersion,
  findPendingGateStage,
  buildHumanTurnEvent,
  resolveVerifiedGateAnswer,
  KNOWN_AIDLC_VERSIONS,
  PresenceDeps,
  TriggerComment,
} from './aidlc-presence';

// ---------------------------------------------------------------------------
// Test Helpers
// ---------------------------------------------------------------------------

function createTempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'aidlc-presence-test-'));
}

function createMockDeps(cwd: string): PresenceDeps {
  return {
    cwd,
    log: jest.fn(),
  };
}

function writeFile(dir: string, relativePath: string, content: string): void {
  const fullPath = path.join(dir, relativePath);
  fs.mkdirSync(path.dirname(fullPath), { recursive: true });
  fs.writeFileSync(fullPath, content, 'utf-8');
}

function createValidAidlcWorkspace(tmpDir: string): void {
  // Write version file with known-good version
  writeFile(tmpDir, 'aidlc/.aidlc-version', 'v2.2.3');
  // Write a pending gate state
  writeFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Requirements Analysis
- **Waiting For**: Human input on requirements gate
- **Next Action**: Wait for gate reply
`);
}

const SAMPLE_TRIGGER_COMMENT: TriggerComment = {
  author: 'jane-dev',
  createdAt: '2026-07-08T10:00:00Z',
  url: 'https://github.com/acme/repo/issues/42',
};

// ---------------------------------------------------------------------------
// Tests: mintSyntheticPresence — no-op paths
// ---------------------------------------------------------------------------

describe('mintSyntheticPresence', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = createTempDir();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('returns no-op when aidlc/ directory does not exist', () => {
    const deps = createMockDeps(tmpDir);
    const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);

    expect(result.written).toBe(false);
    expect(result.reason).toBe('aidlc_not_detected');
  });

  it('returns no-op when .aidlc-version file is missing', () => {
    // Create aidlc/ dir but no version file
    writeFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', '# placeholder');
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);

    expect(result.written).toBe(false);
    expect(result.reason).toBe('no_version_file');
    expect(deps.log).toHaveBeenCalledWith(
      'WARN',
      expect.stringContaining('No .aidlc-version file found'),
    );
  });

  it('returns no-op when version is unknown (skip with loud log)', () => {
    writeFile(tmpDir, 'aidlc/.aidlc-version', 'v99.0.0');
    writeFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# State
- **Stage**: Design
- **Waiting For**: Human input on design
`);
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);

    expect(result.written).toBe(false);
    expect(result.reason).toBe('unknown_version');
    expect(deps.log).toHaveBeenCalledWith(
      'WARN',
      expect.stringContaining('Unknown AIDLC version "v99.0.0"'),
    );
    expect(deps.log).toHaveBeenCalledWith(
      'WARN',
      expect.stringContaining('KNOWN_AIDLC_VERSIONS'),
    );
  });

  it('returns no-op when no pending gate stage exists', () => {
    writeFile(tmpDir, 'aidlc/.aidlc-version', 'v2.2.3');
    writeFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Requirements Analysis
- **Waiting For**: Nothing
- **Next Action**: Continue
`);
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);

    expect(result.written).toBe(false);
    expect(result.reason).toBe('no_pending_gate');
  });

  it('returns no-op when trigger comment is null', () => {
    createValidAidlcWorkspace(tmpDir);
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, null);

    expect(result.written).toBe(false);
    expect(result.reason).toBe('no_trigger_comment');
    expect(result.stage).toBe('requirements-analysis');
  });

  it('returns no-op when trigger comment has empty author', () => {
    createValidAidlcWorkspace(tmpDir);
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, { author: '', createdAt: '2026-07-08T10:00:00Z', url: '' });

    expect(result.written).toBe(false);
    expect(result.reason).toBe('no_trigger_comment');
  });
});

// ---------------------------------------------------------------------------
// Tests: mintSyntheticPresence — success paths
// ---------------------------------------------------------------------------

describe('mintSyntheticPresence — success', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = createTempDir();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('writes HUMAN_TURN event to audit shard on valid gate resume', () => {
    createValidAidlcWorkspace(tmpDir);
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);

    expect(result.written).toBe(true);
    expect(result.reason).toBe('success');
    expect(result.stage).toBe('requirements-analysis');

    // Verify audit file was created
    const auditPath = path.join(tmpDir, 'aidlc', 'spaces', 'default', 'audit.md');
    expect(fs.existsSync(auditPath)).toBe(true);

    const content = fs.readFileSync(auditPath, 'utf-8');
    expect(content).toContain('### HUMAN_TURN — Gate Approval');
    expect(content).toContain('@jane-dev');
    expect(content).toContain('HUMAN_TURN');
    expect(content).toContain('synthetic (ADP gate resume)');
    expect(content).toContain('requirements-analysis');
    expect(content).toContain(SAMPLE_TRIGGER_COMMENT.url);
    expect(content).toContain(SAMPLE_TRIGGER_COMMENT.createdAt);
  });

  it('appends to existing audit shard with separator', () => {
    createValidAidlcWorkspace(tmpDir);
    // Pre-existing audit file
    writeFile(tmpDir, 'aidlc/spaces/default/audit.md', '# AIDLC Audit Log\n\n### Previous Entry\n**Existing content here**\n');
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);

    expect(result.written).toBe(true);

    const content = fs.readFileSync(path.join(tmpDir, 'aidlc', 'spaces', 'default', 'audit.md'), 'utf-8');
    // Should contain both original and new content
    expect(content).toContain('### Previous Entry');
    expect(content).toContain('---');
    expect(content).toContain('### HUMAN_TURN — Gate Approval');
  });

  it('propagates author from trigger comment into event', () => {
    createValidAidlcWorkspace(tmpDir);
    const deps = createMockDeps(tmpDir);
    const comment: TriggerComment = {
      author: 'cto-approver',
      createdAt: '2026-07-08T15:30:00Z',
      url: 'https://github.com/org/repo/issues/99#issuecomment-123',
    };

    mintSyntheticPresence(deps, comment);

    const auditPath = path.join(tmpDir, 'aidlc', 'spaces', 'default', 'audit.md');
    const content = fs.readFileSync(auditPath, 'utf-8');
    expect(content).toContain('@cto-approver');
    expect(content).toContain('2026-07-08T15:30:00Z');
    expect(content).toContain('issuecomment-123');
  });

  it('works with all known-good versions', () => {
    for (const version of KNOWN_AIDLC_VERSIONS) {
      const dir = createTempDir();
      try {
        writeFile(dir, 'aidlc/.aidlc-version', version);
        writeFile(dir, 'aidlc/spaces/default/aidlc-state.md', `# State
- **Stage**: Design
- **Waiting For**: Human input on design
`);
        const deps = createMockDeps(dir);
        const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);
        expect(result.written).toBe(true);
      } finally {
        fs.rmSync(dir, { recursive: true, force: true });
      }
    }
  });

  it('uses legacy aidlc-docs path when spaces layout is absent', () => {
    writeFile(tmpDir, 'aidlc/.aidlc-version', 'v2.2.3');
    writeFile(tmpDir, 'aidlc-docs/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Delivery Planning
- **Waiting For**: Human input on delivery
- **Next Action**: Wait
`);
    const deps = createMockDeps(tmpDir);

    const result = mintSyntheticPresence(deps, SAMPLE_TRIGGER_COMMENT);

    expect(result.written).toBe(true);
    expect(result.stage).toBe('delivery-planning');

    // Should write to aidlc-docs/audit.md
    const auditPath = path.join(tmpDir, 'aidlc-docs', 'audit.md');
    expect(fs.existsSync(auditPath)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Tests: checkVersion
// ---------------------------------------------------------------------------

describe('checkVersion', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = createTempDir();
    fs.mkdirSync(path.join(tmpDir, 'aidlc'), { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('returns ok for known version', () => {
    fs.writeFileSync(path.join(tmpDir, 'aidlc', '.aidlc-version'), 'v2.2.3');
    const log = jest.fn();

    const result = checkVersion(path.join(tmpDir, 'aidlc'), log);

    expect(result.ok).toBe(true);
    expect(result.version).toBe('v2.2.3');
  });

  it('returns not-ok for unknown version', () => {
    fs.writeFileSync(path.join(tmpDir, 'aidlc', '.aidlc-version'), 'v1.0.0');
    const log = jest.fn();

    const result = checkVersion(path.join(tmpDir, 'aidlc'), log);

    expect(result.ok).toBe(false);
    expect(result.reason).toBe('unknown_version');
    expect(result.version).toBe('v1.0.0');
  });

  it('returns not-ok when version file is missing', () => {
    const log = jest.fn();

    const result = checkVersion(path.join(tmpDir, 'aidlc'), log);

    expect(result.ok).toBe(false);
    expect(result.reason).toBe('no_version_file');
  });

  it('returns not-ok when version file is empty', () => {
    fs.writeFileSync(path.join(tmpDir, 'aidlc', '.aidlc-version'), '  \n');
    const log = jest.fn();

    const result = checkVersion(path.join(tmpDir, 'aidlc'), log);

    expect(result.ok).toBe(false);
    expect(result.reason).toBe('empty_version');
  });

  it('trims whitespace from version string', () => {
    fs.writeFileSync(path.join(tmpDir, 'aidlc', '.aidlc-version'), '  v2.3.0  \n');
    const log = jest.fn();

    const result = checkVersion(path.join(tmpDir, 'aidlc'), log);

    expect(result.ok).toBe(true);
    expect(result.version).toBe('v2.3.0');
  });
});

// ---------------------------------------------------------------------------
// Tests: findPendingGateStage
// ---------------------------------------------------------------------------

describe('findPendingGateStage', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = createTempDir();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('returns null when no state files exist', () => {
    expect(findPendingGateStage(tmpDir)).toBeNull();
  });

  it('returns null when not waiting for human input', () => {
    writeFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# State
- **Stage**: Design
- **Waiting For**: Agent completion
`);
    expect(findPendingGateStage(tmpDir)).toBeNull();
  });

  it('detects pending gate from spaces layout', () => {
    writeFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# State
- **Stage**: Intent Capture
- **Waiting For**: Human input on intent
`);
    expect(findPendingGateStage(tmpDir)).toBe('intent-capture');
  });

  it('detects pending gate from legacy layout', () => {
    writeFile(tmpDir, 'aidlc-docs/aidlc-state.md', `# State
- **Stage**: Delivery Planning
- **Waiting For**: Human input on delivery
`);
    expect(findPendingGateStage(tmpDir)).toBe('delivery-planning');
  });
});

// ---------------------------------------------------------------------------
// Tests: buildHumanTurnEvent — fixture match
// ---------------------------------------------------------------------------

describe('buildHumanTurnEvent', () => {
  it('produces content matching the expected fixture structure', () => {
    const comment: TriggerComment = {
      author: 'test-user',
      createdAt: '2026-07-08T12:00:00Z',
      url: 'https://github.com/org/repo/issues/1',
    };

    const event = buildHumanTurnEvent('requirements-analysis', comment);

    // Verify structural elements match fixture template
    expect(event).toContain('### HUMAN_TURN — Gate Approval');
    expect(event).toMatch(/\*\*Timestamp\*\*: \d{4}-\d{2}-\d{2}T/);
    expect(event).toContain('**Author**: @test-user');
    expect(event).toContain('**Type**: HUMAN_TURN');
    expect(event).toContain('**Source**: synthetic (ADP gate resume)');
    expect(event).toContain('- Comment URL: https://github.com/org/repo/issues/1');
    expect(event).toContain('- Comment created: 2026-07-08T12:00:00Z');
    expect(event).toContain('- Gate stage: requirements-analysis');
    expect(event).toContain('**Action**: Gate decision recorded — human presence verified for headless AIDLC run');
  });

  it('handles special characters in author name', () => {
    const comment: TriggerComment = {
      author: 'user-with_special.chars',
      createdAt: '2026-07-08T12:00:00Z',
      url: 'https://github.com/org/repo/issues/1',
    };

    const event = buildHumanTurnEvent('design', comment);
    expect(event).toContain('@user-with_special.chars');
  });
});


describe('SCN-07 verified hosted gate presence', () => {
  const moment = Date.parse('2026-07-08T11:00:00Z');
  const createdAt = (millis: number) => new Date(millis).toISOString();
  const marker = { id: '50', author: 'agent[bot]', authorId: 9, authorType: 'Bot',
    body: '<!-- aidlc-gate:requirements-analysis -->\n## Review needed',
    createdAt: createdAt(moment - 1_000), updatedAt: createdAt(moment - 1_000) };
  const answer = { id: '51', author: 'jane-dev', authorId: 42, authorType: 'User',
    body: '@agent-aidlc approve', createdAt: createdAt(moment), updatedAt: createdAt(moment) };
  const context = { owner: 'org', repo: 'repo', runId: 'current-run',
    tokenProvider: async () => 'fixture-token', now: () => moment + 1000 };
  const originalFetch = global.fetch;
  afterEach(() => { global.fetch = originalFetch; });
  const find = (comments = [marker, answer], id: string | undefined = '51', senderId = 42,
    stage = 'requirements-analysis') => resolveVerifiedGateAnswer(
      comments, stage, 'org', 'repo', 42, id, senderId, context);

  it('writes a human turn only for the exact current trigger by a repository writer', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'maintain', user: { id: 42 } }) });
    const trigger = await find();
    expect(trigger).toEqual({ author: 'jane-dev', createdAt: answer.createdAt,
      url: 'https://github.com/org/repo/issues/42#issuecomment-51' });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const cwd = createTempDir();
    try {
      createValidAidlcWorkspace(cwd);
      expect(mintSyntheticPresence(createMockDeps(cwd), trigger).written).toBe(true);
      expect(fs.readFileSync(path.join(cwd, 'aidlc/spaces/default/audit.md'), 'utf8')).toContain('**Type**: HUMAN_TURN');
    } finally {
      fs.rmSync(cwd, { recursive: true, force: true });
    }
  });

  it('rejects missing, stale, edited, mismatched and non-command triggers before checking permission', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    expect(await find([answer])).toBeNull();
    expect(await find(undefined, '')).toBeNull();
    expect(await find(undefined, '51', 99)).toBeNull();
    expect(await find(undefined, '51', 42, 'design')).toBeNull();
    expect(await find([marker, { ...answer, body: '@agent-aidlc approval' }])).toBeNull();
    expect(await find([marker, { ...answer, body: 'Thanks @agent-aidlc approve' }])).toBeNull();
    expect(await find([marker, { ...answer, updatedAt: createdAt(moment + 2_000) }])).toBeNull();
    expect(await find([marker, { ...answer, authorType: 'Bot' }])).toBeNull();
    expect(await find([{ ...marker, authorType: 'User' }, answer])).toBeNull();
    expect(await find([{ ...marker, createdAt: createdAt(moment + 1000) }, answer])).toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('denies read-only authors and unavailable permission without publishing a presence event', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'read' }) });
    expect(await find()).toBeNull();
    global.fetch = jest.fn().mockRejectedValue(new Error('provider unavailable'));
    expect(await find()).toBeNull();
  });

  it('routes the installed worker through verified trigger selection, never a history fallback', () => {
    const source = fs.readFileSync(path.join(__dirname, 'agent-worker.ts'), 'utf8');
    expect(source).toContain('resolveVerifiedGateAnswer(comments, pendingStage');
    expect(source).toContain('if (pendingStage && process.env.GITHUB_EVENT_PATH)');
    expect(source).not.toContain('extractGateAnswerComment(');
  });

  it('accepts a same-second comment only when its ID follows the marker', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    const same = { ...marker, createdAt: answer.createdAt, updatedAt: answer.createdAt };
    expect(await find([same, answer])).not.toBeNull();
    expect(await find([{ ...same, id: '52' }, answer])).toBeNull();
  });
});
