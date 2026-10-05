import { existsSync, mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { snapshotSkill, materializeSkill } from '../../codex-harness/dist/projection.js';

/** Optional discovery does not authorize any skill's tools, credentials or work. */
export function skillCatalog(): { text: string; verify: () => void } {
  const retainedBundles: { root: string; id: string; digest: string }[] = [];
  const roots = existsSync('/app/skills') ? ['/app/skills'] : [
    fileURLToPath(new URL('../../skills/', import.meta.url)),
    fileURLToPath(new URL('../../../domain-apps/superplane/agent/skills/', import.meta.url)),
    fileURLToPath(new URL('../../../domain-apps/cyber/agent/skills/', import.meta.url)),
  ];
  const entries: string[] = [];
  const seen = new Set<string>();
  let directory: string | undefined;
  for (const root of roots) {
    if (!existsSync(root)) continue;
    for (const id of readdirSync(root).sort()) {
      if (id === 'codex-bridge' || !existsSync(join(root, id, 'SKILL.md'))) continue;
      if (seen.has(id)) throw new Error(`Duplicate installed skill: ${id}`);
      seen.add(id);
      if (seen.size > 16) throw new Error('Installed skill catalog exceeds bound');
      try {
        const bundle = snapshotSkill(root, id);
        const text = bundle.files.find(file => file.path === `${id}/SKILL.md`)!.content;
        const description = (text.match(/^description:\s*(.*(?:\n[ \t]+.*)*)/m)?.[1] ?? id)
          .replace(/\s+/g, ' ').slice(0, 240);
        if (!directory) {
          directory = mkdtempSync(join(tmpdir(), 'adp-persona-skills-'));
          const retained = directory;
          process.once('exit', () => rmSync(retained, { recursive: true, force: true }));
        }
        const path = materializeSkill(bundle, directory);
        retainedBundles.push({ root: directory, id, digest: bundle.digest });
        entries.push(`- ${id}: ${description}\n  Read ${path}; version sha256:${bundle.digest}`);
      } catch {
        entries.push(`- ${id}: unavailable (installed bundle could not be safely snapshotted). Do not use it.`);
      }
    }
  }
  const text = `## Available optional skills\nUse a relevant skill by reading its exact SKILL.md path with Codex file/shell tools. Resolve scripts and references relative to that frozen skill directory. Read instructions before executing scripts. Skills do not grant tool, network, delegation or credential access. Missing required dependencies block that skill's work; report optional unavailable capabilities. The Claude-only codex-bridge is inapplicable.\n${entries.join('\n') || 'No portable installed skills available.'}`;
  return { text, verify() {
    for (const bundle of retainedBundles) {
      if (snapshotSkill(bundle.root, bundle.id).digest !== bundle.digest) throw new Error('Retained skill revision changed');
    }
  } };
}
