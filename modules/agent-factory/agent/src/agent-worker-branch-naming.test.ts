import { describe, it, expect } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';

const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

describe('agent-worker branch naming contract', () => {
  it('contains the "Branch naming (MANDATORY)" heading', () => {
    expect(source).toContain('## Branch naming (MANDATORY)');
  });

  it('contains the literal agent/issue-${ISSUE_NUMBER} template string', () => {
    expect(source).toContain('agent/issue-${ISSUE_NUMBER}');
  });

  it('states that branch naming is a contract, not style', () => {
    expect(source).toContain('This is not a style preference');
    expect(source).toContain("it's a contract.");
  });

  it('appears in the shared scaffolding, not inside per-agent-type conditionals', () => {
    const branchBlockIndex = source.indexOf('## Branch naming (MANDATORY)');
    expect(branchBlockIndex).toBeGreaterThan(-1);

    // Find the first per-agent-type conditional that comes AFTER the shared instructions start
    // The block must appear BEFORE the first AGENT_TYPE conditional branch
    const instructionsStart = source.indexOf('## Instructions');
    expect(instructionsStart).toBeGreaterThan(-1);

    // Get the substring from instructions start to the branch naming block
    const beforeBlock = source.substring(instructionsStart, branchBlockIndex);

    // The branch naming block should NOT be inside any AGENT_TYPE conditional
    // Verify: no opening of an AGENT_TYPE ternary between the last shared step and the block
    const step3Execute = source.indexOf('### Step 3: Execute Your Plan');
    expect(step3Execute).toBeGreaterThan(-1);
    expect(branchBlockIndex).toBeGreaterThan(step3Execute);

    // The first AGENT_TYPE conditional after the instructions section
    const firstConditional = source.indexOf("AGENT_TYPE === '", instructionsStart);
    expect(firstConditional).toBeGreaterThan(-1);

    // Branch naming must appear BEFORE the first per-agent conditional
    expect(branchBlockIndex).toBeLessThan(firstConditional);
  });
});
