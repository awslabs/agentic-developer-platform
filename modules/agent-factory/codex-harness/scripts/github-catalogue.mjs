/** Refresh the generated gateway build artifact from the canonical definitions. */
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const path = relative => fileURLToPath(new URL(relative, import.meta.url));
const output = execFileSync(process.execPath, [path('./catalogue.mjs'),
  ...['architect', 'product', 'pm', 'intent-refinement'].map(key => path(`../personas/${key}.json`))]);
const target = path('../../../gateway/src/agentauth/codex-github-catalogue.json');
if (process.argv.includes('--check')) {
  if (!output.equals(readFileSync(target))) throw new Error('Regenerate gateway catalogue: node scripts/github-catalogue.mjs');
} else writeFileSync(target, output);
