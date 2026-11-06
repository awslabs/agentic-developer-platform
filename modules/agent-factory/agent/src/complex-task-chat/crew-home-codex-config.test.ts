import { mkdtempSync, readFileSync, statSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CREW_CODEX_MANAGED_BEGIN, CREW_CODEX_MANAGED_END, composeCrewCodexConfig, writeCrewHomeCodexConfig } from './crew-home-codex-config';

const settings = { modelId: 'global.anthropic.claude-sonnet-5', baseUrl: 'http://127.0.0.1:39743' };

function home(): string { return mkdtempSync(join(tmpdir(), 'crew-home-')); }

test('a fresh Crew home gets the adp-shell provider, the published model, no web search and the adp-tools MCP server (#9628)', async () => {
  const dir = home();
  const reported: string[] = [];
  expect(await writeCrewHomeCodexConfig(dir, settings, line => reported.push(line))).toBe(true);
  const path = join(dir, '.codex/config.toml');
  const text = readFileSync(path, 'utf8');
  expect(statSync(path).mode & 0o777).toBe(0o600);
  expect(text.startsWith(CREW_CODEX_MANAGED_BEGIN)).toBe(true);
  expect(text).toContain('model = "global.anthropic.claude-sonnet-5"');
  expect(text).toContain('model_provider = "adp-shell"');
  expect(text).toContain('web_search = "disabled"');
  expect(text).toContain('base_url = "http://127.0.0.1:39743/v1"');
  expect(text).toContain('wire_api = "responses"');
  expect(text).toContain('env_key = "OPENAI_API_KEY"');
  expect(text).toContain('args = ["/opt/adp/crew/current/host/adp/adp-mcp.py"]');
  expect(text).toContain(`[projects."${dir}"]`);
  expect(reported).toEqual([]);
  // Every top-level key precedes the first table header, as TOML requires.
  expect(text.indexOf('web_search')).toBeLessThan(text.indexOf('[model_providers.adp-shell]'));
});

test('Codex\'s own and the user\'s settings outside the markers are kept; a stale managed block is replaced', async () => {
  const dir = home();
  mkdirSync(join(dir, '.codex'));
  writeFileSync(join(dir, '.codex/config.toml'), '[tui]\nscreen_reader_detection_done = true\n');
  await writeCrewHomeCodexConfig(dir, { ...settings, baseUrl: 'http://127.0.0.1:11111' });
  await writeCrewHomeCodexConfig(dir, settings);
  const text = readFileSync(join(dir, '.codex/config.toml'), 'utf8');
  expect(text.match(/adp-crew-host managed:/g)).toHaveLength(1);
  expect(text).not.toContain('127.0.0.1:11111');
  expect(text).toContain('127.0.0.1:39743/v1');
  expect(text.trim().endsWith('[tui]\nscreen_reader_detection_done = true')).toBe(true);
  expect(text.indexOf(CREW_CODEX_MANAGED_END)).toBeLessThan(text.indexOf('[tui]'));
});

test('user top-level keys stay top-level (before any table) and user tables follow the managed block', () => {
  const text = composeCrewCodexConfig('approval_policy = "never"\n\n[tui]\nx = 1\n', '/mnt/crew', settings)!;
  const order = ['model = ', 'approval_policy = "never"', '[model_providers.adp-shell]', '[projects."/mnt/crew"]', CREW_CODEX_MANAGED_END, '[tui]']
    .map(needle => text.indexOf(needle));
  expect(order.every(index => index >= 0)).toBe(true);
  expect([...order].sort((a, b) => a - b)).toEqual(order);
});

test('a user-authored provider is never merged over: the file is left unchanged and the refusal is journaled', async () => {
  const dir = home();
  mkdirSync(join(dir, '.codex'));
  const own = 'model = "gpt-5"\nmodel_provider = "mine"\n[model_providers.mine]\nbase_url = "https://example.test/v1"\n';
  writeFileSync(join(dir, '.codex/config.toml'), own);
  const reported: string[] = [];
  expect(await writeCrewHomeCodexConfig(dir, settings, line => reported.push(line))).toBe(false);
  expect(readFileSync(join(dir, '.codex/config.toml'), 'utf8')).toBe(own);
  expect(reported).toHaveLength(1);
  expect(JSON.parse(reported[0]).event).toBe('crew_home_codex_config_user_owned');
});

test('only the loopback adapter and a plain model id are written', () => {
  expect(() => composeCrewCodexConfig('', '/mnt/crew', { ...settings, baseUrl: 'http://10.0.0.5:39743' })).toThrow(/loopback/);
  expect(() => composeCrewCodexConfig('', '/mnt/crew', { ...settings, modelId: 'x"\ny = 1' })).toThrow(/model identifier/);
  expect(() => composeCrewCodexConfig('', '/mnt/crew"]\n[evil', settings)).toThrow(/home path/);
});
