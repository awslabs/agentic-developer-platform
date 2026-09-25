import { readFileSync } from 'fs';
import { resolve } from 'path';
const yaml = require('js-yaml');
const workflow = yaml.load(readFileSync(resolve(__dirname, '../../../../.github/workflows/agent-developer.yml'), 'utf8'));
const steps = workflow.jobs.work.steps;
const named = (name: string) => steps.find((step: any) => step.name === name);

it('keeps checkout credentials transient and requires renewal only on the worker', () => {
  const checkouts = steps.filter((step: any) => step.uses?.startsWith('actions/checkout@'));
  expect(checkouts).toHaveLength(2);
  for (const step of checkouts) expect(step.with['persist-credentials']).toBe(false);
  const agent = named('Run @agent-developer');
  expect(agent.env.ADP_REQUIRE_RENEWABLE_GITHUB).toBe('true');
  expect(agent.env.GH_APP_INSTALLATION_ID).toBe('${{ steps.app-token.outputs.installation-id }}');
  expect(agent.env.GH_APP_PRIVATE_KEY).toBe('${{ steps.aws-secrets.outputs.app_key }}');
  expect(workflow.jobs.work.env.GH_APP_PRIVATE_KEY).toBeUndefined();
  expect(steps.filter((step: any) => step.env?.GH_APP_PRIVATE_KEY)).toHaveLength(1);
});
it('uses a unique private directory outside both checkouts and always removes it', () => {
  const prepare = named('Prepare private GitHub runtime directory');
  expect(prepare.env.RUNNER_TEMP_DIR).toBe('${{ runner.temp }}');
  expect(prepare.run).toContain('mktemp -d "$RUNNER_TEMP_DIR/adp-github.XXXXXXXX"');
  expect(prepare.run).toContain('chmod 700');
  expect(prepare.run).toContain('ADP_TOKEN_FILE=$auth_dir/token');
  expect(named('Remove runtime GitHub credentials').if).toBe('always()');
});
it('mints a repository-restricted token after the agent and gates every final consumer', () => {
  const fresh = named('Generate fresh finalization token');
  expect(steps.indexOf(fresh)).toBeGreaterThan(steps.indexOf(named('Run @agent-developer')));
  expect(fresh.if).toContain('always()');
  expect(fresh.with.repositories).toBe('${{ env.REPO_NAME }}');
  for (const name of ['Commit and push changes', 'Notify completion', 'Handle failure']) {
    const step = named(name);
    expect(step.env.GH_TOKEN).toBe('${{ steps.final-token.outputs.token }}');
    expect(step.if).toContain("steps.final-token.outcome == 'success'");
    expect(JSON.stringify(step)).not.toContain('steps.app-token.outputs.token');
  }
  const commit = named('Commit and push changes');
  expect(commit.env.ADP_GITHUB_FINALIZATION).toBe('true');
  expect(commit.run).toContain('credential.helper=');
  expect(commit.run).toContain('credential.https://github.com.helper=');
  expect(commit.run).toContain('credential.useHttpPath=true');
  expect(commit.run).toContain('http.https://github.com/.extraheader=');
  expect(commit.run).toContain('push -u origin');
});
it('aborts the required worker on startup failures and wires the final SDK boundary', () => {
  const worker = readFileSync(resolve(__dirname, 'agent-worker.ts'), 'utf8');
  expect(worker).toContain('if (brokerMode || runtimeAppAuth.required) throw err;');
  expect(worker).toContain('await initializeRuntimeGitHubToken();');
  expect(worker).toContain('spawnClaudeCodeProcess: spawnSdkWithoutAppKey');
  expect(worker.indexOf('captureRuntimeAppAuth();')).toBeLessThan(worker.indexOf('async function main()'));
});
