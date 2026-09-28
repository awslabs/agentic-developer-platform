/** Compile reviewed persona JSON and digest-pinned skills into gateway config.
 * Usage: node scripts/catalogue.mjs --skills ./skills persona.json ... > catalogue.json
 * This emits configuration only. It neither registers nor authorizes a persona.
 */
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { personaSchema, snapshotPersona } from '../dist/persona.js';
import { projectRules, COMMON_RULES, phaseRules, readInstructionSource } from '../dist/projection.js';
const { values, positionals } = parseArgs({ options: { skills: { type: 'string' }, rules: { type: 'string', default: fileURLToPath(new URL('../../rules/', import.meta.url)) } }, allowPositionals: true });
if (!positionals.length || positionals.length > 64) throw new Error('Provide between 1 and 64 persona definitions');
const snapshots = [];
const keys = new Set();
for (const file of positionals) {
  const raw = await readFile(file, 'utf8');
  const persona = personaSchema.parse(JSON.parse(raw));
  if (keys.has(persona.key)) throw new Error('Duplicate persona key');
  keys.add(persona.key);
  const sources = new Map();
  for (const skill of persona.skills) {
    if (!values.skills) throw new Error('Pinned skills require --skills directory');
    const skillPath = existsSync(resolve(values.skills, skill.id, 'SKILL.md')) ? `${skill.id}/SKILL.md` : `${skill.id}.md`;
    sources.set(skill.id, readInstructionSource(values.skills, skillPath).content);
  }
  const base = persona.key.slice(4);
  const sharedRules = projectRules(values.rules, base, [
    ...COMMON_RULES, `personas/${base}.md`, ...phaseRules(values.rules, base),
  ]);
  snapshots.push(snapshotPersona(JSON.stringify({ ...persona, sharedRules: { ...sharedRules, sources: sharedRules.sources.map(({ path, sha256 }) => ({ path, sha256 })) } }), sources));
}
const output = JSON.stringify({ schemaVersion: 1, snapshots });
if (Buffer.byteLength(output) > 2097152) throw new Error('Catalogue exceeds 2 MiB');
process.stdout.write(output + '\n');
