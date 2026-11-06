import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as ts from 'typescript';
import { withPmIssueAuthoring } from './pm-issue-authoring';

const rules = path.resolve(__dirname, '../../rules');
const sources = ['agents/issue-authoring.md', 'templates/epic-issue.md', 'templates/developer-issue.md'];

describe('PM authoring instructions at inference', () => {
  it('includes the full maintained guide and both templates, independently of task content', () => {
    const prompt = withPmIssueAuthoring('Review the existing story.');
    for (const source of sources) {
      expect(prompt.split(fs.readFileSync(path.join(rules, source), 'utf8'))).toHaveLength(2);
    }
    expect(prompt).toContain('do not create issues merely because templates are supplied');
    expect(prompt.endsWith('Review the existing story.')).toBe(true);
  });

  it('uses a complete installation and rejects missing/empty policy instead of omitting it', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pm-authoring-'));
    try {
      fs.mkdirSync(path.join(root, 'agents'));
      fs.writeFileSync(path.join(root, sources[0]), 'Partial old installation');
      expect(() => withPmIssueAuthoring('task', [root])).toThrow('missing from the installation');
      expect(withPmIssueAuthoring('task', [root, rules])).not.toContain('Partial old installation');
      fs.mkdirSync(path.join(root, 'templates'));
      for (const source of sources) fs.writeFileSync(path.join(root, source), ' ');
      expect(() => withPmIssueAuthoring('task', [root])).toThrow('source is empty');
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
  });

  it('delivers the contract to the actual shared execution call, including quick and resumed work', async () => {
    // Importing agent-pm starts a worker. Execute its production function in
    // isolation and capture SDK input, without GitHub, AWS or model calls.
    const source = fs.readFileSync(path.join(__dirname, 'agent-pm.ts'), 'utf8');
    const ast = ts.createSourceFile('agent-pm.ts', source, ts.ScriptTarget.Latest, true);
    const fn = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'runQuery');
    expect(fn).toBeDefined();
    const compiled = ts.transpileModule(fn!.getText(ast), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
    const captured: string[] = [];
    const query = async function* (args: { queryParams: { prompt: string } }) { captured.push(args.queryParams.prompt); };
    const run = new Function('withPmIssueAuthoring', 'resilientQuery', 'MODEL', 'CWD', 'log',
      `${compiled}; return runQuery;`)(withPmIssueAuthoring, query, 'test-model', '/tmp', () => {});
    for (const task of ['start', 'resume', 'reassess', 'quick']) await run(task, 1);
    expect(captured).toHaveLength(4);
    for (const prompt of captured) {
      expect(prompt).toContain('Requirements lineage contract: epics and stories');
      expect(prompt).toContain('| Scenario ID | Acceptance IDs | Case contract |');
      expect(prompt).toContain('| EC-01 |');
    }
  });

  it.each(['agent-pm.ts', 'monitoring.ts'])('%s includes the contract at every direct inference call', file => {
    const source = fs.readFileSync(path.join(__dirname, file), 'utf8');
    const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
    let calls = 0;
    function visit(node: ts.Node) {
      if (ts.isCallExpression(node) && node.expression.getText(ast) === 'resilientQuery') {
        calls++;
        const args = node.arguments[0] as ts.ObjectLiteralExpression;
        const params = args.properties.find(p => p.name?.getText(ast) === 'queryParams') as ts.PropertyAssignment;
        const prompt = (params.initializer as ts.ObjectLiteralExpression).properties.find(p => p.name?.getText(ast) === 'prompt') as ts.PropertyAssignment;
        expect(prompt.initializer.getText(ast)).toBe('withPmIssueAuthoring(prompt)');
      }
      ts.forEachChild(node, visit);
    }
    visit(ast);
    expect(calls).toBe(file === 'agent-pm.ts' ? 3 : 4);
  });
});
