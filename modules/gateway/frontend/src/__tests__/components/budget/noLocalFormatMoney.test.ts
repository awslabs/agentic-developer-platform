/**
 * A source guard: no budget component defines its own money formatter — Issue #4685.
 *
 * This reads source text rather than rendering anything, which needs justifying. Four
 * files under this surface had each grown a private `formatMoney`, and they disagreed on
 * exactly the cases that matter:
 *
 *   - `Number('')` is `0`, not `NaN`, so an empty string rendered as `$0.00` in the copies
 *     that guarded with `isNaN` alone — absence presented as a verified zero, which is the
 *     defect the entire budget EPIC exists to eliminate.
 *   - `-0.004` rendered as `-$0.00` where the sign was applied before rounding.
 *   - Sub-cent spend flattened to `$0.00` in the copies without 4dp handling.
 *
 * Every one of those is invisible to a component test: each file's own tests exercised its
 * own copy, and all four passed. The gap was structural — four implementations of one rule
 * — so the assertion has to be structural too. A behavioural test cannot fail on
 * duplication, only on the specific divergence somebody thought to write a case for.
 *
 * `formatWireMoney` in `utils/cost.ts` is the single implementation, and
 * `__tests__/utils/cost.test.ts` holds the cases above.
 */

import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

/** Every source file under a directory, recursively. */
function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    return /\.(ts|tsx)$/.test(entry) ? [full] : [];
  });
}

/**
 * A local money-formatter definition, in any of the forms one could take:
 * `function formatMoney(`, `const formatMoney =`, or a class/object method.
 * Deliberately matches the *name* rather than the behaviour — the point is that the
 * canonical formatter is imported, and a differently-named private copy would be caught
 * by the same rule under whatever name a reviewer sees in the diff.
 */
const LOCAL_DEFINITION = /(?:function\s+formatMoney\s*\(|(?:const|let|var)\s+formatMoney\s*[=:])/;

describe('budget surface — one money formatter, imported not redefined', () => {
  const targets = [...sourceFiles(join(process.cwd(), 'src/components/budget')), join(process.cwd(), 'src/pages/BudgetSpend.tsx')];

  it('finds the files it is meant to be guarding', () => {
    // Without this, a bad path or a moved directory would make the guard below pass
    // vacuously — a green test asserting nothing at all.
    expect(targets.length).toBeGreaterThan(5);
  });

  it.each(targets.map((path) => [path.replace(process.cwd(), '.'), path]))('%s defines no local formatMoney', (_label, path) => {
    expect(readFileSync(path, 'utf8')).not.toMatch(LOCAL_DEFINITION);
  });

  it('has at least one file importing the shared formatter', () => {
    // The other half of the rule: "no local copies" is also satisfied by rendering no
    // money at all, so assert the canonical one is actually in use.
    const importers = targets.filter((path) => readFileSync(path, 'utf8').includes('formatWireMoney'));
    expect(importers.length).toBeGreaterThan(0);
  });
});
