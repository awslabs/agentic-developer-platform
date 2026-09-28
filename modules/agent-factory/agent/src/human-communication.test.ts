import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as ts from 'typescript';
import { loadHumanCommunication } from './human-communication';
import { composeSystemPrompt } from './complex-task-chat/persona-loader';

const policy = fs.readFileSync(path.resolve(__dirname, '../../rules/personas/shared/human-communication.md'), 'utf8');

describe('shared communication policy assembly', () => {
  let cwd: string;
  beforeEach(() => { cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-policy-')); });
  afterEach(() => fs.rmSync(cwd, { recursive: true, force: true }));

  it.each(['agent-worker.ts', 'agent-pm.ts'])('%s retains repo persona overrides and appends the policy once', file => {
    const defaults = path.join(cwd, '.adp-rules/personas');
    const overrides = path.join(cwd, '.github-agent/personas');
    fs.mkdirSync(path.join(defaults, 'shared'), { recursive: true });
    fs.mkdirSync(overrides, { recursive: true });
    fs.writeFileSync(path.join(defaults, 'shared/human-communication.md'), policy);
    fs.writeFileSync(path.join(defaults, 'pm.md'), 'DEFAULT PERSONA');
    fs.writeFileSync(path.join(overrides, 'pm.md'), 'REPOSITORY PERSONA');
    // Execute the production loader in isolation; importing the entrypoint starts a worker.
    const source = fs.readFileSync(path.join(__dirname, file), 'utf8');
    const start = source.indexOf('function loadRules(): string {');
    const end = source.indexOf("  return rules.join('\\n\\n---\\n\\n');\n}", start);
    expect(end).toBeGreaterThan(start);
    const compiled = ts.transpileModule(source.slice(start, source.indexOf('\n}', end) + 2), {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const prompt = new Function('fs', 'path', 'CWD', 'AGENT_TYPE', 'loadHumanCommunication',
      'beadsPrimeContext', 'agentMemoryContext', `${compiled}; return loadRules();`)(
      fs, path, cwd, 'pm', loadHumanCommunication, '', '',
    );
    expect(prompt).toContain('REPOSITORY PERSONA');
    expect(prompt).not.toContain('DEFAULT PERSONA');
    expect(prompt.split(policy)).toHaveLength(2);
    expect(prompt.endsWith(policy)).toBe(true);
  });

  it('chat applies the policy once after memory and the selected persona', () => {
    const prompt = composeSystemPrompt({ base: 'Custom persona', personaLearnings: [], memories: [], priorExperience: 'Old reporting template' });
    expect(prompt.split(policy)).toHaveLength(2);
    expect(prompt.indexOf(policy)).toBeGreaterThan(prompt.indexOf('Old reporting template'));
  });
});

it('the PM quick path receives the policy even though it bypasses full workflow rules', () => {
  const source = fs.readFileSync(path.join(__dirname, 'agent-pm.ts'), 'utf8');
  const quick = source.slice(source.indexOf('async function executeQuickTask('), source.indexOf('async function routeToAgent('));
  expect(quick).toContain('loadHumanCommunication(');
  expect(quick).not.toContain('Quick Task Complete');
  expect(quick).toContain('checks run and checks still missing');
});
