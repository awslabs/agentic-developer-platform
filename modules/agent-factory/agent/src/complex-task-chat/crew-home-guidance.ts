/**
 * Operating guidance for agents in the Crew home (issue #9655).
 *
 * A Kiro Crew dashboard session runs Claude Code and Codex as the Crew agent user in
 * `/mnt/crew`. Both read a user-level instruction file from that home (Claude Code:
 * `~/.claude/CLAUDE.md`; Codex: `~/.codex/AGENTS.md`). Nothing else on the box tells an
 * agent which directory survives a session resume, what the organisation's commit rules
 * demand, or that the owner's AWS connections are already wired into the AWS SDK. On
 * 2026-11-06 a session built an adp-flow plan in its scratch area, the push was refused
 * for a missing issue reference, and the resume discarded the plan.
 *
 * Both files are owned jointly, like the Codex provider block: the text between the two
 * marker comments is rewritten at every Crew start and always sits at the top of the file;
 * everything after it belongs to the user and is kept byte for byte. The block carries no secrets and no per-boot values beyond the
 * AWS region. The writer is advisory: the entrypoint journals a failure and still starts
 * Kiro Crew.
 */

import { mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const CREW_GUIDANCE_MANAGED_BEGIN = '<!-- >>> adp-crew-host managed: working on this ADP Dev Box (rewritten at every Crew start) >>> -->';
export const CREW_GUIDANCE_MANAGED_END = '<!-- <<< adp-crew-host managed <<< -->';

const HOME_PATH = /^\/[A-Za-z0-9_./-]+$/;
const REGION = /^[a-z]{2}(-[a-z]+)+-[0-9]$/;

export interface CrewGuidanceSettings { region: string }

/** The guidance text itself; identical for Claude Code and Codex. */
export function renderCrewGuidance(home: string, settings: CrewGuidanceSettings): string {
  if (!HOME_PATH.test(home)) throw new Error('Crew home path is not a plain absolute path');
  if (!REGION.test(settings.region)) throw new Error('AWS region is not a region name');
  const workspace = `${home}/workspace`;
  return [
    '# Working on this ADP Dev Box',
    '',
    'You run as the Crew agent user in a Kiro Crew session on the owner\'s ADP Dev Box.',
    '',
    '## Where to work',
    '',
    `- This home (\`${home}\`) is retained across session resumes, Crew restarts, stop/start and Replace.`,
    '  The session\'s scratch directory and `/tmp` are not: they can be empty after a resume.',
    `- Clone and work on repositories only under \`${workspace}/<repository>\`. Write plan directories,`,
    '  notes and generated files inside such a checkout, never in a temporary directory.',
    '',
    '## Git and GitHub',
    '',
    '- `gh` and `git` are already authenticated for the owner through the box; never configure another account.',
    '- Repositories in this organisation enforce commit rules: every commit message must reference the GitHub',
    '  issue it serves (for example `#1234`). A push refused by `commit_refs_rules` means that reference is',
    '  missing; amend the message and push again. Do not bypass or disable the rule.',
    '',
    '## ADP',
    '',
    '- `adp` is the box CLI and acts with the owner\'s own ADP authority (`adp status --json` reports',
    '  `mode: host-run`). `adp product …` and `adp flow …` work as on the owner\'s laptop; `adp login`,',
    '  `adp codex|claude setup` and `adp mcp configure` are laptop-only.',
    '- Gate decisions (`adp flow gate approve|reject`) are the owner\'s to make: run them only on the owner\'s',
    '  explicit instruction for that gate, never on your own judgement.',
    '- The ADP skills (`adp-flow`, `adp-grilling`) are installed in this home; the `adp-tools` MCP server',
    '  provides web search, a browser and a code interpreter.',
    '',
    '## AWS accounts',
    '',
    '- The AWS accounts the owner connected in ADP (Settings → Connections, with agent access enabled) are',
    '  available here through the AWS SDK `credential_process` protocol. Your environment already has',
    '  `AWS_CONFIG_FILE=/run/adp-crew-aws/config`; there are no credential files to find or write.',
    `- Default region: \`${settings.region}\` (\`AWS_REGION\`); pass \`--region\` for another one.`,
    '- `adp-cred list` shows the available profiles. With exactly one connected account, plain commands work',
    '  (`aws sts get-caller-identity`). With several, choose one: `aws --profile <profile> …` or',
    '  `adp-cred assume --service aws --label <label> --exec <command>`.',
    '- Sessions last 15 minutes and SDKs refresh them automatically. "Connect an AWS account in ADP Settings"',
    '  or "access is disabled" means the owner has to connect or enable the account in ADP; do not look for',
    '  other credentials. Everything you do there is audited as the owner acting through this box.',
  ].join('\n');
}

/**
 * The user's own text, exactly as written, with the managed block removed.
 *
 * The block is always the first thing in the file, followed by one blank line the writer
 * owns; everything after that is the user's and is never trimmed or reflowed. A file whose
 * marker lies elsewhere, lacks its closing marker, or carries a second marker is ambiguous:
 * `null` tells the caller to leave it untouched rather than guess (and possibly delete
 * user instructions, as a prefix-only strip would).
 */
export function stripGuidanceBlock(text: string): string | null {
  const openings = occurrences(text, CREW_GUIDANCE_MANAGED_BEGIN);
  const closings = occurrences(text, CREW_GUIDANCE_MANAGED_END);
  if (openings.length === 0 && closings.length === 0) return text;
  // Exactly one block, at the top, properly closed. Anything else is not ours to rewrite.
  if (openings.length !== 1 || closings.length !== 1 || openings[0] !== 0 || closings[0] < CREW_GUIDANCE_MANAGED_BEGIN.length) return null;
  let rest = text.slice(closings[0] + CREW_GUIDANCE_MANAGED_END.length);
  if (rest.startsWith('\n\n')) rest = rest.slice(2);
  else if (rest.startsWith('\n')) rest = rest.slice(1);
  return rest;
}

function occurrences(text: string, needle: string): number[] {
  const found: number[] = [];
  for (let at = text.indexOf(needle); at !== -1; at = text.indexOf(needle, at + needle.length)) found.push(at);
  return found;
}

/** The file to write, or `null` when the existing file is ambiguous and must be left alone. */
export function composeCrewGuidance(existing: string, home: string, settings: CrewGuidanceSettings): string | null {
  const user = stripGuidanceBlock(existing);
  if (user === null) return null;
  const block = [CREW_GUIDANCE_MANAGED_BEGIN, renderCrewGuidance(home, settings), CREW_GUIDANCE_MANAGED_END].join('\n');
  return user ? `${block}\n\n${user}` : `${block}\n`;
}

function writeManaged(path: string, next: string): boolean {
  let existing = '';
  try {
    if (statSync(path).isFile()) existing = readFileSync(path, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  if (next === existing) return false;
  const temporary = `${path}.adp-${process.pid}.tmp`;
  writeFileSync(temporary, next, { mode: 0o600 });
  renameSync(temporary, path);
  return true;
}

/** The two instruction files, relative to the Crew home. */
export const CREW_GUIDANCE_FILES = ['.claude/CLAUDE.md', '.codex/AGENTS.md'] as const;

/**
 * Write the managed guidance into `<home>/.claude/CLAUDE.md` and `<home>/.codex/AGENTS.md`
 * and make sure `<home>/workspace` exists. Returns the files that changed.
 */
export async function writeCrewHomeGuidance(home: string, settings: CrewGuidanceSettings,
  report: (line: string) => void = line => process.stderr.write(`${line}\n`)): Promise<string[]> {
  mkdirSync(join(home, 'workspace'), { recursive: true, mode: 0o700 });
  const changed: string[] = [];
  for (const relative of CREW_GUIDANCE_FILES) {
    const path = join(home, relative);
    mkdirSync(join(path, '..'), { recursive: true, mode: 0o700 });
    let existing = '';
    try { existing = readFileSync(path, 'utf8'); } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }
    const next = composeCrewGuidance(existing, home, settings);
    if (next === null) {
      report(JSON.stringify({ event: 'crew_home_guidance_ambiguous', path, message: 'managed markers are misplaced or unmatched; file left unchanged' }));
      continue;
    }
    if (writeManaged(path, next)) changed.push(relative);
  }
  if (changed.length) report(JSON.stringify({ event: 'crew_home_guidance_written', files: changed }));
  return changed;
}
