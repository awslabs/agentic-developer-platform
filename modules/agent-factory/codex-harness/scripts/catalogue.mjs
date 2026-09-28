/** Compile reviewed persona JSON and digest-pinned skills into gateway config.
 * Usage: node scripts/catalogue.mjs --skills ./skills persona.json ... > catalogue.json
 * This emits configuration only. It neither registers nor authorizes a persona.
 */
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { personaSchema, snapshotPersona } from '../dist/persona.js';
const { values, positionals } = parseArgs({ options: { skills: { type: 'string' } }, allowPositionals: true });
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
    sources.set(skill.id, await readFile(resolve(values.skills, skill.id + '.md'), 'utf8'));
  }
  snapshots.push(snapshotPersona(raw, sources));
}
const output = JSON.stringify({ schemaVersion: 1, snapshots });
if (Buffer.byteLength(output) > 2097152) throw new Error('Catalogue exceeds 2 MiB');
process.stdout.write(output + '\n');
