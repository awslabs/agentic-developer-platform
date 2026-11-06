/**
 * Codex provider configuration for the Crew home (issue #9628).
 *
 * A Kiro Crew dashboard session runs Claude Code and Codex as the Crew agent user in
 * `/mnt/crew`. Claude Code is configured by the `ANTHROPIC_*` environment the host model
 * entrypoint already exports; Codex reads a provider from `~/.codex/config.toml` and the
 * key from the environment variable that provider names. The owner shell gets the same
 * wiring from `prepare_shell.sh`'s `codex()` function, which never touches this home.
 *
 * The file is owned jointly: everything between the two marker lines is rewritten on every
 * Crew start (the adapter's loopback port and the published model change per boot); anything
 * outside the markers belongs to the user (and to Codex itself, e.g. `[tui]`) and is kept.
 * A user file that already defines the keys this block sets is left alone with a warning,
 * never merged, because duplicate TOML keys would make Codex refuse the whole file.
 */

import { mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export const CREW_CODEX_MANAGED_BEGIN = '# >>> adp-crew-host managed: Codex provider for this Dev Box (rewritten at every Crew start) >>>';
export const CREW_CODEX_MANAGED_END = '# <<< adp-crew-host managed <<<';

const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/;
const LOOPBACK_BASE_URL = /^http:\/\/127\.0\.0\.1:[0-9]{2,5}$/;
const HOME_PATH = /^\/[A-Za-z0-9_./-]+$/;
/** Keys and tables the managed block defines; a user file that sets them is not touched. */
const CONFLICTING_LINES = [
  /^\s*model\s*=/, /^\s*model_provider\s*=/, /^\s*web_search\s*=/,
  /^\s*\[model_providers\.adp-shell\]/, /^\s*\[mcp_servers\.adp-tools\]/, /^\s*\[projects\./,
];

export interface CrewCodexSettings { modelId: string; baseUrl: string }

export function renderCrewCodexManagedBlock(home: string, settings: CrewCodexSettings): { topLevel: string; tables: string } {
  if (!MODEL_ID.test(settings.modelId)) throw new Error('Codex model id is not a model identifier');
  if (!LOOPBACK_BASE_URL.test(settings.baseUrl)) throw new Error('Codex provider base URL must be the loopback adapter');
  if (!HOME_PATH.test(home)) throw new Error('Crew home path is not a plain absolute path');
  const topLevel = [
    `model = "${settings.modelId}"`,
    'model_provider = "adp-shell"',
    '# Bedrock serves no hosted web-search tool for GPT models; Codex sends it by default and the whole turn',
    '# is rejected ("web search is not supported for this request"). ADP\'s websearch lives in adp-tools (#8859).',
    'web_search = "disabled"',
  ].join('\n');
  const tables = [
    '[model_providers.adp-shell]',
    'name = "ADP Dev Box"',
    `base_url = "${settings.baseUrl}/v1"`,
    'wire_api = "responses"',
    'env_key = "OPENAI_API_KEY"',
    'requires_openai_auth = false',
    '',
    '[mcp_servers.adp-tools]',
    'command = "/opt/adp/crew/current/venv/bin/python3"',
    'args = ["/opt/adp/crew/current/host/adp/adp-mcp.py"]',
    '',
    `[projects."${home}"]`,
    'trust_level = "trusted"',
  ].join('\n');
  return { topLevel, tables };
}

/** Split a user config into the top-level key lines (before any table header) and the rest. */
function splitUserConfig(text: string): { leading: string; rest: string } {
  const lines = text.split('\n');
  const firstTable = lines.findIndex(line => /^\s*\[/.test(line));
  if (firstTable === -1) return { leading: text.trim(), rest: '' };
  return { leading: lines.slice(0, firstTable).join('\n').trim(), rest: lines.slice(firstTable).join('\n').trim() };
}

/** Everything outside a previous managed block, or the whole text when none is present. */
export function stripManagedBlock(text: string): string {
  const begin = text.indexOf(CREW_CODEX_MANAGED_BEGIN);
  if (begin === -1) return text;
  const end = text.indexOf(CREW_CODEX_MANAGED_END, begin);
  if (end === -1) return text.slice(0, begin);
  return text.slice(0, begin) + text.slice(end + CREW_CODEX_MANAGED_END.length);
}

export function composeCrewCodexConfig(existing: string, home: string, settings: CrewCodexSettings): string | null {
  const user = stripManagedBlock(existing);
  if (user.split('\n').some(line => CONFLICTING_LINES.some(pattern => pattern.test(line)))) return null;
  const { topLevel, tables } = renderCrewCodexManagedBlock(home, settings);
  const { leading, rest } = splitUserConfig(user);
  // TOML: every top-level key must precede the first table header, including the user's own.
  const parts = [CREW_CODEX_MANAGED_BEGIN, topLevel];
  if (leading) parts.push('', leading);
  parts.push('', tables, CREW_CODEX_MANAGED_END);
  if (rest) parts.push('', rest);
  return `${parts.join('\n')}\n`;
}

/**
 * Write the managed Codex provider into `<home>/.codex/config.toml`.
 *
 * Returns `true` when the file now carries the managed block, `false` when a user-authored file
 * already defines the same keys and was left alone (reported through `report`).
 */
export async function writeCrewHomeCodexConfig(home: string, settings: CrewCodexSettings,
  report: (line: string) => void = line => process.stderr.write(`${line}\n`)): Promise<boolean> {
  const directory = join(home, '.codex');
  const path = join(directory, 'config.toml');
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  let existing = '';
  try {
    if (statSync(path).isFile()) existing = readFileSync(path, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
  const next = composeCrewCodexConfig(existing, home, settings);
  if (next === null) {
    report(JSON.stringify({ event: 'crew_home_codex_config_user_owned', path, message: 'config.toml already defines the provider keys; left unchanged' }));
    return false;
  }
  if (next === existing) return true;
  const temporary = `${path}.adp-${process.pid}.tmp`;
  writeFileSync(temporary, next, { mode: 0o600 });
  renameSync(temporary, path);
  return true;
}
