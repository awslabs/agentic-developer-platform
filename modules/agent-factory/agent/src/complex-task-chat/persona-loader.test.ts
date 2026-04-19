import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { composeSystemPrompt, loadPersona } from './persona-loader';
import { NullMemoryProvider } from './memory/null-memory';

describe('loadPersona defense-in-depth validation', () => {
  let tmpDir: string;
  const originalEnv = process.env.PERSONAS_DIR;

  beforeAll(() => {
    // Set up a temp personas dir with just `developer.md` allowlisted.
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'personas-'));
    fs.writeFileSync(path.join(tmpDir, 'developer.md'), 'You are a developer persona.');
    fs.writeFileSync(path.join(tmpDir, 'operations.md'), 'You are an ops persona.');
    // NOTE: by this point the module already captured its allowlist at require-time;
    // the assertions below rely on the test module being loaded AFTER this setup.
    process.env.PERSONAS_DIR = tmpDir;
  });

  afterAll(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    if (originalEnv === undefined) delete process.env.PERSONAS_DIR;
    else process.env.PERSONAS_DIR = originalEnv;
  });

  const memory = new NullMemoryProvider();

  it('rejects path-traversal attempt and falls back to default', async () => {
    const result = await loadPersona('../../../etc/passwd', {
      memory,
      query: 'hi',
      tokenBudget: 100,
    });
    // Falls back to default prompt — never tries to read the traversal path.
    expect(result.baseSystemPrompt).toMatch(/helpful assistant/i);
    expect(result.name).toBe('unknown');
  });

  it('rejects non-string input', async () => {
    const result = await loadPersona(null as unknown as string, {
      memory,
      query: 'hi',
      tokenBudget: 100,
    });
    expect(result.baseSystemPrompt).toMatch(/helpful assistant/i);
    expect(result.name).toBe('unknown');
  });

  it('rejects names with disallowed chars', async () => {
    const result = await loadPersona('dev/admin', {
      memory,
      query: 'hi',
      tokenBudget: 100,
    });
    expect(result.baseSystemPrompt).toMatch(/helpful assistant/i);
    expect(result.name).toBe('unknown');
  });

  it('rejects names passing regex but not in allowlist', async () => {
    // Because the module's allowlist is captured at require-time BEFORE this
    // test suite ran its beforeAll, the allowlist is whatever existed at import
    // — which is either empty (unit test env) or the baked personas from an
    // installed image. Either way, a made-up name must fall through to
    // default.
    const result = await loadPersona('nonexistent', {
      memory,
      query: 'hi',
      tokenBudget: 100,
    });
    expect(result.baseSystemPrompt).toMatch(/helpful assistant/i);
    expect(result.name).toBe('nonexistent');
  });
});

describe('composeSystemPrompt', () => {
  it('renders base persona only', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [],
      memories: [],
    });
    expect(result).toContain('<persona>');
    expect(result).toContain('You are a developer.');
    expect(result).toContain('</persona>');
    expect(result).not.toContain('<persona-learnings>');
    expect(result).not.toContain('<memories>');
  });

  it('renders persona learnings block', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [
        {
          id: 'mem_1',
          content: 'EKS cluster name is adp-dev-eks-cluster',
          scope: { persona: 'developer' },
          kind: 'learning',
          createdAt: '2026-04-10T00:00:00Z',
        },
      ],
      memories: [],
    });
    expect(result).toContain('<persona-learnings>');
    expect(result).toContain('mem_1');
    expect(result).toContain('EKS cluster name is adp-dev-eks-cluster');
    expect(result).toContain('</persona-learnings>');
  });

  it('renders memories block', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [],
      memories: [
        {
          id: 'mem_2',
          content: 'Prefers real-time updates',
          scope: { user: 'pranav' },
          kind: 'preference',
          createdAt: '2026-04-14T00:00:00Z',
        },
      ],
    });
    expect(result).toContain('<memories>');
    expect(result).toContain('mem_2');
    expect(result).toContain('Prefers real-time updates');
    expect(result).toContain('</memories>');
  });

  it('renders all blocks together', () => {
    const result = composeSystemPrompt({
      base: 'You are a developer.',
      personaLearnings: [
        {
          id: 'mem_1',
          content: 'learning content',
          scope: { persona: 'dev' },
          kind: 'learning',
          createdAt: '2026-04-10T00:00:00Z',
        },
      ],
      memories: [
        {
          id: 'mem_2',
          content: 'memory content',
          scope: { user: 'test' },
          kind: 'fact',
          createdAt: '2026-04-10T00:00:00Z',
        },
      ],
    });
    expect(result).toContain('<persona>');
    expect(result).toContain('<persona-learnings>');
    expect(result).toContain('<memories>');
  });
});
