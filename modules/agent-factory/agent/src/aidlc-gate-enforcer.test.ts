/**
 * Unit tests for AIDLC Gate Enforcer (Issue #3231, EPIC #3158).
 *
 * Covers:
 * - dirty state → commit called
 * - clean state → no-op
 * - pending gate without marker comment → fallback posted
 * - marker present → no duplicate
 * - non-AIDLC CWD → enforcer never invoked
 */

import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import {
  enforceAidlcGate,
  commitDirtyAidlcState,
  ensureGateComment,
  findPendingGateStage,
  normalizeStageId,
  EnforcerDeps,
} from './aidlc-gate-enforcer';

// Mock child_process.execSync
jest.mock('child_process', () => ({
  execSync: jest.fn(),
}));

import { execSync } from 'child_process';
const mockExecSync = execSync as jest.MockedFunction<typeof execSync>;

// ---------------------------------------------------------------------------
// Test Helpers
// ---------------------------------------------------------------------------

function createTempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'aidlc-gate-test-'));
}

function createMockDeps(overrides: Partial<EnforcerDeps> = {}): EnforcerDeps {
  return {
    cwd: '/tmp/test-workspace',
    issueNumber: '42',
    repoOwner: 'test-org',
    repoName: 'test-repo',
    log: jest.fn(),
    execCommand: jest.fn().mockResolvedValue(''),
    postComment: jest.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

function writeStateFile(dir: string, relativePath: string, content: string): void {
  const fullPath = path.join(dir, relativePath);
  fs.mkdirSync(path.dirname(fullPath), { recursive: true });
  fs.writeFileSync(fullPath, content, 'utf-8');
}

// ---------------------------------------------------------------------------
// Tests: normalizeStageId
// ---------------------------------------------------------------------------

describe('normalizeStageId', () => {
  it('converts title case to kebab-case', () => {
    expect(normalizeStageId('Requirements Analysis')).toBe('requirements-analysis');
  });

  it('converts mixed case with special chars', () => {
    expect(normalizeStageId('Intent Capture (v2)')).toBe('intent-capture-v2');
  });

  it('handles already-kebab input', () => {
    expect(normalizeStageId('delivery-planning')).toBe('delivery-planning');
  });

  it('trims leading/trailing hyphens', () => {
    expect(normalizeStageId(' -- hello -- ')).toBe('hello');
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

  it('returns null when no aidlc state files exist', () => {
    expect(findPendingGateStage(tmpDir)).toBeNull();
  });

  it('returns null when state file has no pending gate', () => {
    writeStateFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Requirements Analysis
- **Waiting For**: Nothing
- **Next Action**: Continue
`);
    expect(findPendingGateStage(tmpDir)).toBeNull();
  });

  it('detects pending gate from spaces layout', () => {
    writeStateFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Intent Capture
- **Waiting For**: Human input on approval
- **Next Action**: Wait for gate reply
`);
    expect(findPendingGateStage(tmpDir)).toBe('intent-capture');
  });

  it('detects pending gate from legacy aidlc-docs layout', () => {
    writeStateFile(tmpDir, 'aidlc-docs/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Delivery Planning
- **Waiting For**: Human input on review
- **Next Action**: Await approval
`);
    expect(findPendingGateStage(tmpDir)).toBe('delivery-planning');
  });

  it('picks up nested spaces correctly', () => {
    writeStateFile(tmpDir, 'aidlc/spaces/project-alpha/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Requirements Analysis
- **Waiting For**: Human input on requirements gate
- **Next Action**: Waiting
`);
    expect(findPendingGateStage(tmpDir)).toBe('requirements-analysis');
  });
});

// ---------------------------------------------------------------------------
// Tests: commitDirtyAidlcState
// ---------------------------------------------------------------------------

describe('commitDirtyAidlcState', () => {
  beforeEach(() => {
    mockExecSync.mockReset();
  });

  it('returns false when aidlc/ is clean (no changes)', async () => {
    mockExecSync.mockReturnValue('' as any);
    const deps = createMockDeps();

    const result = await commitDirtyAidlcState(deps);

    expect(result).toBe(false);
    expect(mockExecSync).toHaveBeenCalledTimes(1);
    expect(mockExecSync).toHaveBeenCalledWith(
      'git status --porcelain aidlc/',
      expect.objectContaining({ cwd: deps.cwd }),
    );
  });

  it('commits and pushes when aidlc/ has dirty state', async () => {
    mockExecSync
      .mockReturnValueOnce(' M aidlc/spaces/default/aidlc-state.md\n' as any) // status
      .mockReturnValueOnce('' as any) // git add
      .mockReturnValueOnce('' as any) // git commit
      .mockReturnValueOnce('' as any); // git push
    const deps = createMockDeps();

    const result = await commitDirtyAidlcState(deps);

    expect(result).toBe(true);
    expect(mockExecSync).toHaveBeenCalledTimes(4);
    // Verify git add aidlc/
    expect(mockExecSync).toHaveBeenCalledWith(
      'git add aidlc/',
      expect.objectContaining({ cwd: deps.cwd }),
    );
    // Verify commit message format
    const commitCall = mockExecSync.mock.calls[2];
    expect(commitCall[0]).toMatch(/^git commit -m "aidlc: checkpoint .+ \(enforced\)"$/);
    // Verify git push
    expect(mockExecSync).toHaveBeenCalledWith(
      'git push',
      expect.objectContaining({ cwd: deps.cwd }),
    );
  });

  it('returns false and logs warning when git push fails', async () => {
    mockExecSync
      .mockReturnValueOnce(' M aidlc/state.md\n' as any) // status
      .mockReturnValueOnce('' as any) // git add
      .mockReturnValueOnce('' as any) // git commit
      .mockImplementationOnce(() => { throw new Error('push failed: no upstream'); }); // git push fails
    const deps = createMockDeps();

    const result = await commitDirtyAidlcState(deps);

    expect(result).toBe(false);
    expect(deps.log).toHaveBeenCalledWith(
      'WARN',
      expect.stringContaining('Enforced commit/push failed'),
    );
  });
});

// ---------------------------------------------------------------------------
// Tests: ensureGateComment
// ---------------------------------------------------------------------------

describe('ensureGateComment', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = createTempDir();
    mockExecSync.mockReset();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('does nothing when no pending gate stage exists', async () => {
    const deps = createMockDeps({ cwd: tmpDir });

    const result = await ensureGateComment(deps);

    expect(result.posted).toBe(false);
    expect(result.stage).toBeNull();
    expect(deps.postComment).not.toHaveBeenCalled();
  });

  it('does NOT post duplicate when marker already exists', async () => {
    writeStateFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Intent Capture
- **Waiting For**: Human input on gate
- **Next Action**: Wait
`);
    const deps = createMockDeps({
      cwd: tmpDir,
      // Simulate existing marker in comments
      execCommand: jest.fn().mockResolvedValue(
        '<!-- aidlc-gate:intent-capture -->\n## Gate: intent-capture\nSome content here',
      ),
    });

    const result = await ensureGateComment(deps);

    expect(result.posted).toBe(false);
    expect(result.stage).toBe('intent-capture');
    expect(deps.postComment).not.toHaveBeenCalled();
  });

  it('posts fallback gate comment when marker is missing', async () => {
    writeStateFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Requirements Analysis
- **Waiting For**: Human input on requirements
- **Next Action**: Wait
`);
    const deps = createMockDeps({
      cwd: tmpDir,
      // No marker in comments
      execCommand: jest.fn().mockResolvedValue('some comment without markers'),
    });

    const result = await ensureGateComment(deps);

    expect(result.posted).toBe(true);
    expect(result.stage).toBe('requirements-analysis');
    expect(deps.postComment).toHaveBeenCalledTimes(1);

    // Verify fallback comment structure
    const body = (deps.postComment as jest.Mock).mock.calls[0][0] as string;
    expect(body).toContain('<!-- aidlc-gate:requirements-analysis -->');
    expect(body).toContain('approve');
    expect(body).toContain('feedback:');
    expect(body).toContain('skip');
    expect(body).toContain('gate enforcer');
  });

  it('handles execCommand failure gracefully', async () => {
    writeStateFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Design
- **Waiting For**: Human input on design gate
- **Next Action**: Wait
`);
    const deps = createMockDeps({
      cwd: tmpDir,
      // gh command fails → assumes marker absent, then posts
      execCommand: jest.fn().mockRejectedValue(new Error('gh: command not found')),
    });

    const result = await ensureGateComment(deps);

    // Should still attempt to post (marker assumed absent)
    expect(result.posted).toBe(true);
    expect(result.stage).toBe('design');
  });
});

// ---------------------------------------------------------------------------
// Tests: enforceAidlcGate (integration)
// ---------------------------------------------------------------------------

describe('enforceAidlcGate', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = createTempDir();
    mockExecSync.mockReset();
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('does nothing on clean state with no pending gate', async () => {
    mockExecSync.mockReturnValue('' as any); // clean status
    const deps = createMockDeps({ cwd: tmpDir });

    const result = await enforceAidlcGate(deps);

    expect(result.committed).toBe(false);
    expect(result.gateCommentPosted).toBe(false);
    expect(result.stage).toBeNull();
  });

  it('commits dirty state AND posts gate comment when both needed', async () => {
    writeStateFile(tmpDir, 'aidlc/spaces/default/aidlc-state.md', `# AIDLC State

## Current Status
- **Phase**: Inception
- **Stage**: Delivery Planning
- **Waiting For**: Human input on delivery plan
- **Next Action**: Wait
`);
    mockExecSync
      .mockReturnValueOnce(' M aidlc/spaces/default/aidlc-state.md\n' as any) // dirty status
      .mockReturnValueOnce('' as any) // git add
      .mockReturnValueOnce('' as any) // git commit
      .mockReturnValueOnce('' as any); // git push

    const deps = createMockDeps({
      cwd: tmpDir,
      execCommand: jest.fn().mockResolvedValue('no markers here'),
    });

    const result = await enforceAidlcGate(deps);

    expect(result.committed).toBe(true);
    expect(result.gateCommentPosted).toBe(true);
    expect(result.stage).toBe('delivery-planning');
  });
});

// ---------------------------------------------------------------------------
// Tests: Non-AIDLC guard (source inspection)
// ---------------------------------------------------------------------------

describe('agent-worker AIDLC gate enforcement guard', () => {
  const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
  const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

  it('calls enforceAidlcGate only when AIDLC_ENABLED is true', () => {
    expect(source).toContain('if (AIDLC_ENABLED)');
    expect(source).toContain('enforceAidlcGate');
  });

  it('imports the enforcer module', () => {
    expect(source).toContain("from './aidlc-gate-enforcer'");
  });
});
