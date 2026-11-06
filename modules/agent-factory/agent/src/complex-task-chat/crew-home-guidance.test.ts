import { mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CREW_GUIDANCE_MANAGED_BEGIN, CREW_GUIDANCE_MANAGED_END, composeCrewGuidance, renderCrewGuidance, stripGuidanceBlock, writeCrewHomeGuidance } from './crew-home-guidance';

const settings = { region: 'us-east-1' };

function home(): string { return mkdtempSync(join(tmpdir(), 'crew-home-')); }

test('a fresh Crew home gets the workspace directory and the same guidance for Claude Code and Codex (#9655 AC-01)', async () => {
  const dir = home();
  const reported: string[] = [];
  expect(await writeCrewHomeGuidance(dir, settings, line => reported.push(line))).toEqual(['.claude/CLAUDE.md', '.codex/AGENTS.md']);
  expect(statSync(join(dir, 'workspace')).isDirectory()).toBe(true);
  const claude = readFileSync(join(dir, '.claude/CLAUDE.md'), 'utf8');
  const codex = readFileSync(join(dir, '.codex/AGENTS.md'), 'utf8');
  expect(codex).toBe(claude);
  expect(statSync(join(dir, '.claude/CLAUDE.md')).mode & 0o777).toBe(0o600);
  expect(claude.startsWith(CREW_GUIDANCE_MANAGED_BEGIN)).toBe(true);
  expect(claude.trimEnd().endsWith(CREW_GUIDANCE_MANAGED_END)).toBe(true);
  // The three facts the 2026-11-06 session lacked.
  expect(claude).toContain(`${dir}/workspace/<repository>`);
  expect(claude).toContain('can be empty after a resume');
  expect(claude).toContain('commit_refs_rules');
  expect(claude).toContain('must reference the GitHub');
  expect(claude).toContain('AWS_CONFIG_FILE=/run/adp-crew-aws/config');
  expect(claude).toContain('adp-cred list');
  expect(claude).toContain('aws --profile <profile>');
  expect(claude).toContain('adp-cred assume --service aws --label <label> --exec <command>');
  expect(claude).toContain('Sessions last 15 minutes');
  expect(claude).toContain('Default region: `us-east-1`');
  expect(claude).toContain('Connect an AWS account in ADP Settings');
  // Gate decisions stay the owner's even though the CLI now holds the owner's authority (#9640).
  expect(claude).toContain('only on the owner\'s');
  expect(reported).toEqual([JSON.stringify({ event: 'crew_home_guidance_written', files: ['.claude/CLAUDE.md', '.codex/AGENTS.md'] })]);
});

test('user-authored text is kept byte for byte after the block; a stale block is replaced; a second run writes nothing (AC-02)', async () => {
  const dir = home();
  mkdirSync(join(dir, '.claude'));
  // Leading indentation (a Markdown code block), inner blank lines and trailing blank lines all matter.
  const mine = '    Preserve this Markdown code block\n\n\n# Mine\n\nAlways answer in British English.\n\n\n';
  writeFileSync(join(dir, '.claude/CLAUDE.md'), mine);
  await writeCrewHomeGuidance(dir, { region: 'eu-west-1' }, () => undefined);
  const reported: string[] = [];
  expect(await writeCrewHomeGuidance(dir, settings, line => reported.push(line))).toEqual(['.claude/CLAUDE.md', '.codex/AGENTS.md']);
  const text = readFileSync(join(dir, '.claude/CLAUDE.md'), 'utf8');
  expect(text.startsWith(CREW_GUIDANCE_MANAGED_BEGIN)).toBe(true);
  expect(text.endsWith(CREW_GUIDANCE_MANAGED_END + '\n\n' + mine)).toBe(true);
  expect(text.match(/adp-crew-host managed:/g)).toHaveLength(1);
  expect(text).not.toContain('eu-west-1');
  expect(text).toContain('us-east-1');
  expect(await writeCrewHomeGuidance(dir, settings, line => reported.push(line))).toEqual([]);
  expect(reported).toHaveLength(1);
  // Codex's file had no user text: block only, no stray blank line.
  expect(readFileSync(join(dir, '.codex/AGENTS.md'), 'utf8').endsWith(CREW_GUIDANCE_MANAGED_END + '\n')).toBe(true);
});

