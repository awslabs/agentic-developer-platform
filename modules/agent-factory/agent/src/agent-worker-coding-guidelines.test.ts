import * as fs from 'fs';
import * as path from 'path';
import { tmpdir } from 'os';
import { execFileSync } from 'child_process';
import ts from 'typescript';
import { loadCodingGuidelines } from './coding-guidelines';

const factory = path.resolve(__dirname, '../..');
const canonical = fs.readFileSync(path.join(factory, 'rules/coding-guidelines.md'), 'utf-8');
const workerSource = fs.readFileSync(path.join(__dirname, 'agent-worker.ts'), 'utf-8');

describe('shared coding guidelines delivery', () => {
  it('loads the full canonical policy in a source checkout', () => {
    expect(loadCodingGuidelines()).toBe(canonical);
    for (const title of ['Think Before Coding', 'Simplicity First', 'Surgical Changes', 'Goal-Driven Execution']) {
      expect(canonical).toContain(title);
    }
  });

  it('includes the policy once in the shared rules before the task, without the old inline summary', () => {
    expect(workerSource.match(/rules\.push\(loadCodingGuidelines\(\)\)/g)).toHaveLength(1);
    expect(workerSource).not.toContain('## Coding Guidelines (MANDATORY for all code changes)');
    const prompt = workerSource.slice(workerSource.indexOf('const prompt = `'));
    expect(prompt.indexOf('${rules}')).toBeGreaterThan(-1);
    expect(prompt.indexOf('${rules}')).toBeLessThan(prompt.indexOf('## Your Task'));
  });

  it('loads the installed package outside the checkout and refuses a missing policy despite repository overrides', () => {
    const root = fs.mkdtempSync(path.join(tmpdir(), 'adp-claude-guidelines-'));
    try {
      for (const dir of ['app/dist', 'app/rules', 'repo/.adp-rules', 'repo/docs']) {
        fs.mkdirSync(path.join(root, dir), { recursive: true });
      }
      const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, 'coding-guidelines.ts'), 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
      }).outputText;
      fs.writeFileSync(path.join(root, 'app/dist/coding-guidelines.js'), compiled);
      const installed = path.join(root, 'app/rules/coding-guidelines.md');
      fs.writeFileSync(installed, canonical);
      for (const file of ['.adp-rules/coding-guidelines.md', 'docs/agent-coding-guidelines.md']) {
        fs.writeFileSync(path.join(root, 'repo', file), 'REPOSITORY_OVERRIDE');
      }
      const probe = () => execFileSync(process.execPath, ['-e',
        `process.stdout.write(require(${JSON.stringify(path.join(root, 'app/dist/coding-guidelines.js'))}).loadCodingGuidelines())`],
        { cwd: path.join(root, 'repo'), encoding: 'utf8', stdio: 'pipe' });
      expect(probe()).toBe(canonical);
      fs.writeFileSync(installed, '');
      expect(probe).toThrow(/Installed coding guidelines are empty/);
      fs.unlinkSync(installed);
      expect(probe).toThrow(/Shared coding guidelines are missing/);
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
  });

  it('packages the canonical policy in both Claude worker images', () => {
    for (const [file, source, destination] of [
      ['agent-worker-image/Dockerfile', 'modules/agent-factory/rules/coding-guidelines.md', '/app/rules/coding-guidelines.md'],
      ['agent/Dockerfile', 'rules/coding-guidelines.md', './rules/coding-guidelines.md'],
    ]) {
      expect(fs.readFileSync(path.join(factory, file), 'utf8'))
        .toContain(`COPY --chown=agent:agent ${source} ${destination}`);
    }
  });
});
