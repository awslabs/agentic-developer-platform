/**
 * Tests for the agent log group resolver (issue #4221).
 *
 * The defect these guard against is silent: a log group name that disagrees with
 * the IAM grant / Terraform resource produces no error, just missing logs. So the
 * drift-guard test below matters as much as the behavioural ones.
 */

import * as fs from 'fs';
import * as path from 'path';
import { resolveAgentLogGroup, resolveEnvironment } from './logGroup';

describe('resolveEnvironment', () => {
  it('prefers ENVIRONMENT, which the KEDA pod template sets explicitly', () => {
    expect(resolveEnvironment({ ENVIRONMENT: 'prod', ENV: 'dev' })).toBe('prod');
  });

  it('falls back to ENV when ENVIRONMENT is unset', () => {
    expect(resolveEnvironment({ ENV: 'staging' })).toBe('staging');
  });

  it('defaults to dev when neither is set, keeping the ARC workflow path working', () => {
    expect(resolveEnvironment({})).toBe('dev');
  });

  it('treats an empty ENVIRONMENT as unset rather than resolving /adp//...', () => {
    expect(resolveEnvironment({ ENVIRONMENT: '', ENV: 'staging' })).toBe('staging');
    expect(resolveEnvironment({ ENVIRONMENT: '', ENV: '' })).toBe('dev');
  });
});

describe('resolveAgentLogGroup', () => {
  it('resolves the deployed dev default', () => {
    // Must match aws_cloudwatch_log_group.agent_logs in
    // webhook-ingress/infra/cloudwatch.tf for environment = "dev".
    expect(resolveAgentLogGroup({ ENVIRONMENT: 'dev' })).toBe('/adp/dev/agent-factory/agent');
  });

  it('scopes the group per environment so staging/prod do not write into dev (#4028)', () => {
    expect(resolveAgentLogGroup({ ENVIRONMENT: 'staging' })).toBe('/adp/staging/agent-factory/agent');
    expect(resolveAgentLogGroup({ ENVIRONMENT: 'prod' })).toBe('/adp/prod/agent-factory/agent');
  });

  it('is distinct from the bootstrap group, which has its own IAM statement', () => {
    expect(resolveAgentLogGroup({ ENVIRONMENT: 'dev' })).not.toBe('/adp/dev/agent-factory/bootstrap');
  });
});

describe('no stale log group literal remains', () => {
  // The old name was duplicated across five entrypoints. Any reintroduction
  // silently breaks logging again, because the IAM grant no longer covers it.
  //
  // Matches the name only when quoted — i.e. used as a string value in code.
  // Prose mentions in doc comments (which use backticks) are intentionally
  // allowed, so the historical context explaining #4221 can stay documented.
  const QUOTED_OLD_GROUP = /['"]\/github-ccsdk-agent\/logs/;

  it('finds no hardcoded /github-ccsdk-agent/logs string anywhere under src/', () => {
    const srcRoot = path.resolve(__dirname, '..');
    const offenders: string[] = [];

    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === 'node_modules') continue;
          walk(full);
        } else if (/\.ts$/.test(entry.name) && full !== __filename) {
          if (QUOTED_OLD_GROUP.test(fs.readFileSync(full, 'utf8'))) {
            offenders.push(path.relative(srcRoot, full));
          }
        }
      }
    };
    walk(srcRoot);

    expect(offenders).toEqual([]);
  });
});