test('user text that starts with a newline survives a round trip', () => {
  const user = '\n\n- Never deploy without approval.\n';
  const once = composeCrewGuidance(user, '/mnt/crew', settings)!;
  expect(once.endsWith(CREW_GUIDANCE_MANAGED_END + '\n\n' + user)).toBe(true);
  expect(composeCrewGuidance(once, '/mnt/crew', settings)).toBe(once);
  expect(stripGuidanceBlock(once)).toBe(user);
});

test('misplaced or unmatched markers never delete user text: the file is left alone and reported (R2)', async () => {
  const dir = home();
  mkdirSync(join(dir, '.claude'));
  mkdirSync(join(dir, '.codex'));
  const unmatched = `${CREW_GUIDANCE_MANAGED_BEGIN}\nstale block without its end\n\nNever deploy without approval.\n`;
  const misplaced = `# Mine first\n\n${CREW_GUIDANCE_MANAGED_BEGIN}\nold\n${CREW_GUIDANCE_MANAGED_END}\n\nNever deploy without approval.\n`;
  writeFileSync(join(dir, '.claude/CLAUDE.md'), unmatched);
  writeFileSync(join(dir, '.codex/AGENTS.md'), misplaced);
  const reported: string[] = [];
  expect(await writeCrewHomeGuidance(dir, settings, line => reported.push(line))).toEqual([]);
  expect(readFileSync(join(dir, '.claude/CLAUDE.md'), 'utf8')).toBe(unmatched);
  expect(readFileSync(join(dir, '.codex/AGENTS.md'), 'utf8')).toBe(misplaced);
  expect(reported.filter(line => line.includes('crew_home_guidance_ambiguous'))).toHaveLength(2);
  expect(composeCrewGuidance(`${CREW_GUIDANCE_MANAGED_BEGIN}\na\n${CREW_GUIDANCE_MANAGED_END}\n\n${CREW_GUIDANCE_MANAGED_END}\n`, '/mnt/crew', settings)).toBeNull();
});

test('a duplicate opening marker before the closing one, or a closing marker alone, is ambiguous and leaves the file byte-identical (R2)', async () => {
  const dir = home();
  mkdirSync(join(dir, '.claude'));
  mkdirSync(join(dir, '.codex'));
  const duplicateOpening = `${CREW_GUIDANCE_MANAGED_BEGIN}\nstale\nNever deploy without approval.\n${CREW_GUIDANCE_MANAGED_BEGIN}\nsecond\n${CREW_GUIDANCE_MANAGED_END}\n`;
  const closingOnly = `# Mine\n${CREW_GUIDANCE_MANAGED_END}\nNever deploy without approval.\n`;
  writeFileSync(join(dir, '.claude/CLAUDE.md'), duplicateOpening);
  writeFileSync(join(dir, '.codex/AGENTS.md'), closingOnly);
  const reported: string[] = [];
  expect(await writeCrewHomeGuidance(dir, settings, line => reported.push(line))).toEqual([]);
  expect(readFileSync(join(dir, '.claude/CLAUDE.md'), 'utf8')).toBe(duplicateOpening);
  expect(readFileSync(join(dir, '.codex/AGENTS.md'), 'utf8')).toBe(closingOnly);
  expect(reported.filter(line => line.includes('crew_home_guidance_ambiguous'))).toHaveLength(2);
  expect(stripGuidanceBlock(duplicateOpening)).toBeNull();
  expect(stripGuidanceBlock(closingOnly)).toBeNull();
  // Markers inside the user's text after a valid block are theirs to keep, so that file is ambiguous too.
  expect(stripGuidanceBlock(`${CREW_GUIDANCE_MANAGED_BEGIN}\na\n${CREW_GUIDANCE_MANAGED_END}\n\nsee ${CREW_GUIDANCE_MANAGED_BEGIN}\n`)).toBeNull();
});

test('the block never carries anything but the home path and the region, and refuses odd values', () => {
  expect(() => renderCrewGuidance('/mnt/crew', { region: 'us-east-1; rm -rf' })).toThrow('region');
  expect(() => renderCrewGuidance('/mnt/crew\n', settings)).toThrow('home path');
  const text = composeCrewGuidance('', '/mnt/crew', settings)!;
  expect(text).not.toMatch(/[0-9a-f]{64}/);
  expect(text).not.toContain('127.0.0.1');
});
