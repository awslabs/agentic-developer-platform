/**
 * Contract tests for most-stable-first ordering of the agent-worker prompt
 * (issue #4183).
 *
 * The worker's user turn used to emit the issue title/body/comments BEFORE the
 * large `${rules}` block. That placed the highest-entropy content ahead of the
 * biggest invariant one, so nothing downstream of the issue body could ever be
 * a reusable prefix. These tests pin the corrected order.
 *
 * Two notes on method, both deliberate:
 *
 *  1. These assertions run against the SOURCE TEXT of `agent-worker.ts`, not a
 *     built prompt. That module calls `main()` at load and exports nothing, so
 *     importing it would execute the agent. Five sibling suites
 *     (`agent-worker-branch-naming`, `-coding-guidelines`, `-presubmit-checks`,
 *     `agent-reviewer-spec-review`, `aidlc-gate-enforcer`) use the same
 *     technique for the same reason.
 *
 *  2. #4183's Validation section asks for a byte-identical-prefix check across
 *     two different issue inputs. Sampling two inputs cannot be done without an
 *     importable builder, and would only ever prove the property for those two.
 *     Instead, `invariant prefix` below extracts EVERY `${...}` interpolation
 *     above the boundary and asserts each one is run-invariant. That implies
 *     byte-stability for all inputs, not two — a strictly stronger claim, and
 *     it is what actually fails if someone drops a run-specific value into the
 *     prefix later.
 *
 * Ordering is necessary but NOT sufficient for prompt-cache reuse: the provider
 * must also be told where the stable boundary is. That is #4180's job. Nothing
 * here asserts a cost saving, because none is measurable yet.
 */

import * as fs from 'fs';
import * as path from 'path';

const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

/**
 * The `runAgent` prompt template literal, from the opening backtick to the
 * closing `Now, complete the assigned task.`. Isolated so that identical
 * headings in other prompts (agent-pm, agent-superpower) or in this file's own
 * surrounding code cannot influence the index comparisons below.
 */
const PROMPT_TEMPLATE = (() => {
  const open = source.indexOf('const prompt = `');
  expect(open).toBeGreaterThan(-1);
  const start = open + 'const prompt = `'.length;
  const end = source.indexOf('\nNow, complete the assigned task.`;', start);
  expect(end).toBeGreaterThan(start);
  return source.slice(start, end);
})();

/** The heading that separates the invariant prefix from run-specific content. */
const BOUNDARY = '## Your Task';

/** Interpolated expressions that carry run-specific (per-issue) content. */
const RUN_SPECIFIC_INTERPOLATIONS = [
  'wrapUntrusted(issue.body)',
  'issue.number',
  'issue.title',
  'memoryCtx',
  'wrapUntrusted(commentsContext)',
  'mainIssueInfo',
  'beadsPrimeContext',
  'ISSUE_NUMBER',
];

/**
 * Expressions allowed to appear ABOVE the boundary. Each is invariant across
 * runs of the same persona in the same deployment:
 *   - AGENT_TYPE / agentDescriptions[...] — persona-level, fixed for the pod
 *   - rules                               — loadRules() output; reads only
 *                                           .adp-rules files, never the issue
 *   - KNOWLEDGE_LAYER_*                   — module constants from env
 *   - MEDIATED_GITHUB_*                   — module constants from env (#5223)
 */
const INVARIANT_INTERPOLATIONS = [
  'AGENT_TYPE',
  "agentDescriptions[AGENT_TYPE] || 'agent'",
  'rules',
  'KNOWLEDGE_LAYER_ENABLED',
  'KNOWLEDGE_LAYER_PROMPT',
  'MEDIATED_GITHUB_ENABLED',
  'MEDIATED_GITHUB_PROMPT',
];

/** Every `${...}` interpolation in `text`, innermost-first, de-duplicated. */
function interpolations(text: string): string[] {
  return [...new Set(Array.from(text.matchAll(/\$\{([^{}]*)\}/g), m => m[1].trim()))];
}

