import { describe, it, expect } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';

const WORKER_SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const GUIDELINES_PATH = path.resolve(__dirname, '../../../../docs/agent-coding-guidelines.md');

const workerSource = fs.readFileSync(WORKER_SOURCE_PATH, 'utf-8');

describe('agent-worker coding guidelines contract', () => {
  it('agent-worker.ts contains a reference to docs/agent-coding-guidelines.md', () => {
    expect(workerSource).toContain('docs/agent-coding-guidelines.md');
  });

  it('the reference is in the shared scaffolding, not inside per-agent-type conditionals', () => {
    const guidelinesIndex = workerSource.indexOf('## Coding Guidelines (MANDATORY for all code changes)');
    expect(guidelinesIndex).toBeGreaterThan(-1);

    // Must appear after the shared Step 3 section
    const step3Execute = workerSource.indexOf('### Step 3: Execute Your Plan');
    expect(step3Execute).toBeGreaterThan(-1);
    expect(guidelinesIndex).toBeGreaterThan(step3Execute);

    // Must appear BEFORE the first per-agent AGENT_TYPE conditional
    const instructionsStart = workerSource.indexOf('## Instructions');
    expect(instructionsStart).toBeGreaterThan(-1);
    const firstConditional = workerSource.indexOf("AGENT_TYPE === '", instructionsStart);
    expect(firstConditional).toBeGreaterThan(-1);
    expect(guidelinesIndex).toBeLessThan(firstConditional);
  });

  it('docs/agent-coding-guidelines.md exists and contains all 4 section headers', () => {
    expect(fs.existsSync(GUIDELINES_PATH)).toBe(true);
    const guidelines = fs.readFileSync(GUIDELINES_PATH, 'utf-8');
    expect(guidelines).toContain('## 1. Think Before Coding');
    expect(guidelines).toContain('## 2. Simplicity First');
    expect(guidelines).toContain('## 3. Surgical Changes');
    expect(guidelines).toContain('## 4. Goal-Driven Execution');
  });
});
