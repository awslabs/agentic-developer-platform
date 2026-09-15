import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as ts from 'typescript';
import { loadHumanCommunication } from './human-communication';

const personas = path.resolve(__dirname, '../../rules/personas');
const persona = fs.readFileSync(path.join(personas, 'aidlc.md'), 'utf8');
const emitter = fs.readFileSync(path.resolve(__dirname, '../../skills/aidlc-emit-issues/SKILL.md'), 'utf8');

function section(text: string, start: string, end: string): string {
  const from = text.indexOf(start);
  const to = text.indexOf(end, from + start.length);
  if (from < 0 || to < 0) throw new Error(`Missing presentation section: ${start}`);
  return text.slice(from + start.length, to);
}

function tableRows(text: string): string[][] {
  return text.split('\n').map(line => line.trim()).filter(line => line.startsWith('|'))
    .map(line => line.slice(1, -1).split('|').map(cell => cell.trim()));
}

function tableHeaders(text: string): string[][] {
  const rows = tableRows(text);
  return rows.filter((_, i) => rows[i + 1]?.every(cell => /^:?-+:?$/.test(cell)));
}

// These are delivery/format contracts for the actual prompt assets, not a
// simulation of model compliance. The existing gate-enforcer tests exercise
// publication, supported actions and the human approval boundary separately.
describe('AI-DLC presentation delivered to hosted workers', () => {
  let cwd: string;
  beforeEach(() => {
    cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-aidlc-presentation-'));
    fs.cpSync(personas, path.join(cwd, '.adp-rules/personas'), { recursive: true });
  });
  afterEach(() => fs.rmSync(cwd, { recursive: true, force: true }));

  function loadRules(agentType: string): string {
    // Execute the production loader without starting the worker's main().
    const source = ts.createSourceFile('agent-worker.ts',
      fs.readFileSync(path.join(__dirname, 'agent-worker.ts'), 'utf8'), ts.ScriptTarget.Latest, true);
    const loader = source.statements.find((node): node is ts.FunctionDeclaration =>
      ts.isFunctionDeclaration(node) && node.name?.text === 'loadRules');
    if (!loader) throw new Error('Missing production rules loader');
    const compiled = ts.transpileModule(loader.getText(source), {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText;
    return new Function('fs', 'path', 'CWD', 'AGENT_TYPE', 'loadHumanCommunication',
      `${compiled}; return loadRules();`)(fs, path, cwd, agentType, loadHumanCommunication);
  }

  it('loads the complete AI-DLC layout from shipped assets without optional workflow files', () => {
    expect(fs.existsSync(path.join(cwd, '.adp-rules/core-workflow.md'))).toBe(false);
    const rules = loadRules('aidlc');
    expect(rules.split(persona)).toHaveLength(2);
    const policy = loadHumanCommunication([path.join(cwd, '.adp-rules/personas')]);
    expect(rules.endsWith(policy)).toBe(true);
    expect(policy).toContain('except required AI-DLC workflow layouts');
    expect(policy).not.toContain('These rules replace conflicting presentation templates');
    expect(loadRules('developer')).not.toContain(persona);
  });

  it('keeps a usable gate template with review tables, revision metadata and the reply footer last', () => {
    const rules = loadRules('aidlc');
    const gate = section(rules, '```markdown\n', '\n```');
    expect(gate.split('\n')[0]).toBe('<!-- aidlc-gate:<stage-name> -->');
    expect(tableHeaders(gate)).toEqual([
      ['Artifact', 'What it contains / what to review', 'Revision'],
      ['Decision', 'Recommendation and reason', 'Alternative / tradeoff', 'Human input needed'],
    ]);
    expect([...gate.matchAll(/\*\*(\w+)\*\*:/g)].map(match => match[1])).toEqual([
      'Status', 'Phase', 'Scope', 'Depth', 'Revision', 'Branch',
    ]);
    expect(gate.split('\n').filter(line => line.startsWith('### '))).toEqual([
      '### Decision needed', '### Artifacts to review', '### Decisions and tradeoffs',
      '### Changes since the last gate', '### Validation and remaining holds',
      '### What approval authorizes', '### Reply to continue',
    ]);
    const actions = gate.slice(gate.indexOf('### Reply to continue'));
    expect(actions).toContain('`@agent-aidlc approve`');
    expect(actions).toContain('`@agent-aidlc feedback: [your notes]`');
    expect(actions).not.toContain('`@agent-aidlc skip`');
  });

  it('keeps wave ownership, credential selection and every current emission rule visible at the final gate', () => {
    const loop = section(loadRules('aidlc'), '### After Delivery-Planning Gate Approval', '### After Loop-Proposal Gate Approval');
    expect(tableHeaders(loop)).toEqual([
      ['Wave / capability', 'Story issues', 'Orchestrator / evaluation drafts', 'Planned checks', 'Remaining holds'],
      ['Target environment', 'AWS account ID', 'Region', 'adp-cred label', 'Selection status'],
      ['Emission rule', 'Result', 'Evidence / remaining action'],
    ]);
    const currentRules = [...emitter.matchAll(/^\*\*Rule (\d+) — /gm)].map(match => match[1]);
    const displayedRules = tableRows(loop).map(row => row[0].match(/^(\d+) — /)?.[1]).filter(Boolean);
    expect(currentRules).toHaveLength(5);
    expect(displayedRules).toEqual(currentRules);
  });

  it('retains the tracker status columns and performs its update before the final gate post', () => {
    const rules = loadRules('aidlc');
    const tracker = section(rules, '#### Tracker content', '#### Data source');
    expect(tableHeaders(tracker)).toEqual([['Phase', 'Stage', 'Status', 'Artifact', 'Cost']]);
    expect(tracker).toContain('2/5 stages resolved · Gate 3/5');
    expect(tracker).toContain('an open\n   gate is not done');
    const sequence = section(rules, 'Sequence for a single run:\n```\n', '\n```');
    expect(sequence.indexOf('update Live Tracker')).toBeLessThan(sequence.indexOf('Post Gate Brief'));
    expect(sequence.trim().endsWith('5. EXIT — run is over')).toBe(true);
  });

  it('preserves the completion inventory and story mapping in the emitter template', () => {
    const completion = section(emitter, '### Step 9:', '## Error handling');
    expect(tableHeaders(completion)).toEqual([
      ['Wave and capability', 'Story issues', 'Orchestrator', 'Evaluation', 'Current state'],
      ['Check', 'Result', 'Evidence / remaining action'],
    ]);
    expect([...completion.matchAll(/\*\*([^*]+)\*\*:/g)].map(match => match[1])).toEqual([
      'EPIC', 'Story issues', 'Orchestrators', 'Evaluations', 'Artifact revision', 'Next', 'Evidence',
    ]);
  });
});