describe('agent-worker prompt ordering (#4183)', () => {
  describe('extraction sanity', () => {
    // If these fail, every assertion below is measuring the wrong string.
    it('isolates the runAgent prompt template', () => {
      expect(PROMPT_TEMPLATE.length).toBeGreaterThan(1000);
      expect(PROMPT_TEMPLATE).toContain('You are @agent-${AGENT_TYPE}');
    });

    it('the template contains exactly one boundary heading', () => {
      expect(PROMPT_TEMPLATE.split(BOUNDARY).length - 1).toBe(1);
    });
  });

  describe('most-stable-first order', () => {
    it('emits the rules block before any issue-derived content', () => {
      const rulesIdx = PROMPT_TEMPLATE.indexOf('## Rules and Guidelines');
      const bodyIdx = PROMPT_TEMPLATE.indexOf('${wrapUntrusted(issue.body)}');

      expect(rulesIdx).toBeGreaterThan(-1);
      expect(bodyIdx).toBeGreaterThan(-1);
      expect(rulesIdx).toBeLessThan(bodyIdx);
    });

    it('emits ${rules} itself before the issue title and body', () => {
      const rulesInterp = PROMPT_TEMPLATE.indexOf('${rules}');
      expect(rulesInterp).toBeGreaterThan(-1);

      for (const expr of ['${issue.title}', '${wrapUntrusted(issue.body)}', '${issue.number}']) {
        expect(PROMPT_TEMPLATE.indexOf(expr)).toBeGreaterThan(rulesInterp);
      }
    });

    it('places the static Available Skills and Knowledge Layer blocks in the prefix', () => {
      const boundaryIdx = PROMPT_TEMPLATE.indexOf(BOUNDARY);

      expect(PROMPT_TEMPLATE.indexOf('## Available Skills')).toBeLessThan(boundaryIdx);
      expect(PROMPT_TEMPLATE.indexOf('${KNOWLEDGE_LAYER_PROMPT}')).toBeLessThan(boundaryIdx);
    });

    it('keeps run-specific content below the boundary', () => {
      const boundaryIdx = PROMPT_TEMPLATE.indexOf(BOUNDARY);
      expect(boundaryIdx).toBeGreaterThan(-1);

      for (const expr of RUN_SPECIFIC_INTERPOLATIONS) {
        const idx = PROMPT_TEMPLATE.indexOf(`\${${expr}}`);
        if (idx === -1) continue; // some appear only inside nested templates
        expect(idx).toBeGreaterThan(boundaryIdx);
      }
    });

    it('keeps the issue block above ## Instructions', () => {
      // The reviewer prompt says "Re-read the issue body (already shown
      // above)". Moving the issue block below ## Instructions would make that
      // pointer false, which is why the fix hoists the invariant block up
      // rather than pushing the issue block down.
      const bodyIdx = PROMPT_TEMPLATE.indexOf('${wrapUntrusted(issue.body)}');
      const instructionsIdx = PROMPT_TEMPLATE.indexOf('## Instructions');

      expect(instructionsIdx).toBeGreaterThan(-1);
      expect(bodyIdx).toBeLessThan(instructionsIdx);
      expect(PROMPT_TEMPLATE).toContain('already shown above');
    });
  });

  describe('invariant prefix', () => {
    const prefix = PROMPT_TEMPLATE.slice(0, PROMPT_TEMPLATE.indexOf(BOUNDARY));

    it('interpolates only run-invariant expressions above the boundary', () => {
      const offenders = interpolations(prefix).filter(
        expr => !INVARIANT_INTERPOLATIONS.includes(expr),
      );

      expect(offenders).toEqual([]);
    });

    it('names no issue-derived expression above the boundary', () => {
      // Belt-and-braces against the regex above missing a nested form: the
      // prefix text must not mention these identifiers at all.
      for (const needle of ['issue.body', 'issue.title', 'issue.number', 'commentsContext', 'memoryCtx', 'mainIssueInfo', 'ISSUE_NUMBER']) {
        expect(prefix).not.toContain(needle);
      }
    });

    it('carries the bulk of the prompt in the prefix', () => {
      // ${rules} is the largest single segment and must be in the prefix for
      // the reorder to be worth anything.
      expect(prefix).toContain('${rules}');
      expect(interpolations(prefix)).toContain('rules');
    });

    it('documents the boundary for the cache-marker work', () => {
      // #4180 needs to find this spot without re-deriving the analysis.
      expect(source).toContain('#4183');
      expect(source).toContain('#4180');
      expect(source).toContain('stable/variable boundary');
    });
  });

  describe('completeness — no section dropped or duplicated', () => {
    const SECTIONS = [
      '## Rules and Guidelines',
      '## Available Skills',
      '## Your Task',
      '## Existing Discussion / Comments',
      '## Instructions',
      '### Step 1: Analyze and Plan',
      '### Step 2: Post Your Plan',
      '### Step 3: Execute Your Plan',
      '### Step 4: Report Results',
      '## Branch naming (MANDATORY)',
      '## Coding Guidelines (MANDATORY for all code changes)',
      '## Pre-submit checks (MANDATORY before requesting review)',
      '## Available Tools',
      '### Beads Task Management (bd)',
      '## Completion Summary Format',
    ];

    it.each(SECTIONS)('%s appears exactly once', section => {
      expect(PROMPT_TEMPLATE.split(section).length - 1).toBe(1);
    });

    it('retains every interpolation the prompt needs', () => {
      // A reorder that dropped an interpolation would silently starve the
      // agent of context while still producing a well-formed prompt.
      const all = interpolations(PROMPT_TEMPLATE);

      for (const expr of ['rules', 'wrapUntrusted(issue.body)', 'issue.title', 'issue.number', 'AGENT_TYPE', 'ISSUE_NUMBER']) {
        expect(all).toContain(expr);
      }
      expect(PROMPT_TEMPLATE).toContain('${mainIssueInfo}');
      expect(PROMPT_TEMPLATE).toContain('${wrapUntrusted(commentsContext)}');
      expect(PROMPT_TEMPLATE).toContain('${memoryCtx ?');
      expect(PROMPT_TEMPLATE).toContain('${beadsPrimeContext ?');
    });

    it('still wraps both untrusted inputs (trust boundary survives the move)', () => {
      expect(PROMPT_TEMPLATE).toContain('${wrapUntrusted(issue.body)}');
      expect(PROMPT_TEMPLATE).toContain('${wrapUntrusted(commentsContext)}');
      expect(PROMPT_TEMPLATE).not.toMatch(/\$\{issue\.body\}/);
      expect(PROMPT_TEMPLATE).not.toMatch(/\$\{commentsContext\}/);
    });

    it('preserves the per-persona conditional blocks', () => {
      expect(PROMPT_TEMPLATE).toContain("AGENT_TYPE === 'reviewer'");
      expect(PROMPT_TEMPLATE).toContain("AGENT_TYPE === 'operations'");
    });
  });

  describe('rules block internal order unchanged', () => {
    /** The body of `loadRules()`, which fixes the order of the rules segments. */
    const loadRulesBody = (() => {
      const start = source.indexOf('function loadRules(): string {');
      expect(start).toBeGreaterThan(-1);
      const end = source.indexOf("return rules.join('\\n\\n---\\n\\n');", start);
      expect(end).toBeGreaterThan(start);
      return source.slice(start, end);
    })();

    it('pushes rule segments in the documented order', () => {
      // Instruction priority inside the block must not shift: the persona is
      // loaded first so the agent has an identity before any task rules.
      const ordered = [
        '## Your Persona',
        '## Core Workflow',
        '## Agent Routing',
        '## Research Guide',
        '## Task Management (Beads)',
        '## Agent Memory',
      ];

      const indices = ordered.map(h => loadRulesBody.indexOf(h));
      for (const [i, idx] of indices.entries()) {
        expect(idx).toBeGreaterThan(-1);
        if (i > 0) expect(idx).toBeGreaterThan(indices[i - 1]);
      }
    });

    it('loads the persona before the phase-specific rules', () => {
      expect(loadRulesBody.indexOf('## Your Persona')).toBeLessThan(
        loadRulesBody.indexOf('const phasePaths = phaseMap[AGENT_TYPE]'),
      );
    });

    it('joins segments with the same separator as before', () => {
      expect(source).toContain("return rules.join('\\n\\n---\\n\\n');");
    });

    it('derives nothing in the rules block from the issue', () => {
      // This is what makes ${rules} safe to put in the cacheable prefix.
      for (const needle of ['issue.body', 'issue.title', 'issue.number', 'commentsContext']) {
        expect(loadRulesBody).not.toContain(needle);
      }
    });
  });
});
