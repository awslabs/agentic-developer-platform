import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as ts from 'typescript';
import { loadHumanCommunication } from './human-communication';
import { developerCheckpointGuidance } from './developer-checkpoints';
import { wrapUntrusted } from './utils/trust-boundary';

const worker = ts.createSourceFile('agent-worker.ts',
  fs.readFileSync(path.join(__dirname, 'agent-worker.ts'), 'utf8'),
  ts.ScriptTarget.Latest, true);
const personas = path.resolve(__dirname, '../../rules/personas');

function declaration(name: string): ts.FunctionDeclaration {
  const found = worker.statements.find((node): node is ts.FunctionDeclaration =>
    ts.isFunctionDeclaration(node) && node.name?.text === name);
  if (!found) throw new Error(`Missing production function ${name}`);
  return found;
}

function evaluate(source: string, bindings: Record<string, unknown>): string {
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return new Function(...Object.keys(bindings), compiled)(...Object.values(bindings));
}

// Run production assembly without importing the worker's side-effecting main().
// The fixture contains only persona assets actually shipped by the hosted image,
// not a manually supplied core workflow that is absent from normal staging.
describe('hosted operations delivery instructions', () => {
  let cwd: string;
  beforeEach(() => {
    cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-operations-prompt-'));
    fs.cpSync(personas, path.join(cwd, '.adp-rules/personas'), { recursive: true });
  });
  afterEach(() => fs.rmSync(cwd, { recursive: true, force: true }));

  function assemble(agentType: string): string {
    const rules = evaluate(`${declaration('loadRules').getText(worker)}; return loadRules();`, {
      fs, path, CWD: cwd, AGENT_TYPE: agentType, loadHumanCommunication,
    });
    const statements = declaration('runAgent').body!.statements;
    const promptStatement = statements.find(node => ts.isVariableStatement(node) &&
      node.declarationList.declarations.some(d => d.name.getText(worker) === 'prompt'));
    if (!promptStatement) throw new Error('Missing production prompt');
    return evaluate(`${promptStatement.getText(worker)}; return prompt;`, {
      AGENT_TYPE: agentType, agentDescriptions: { operations: 'Delivery coordinator' },
      rules, KNOWLEDGE_LAYER_ENABLED: false, KNOWLEDGE_LAYER_PROMPT: '',
      issue: { number: 42, title: 'Drive a delivery wave', body: 'Review and accept both stories.' },
      ISSUE_NUMBER: '42', mainIssueInfo: '', memoryCtx: '', commentsContext: '', beadsPrimeContext: '',
      wrapUntrusted, developerCheckpointGuidance,
    });
  }

  it('ships the delivery lifecycle and ownership contract without optional workflow files', () => {
    expect(fs.existsSync(path.join(cwd, '.adp-rules/core-workflow.md'))).toBe(false);
    const prompt = assemble('operations');
    expect(prompt).toContain('## Delivery orchestration');
    expect(prompt).toContain('Waiting retains ownership.');
    expect(prompt).toContain('Before ending, reconcile every remaining obligation.');
    expect(prompt).toContain('reviewed and deployed revisions');
    expect(prompt).toContain('re-read current issue decisions');
    expect(prompt).toContain('A child run\n  only owns its assigned work');
    expect(prompt).toContain('## TRUST BOUNDARY — MANDATORY');
    expect(prompt).toContain('not grant merge, deployment,\ncredential or dispatch authority');
    expect(prompt).toContain('budget limits or a human refusal');
  });

  it('keeps reporting brevity separate from execution completion in the final instructions', () => {
    const prompt = assemble('operations');
    const closing = prompt.slice(prompt.indexOf('## Completion Summary Format'));
    expect(closing).toContain('Ending an explanation does not end execution');
    expect(closing).toContain('A written\nnext action is not an accepted handoff');
    expect(closing).toContain('A standalone assessment ends');
    expect(prompt).not.toContain('Stop when the conclusion');
    expect(prompt).not.toContain('either deployed OR clearly stated');
    expect(prompt).not.toContain('drop the prior activity');
  });

  it('preserves user-scoped credentials without teaching the historical pod-role fallback', () => {
    const prompt = assemble('operations');
    expect(prompt).toContain('check\n  both account and assumed role');
    expect(prompt).toContain('do not\nreplace the selected target role');
    expect(prompt).toContain('Missing or expired shell credentials');
    expect(prompt).not.toContain('adp-cred` does NOT work');
    expect(prompt).not.toContain('export AWS_ROLE_ARN=');
  });

  it('does not assign the operations lifecycle to other personas', () => {
    const prompt = assemble('developer');
    expect(prompt).not.toContain('## Delivery orchestration');
    expect(prompt).not.toContain('### Step 3.5: Execution');
    expect(prompt).toContain('### Developer branch checkpoints');
    expect(prompt).toContain('A standalone assessment ends');
  });
});
