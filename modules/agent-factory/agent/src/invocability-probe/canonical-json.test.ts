import manifest from './request-shape-manifest.json';
import { REQUEST_SHAPE_NORMALIZATION, canonicalJson, normalizeRequestShape, requestShapeSha256 } from './canonical-json';

describe('canonical JSON request digest', () => {
  it('sorts object keys recursively without reordering arrays', () => {
    expect(canonicalJson({ z: 1, a: { d: 2, c: [3, 1] } }))
      .toBe('{"a":{"c":[3,1],"d":2},"z":1}');
  });

  it('ignores whitespace and insertion order', () => {
    expect(requestShapeSha256('{ "b": 2, "a": 1 }'))
      .toBe(requestShapeSha256('{"a":1,"b":2}'));
  });

  it('rejects a non-JSON body', () => {
    expect(() => requestShapeSha256('not-json')).toThrow('non-JSON Bedrock request');
  });

  it('records the normalization revision the manifests were generated under', () => {
    expect(manifest.request_shape_normalization).toBe(REQUEST_SHAPE_NORMALIZATION);
    expect(REQUEST_SHAPE_NORMALIZATION).toBe('claude-code-probe-context-v5');
  });
});

const identity = {
  device_id: 'a'.repeat(64), account_uuid: '',
  session_id: '00000000-0000-4000-8000-000000000002',
};

/** Claude Code 0.3.283 context for models without the mid-conversation beta (Haiku 4.5, Sonnet 4.x). */
function userContextRequest(context: {
  shell?: string; os?: string; budget?: string; spent?: string; remaining?: string; date?: string;
  platform?: string; model?: string; tokensLeft?: string;
} = {}, request: Record<string, unknown> = {}) {
  const budget = context.budget ?? '0.01';
  return JSON.stringify({
    anthropic_beta: ['tool-search-tool-2025-10-19'],
    messages: [{ role: 'user', content: [
      { type: 'text', text: `<system-reminder>\n# Environment\nYou have been invoked in the following environment: \n - Primary working directory: /tmp\n - Is a git repository: false\n - Platform: ${context.platform ?? 'linux'}\n - Shell: ${context.shell ?? 'unknown'}\n - OS Version: ${context.os ?? 'Linux 6.12.110'}\n</system-reminder>` },
      { type: 'text', text: `<system-reminder>\nYou are powered by the model named ${context.model ?? 'Haiku 4.5'}.\n</system-reminder>` },
      { type: 'text', text: `<system-reminder>\n<total_tokens>${context.tokensLeft ?? '15000000'} tokens left</total_tokens>\n</system-reminder>` },
      { type: 'text', text: `<system-reminder>\nUSD budget: $${context.spent ?? '0'}/$${budget}; $${context.remaining ?? budget} remaining\n</system-reminder>` },
      { type: 'text', text: `<system-reminder>\nToday's date is ${context.date ?? '2026-10-17'}.\n</system-reminder>` },
      { type: 'text', text: 'Reply with exactly ADP_PROBE_OK. Do not use tools.' },
    ] }],
    metadata: { user_id: JSON.stringify(identity) },
    max_tokens: 32000, thinking: { type: 'enabled', budget_tokens: 31999 }, tools: [{ name: 'Read' }],
    ...request,
  });
}

/** Claude Code 0.3.283 context for Opus 5 / Sonnet 5: one system-role message after the prompt. */
function systemContextRequest(context: {
  shell?: string; os?: string; budget?: string; spent?: string; remaining?: string; date?: string;
  beta?: string[]; systemRole?: string; model?: string;
} = {}, request: Record<string, unknown> = {}) {
  const budget = context.budget ?? '0.01';
  return JSON.stringify({
    anthropic_beta: context.beta ?? ['interleaved-thinking-2025-05-14', 'mid-conversation-system-2026-04-07'],
    messages: [
      { role: 'user', content: [{ type: 'text', text: 'Reply with exactly ADP_PROBE_OK. Do not use tools.' }] },
      { role: context.systemRole ?? 'system', content: [{ type: 'text', cache_control: { type: 'ephemeral' }, text:
        `# Environment\nYou have been invoked in the following environment: \n - Primary working directory: /tmp\n - Is a git repository: false\n - Platform: linux\n - Shell: ${context.shell ?? 'unknown'}\n - OS Version: ${context.os ?? 'Linux 6.12.110'}\n\nYou are powered by the model named ${context.model ?? 'Opus 5'}.\n\n<total_tokens>15000000 tokens left</total_tokens>\n\nUSD budget: $${context.spent ?? '0'}/$${budget}; $${context.remaining ?? budget} remaining\n\nToday's date is ${context.date ?? '2026-10-17'}.` }] },
    ],
    metadata: { user_id: JSON.stringify(identity) },
    max_tokens: 64000, thinking: { type: 'adaptive' }, output_config: { effort: 'low' }, tools: [{ name: 'Read' }],
    ...request,
  });
}

describe.each([
  ['user system-reminder blocks', userContextRequest],
  ['mid-conversation system message', systemContextRequest],
])('0.3.283 probe context in %s', (_shape, request) => {
  const reference = requestShapeSha256(request());

  it('keeps a stable fingerprint across operator budgets (generator 0.01, gateway 0.5 or 1)', () => {
    expect(requestShapeSha256(request({ budget: '0.5' }))).toBe(reference);
    expect(requestShapeSha256(request({ budget: '1' }))).toBe(reference);
  });

  it('keeps a stable fingerprint across wall-clock dates', () => {
    expect(requestShapeSha256(request({ date: '2026-09-27' }))).toBe(reference);
    expect(requestShapeSha256(request({ date: '2030-01-02' }))).toBe(reference);
  });

  it('keeps a stable fingerprint across the host shell and kernel of the pod or developer machine', () => {
    expect(requestShapeSha256(request({ shell: 'bash', os: 'Linux 7.0.0-1013-aws' }))).toBe(reference);
    expect(requestShapeSha256(request({ shell: 'zsh', os: 'Darwin 25.0.0' }))).toBe(reference);
  });

  it('places placeholders where the run-specific values were', () => {
    const normalized = canonicalJson(normalizeRequestShape(JSON.parse(request({ budget: '0.5', shell: 'bash' }))));
    expect(normalized).toContain(' - Shell: <ADP_PROBE_SHELL>\\n - OS Version: <ADP_PROBE_OS_VERSION>');
    expect(normalized).toContain('USD budget: $0/$<ADP_PROBE_BUDGET>; $<ADP_PROBE_BUDGET> remaining');
    expect(normalized).toContain("Today's date is <ADP_PROBE_DATE>.");
    expect(normalized).not.toContain('0.5');
    expect(normalized).not.toContain('bash');
  });

  it.each([
    { spent: '0.001', remaining: '0.009' }, { remaining: '0.5' }, { budget: '0' },
  ])('retains spend and unequal or zero budgets: %j', (change) => {
    expect(requestShapeSha256(request(change))).not.toBe(reference);
  });

  it('retains the model, the platform and the token budget the SDK reports', () => {
    expect(requestShapeSha256(request({ model: 'Opus 4.8' }))).not.toBe(reference);
    if (request === userContextRequest) {
      expect(requestShapeSha256(request({ platform: 'darwin' }))).not.toBe(reference);
      expect(requestShapeSha256(request({ tokensLeft: '1000' }))).not.toBe(reference);
    }
  });

  it.each([{ max_tokens: 1 }, { tools: [{ name: 'Bash' }] }, { model: 'other-model' }, { thinking: { type: 'disabled' } }])(
    'retains provider request changes: %j', (change) => {
      expect(requestShapeSha256(request({}, change))).not.toBe(reference);
    },
  );
});

describe('normalization boundaries', () => {
  it('fires each rule for exactly one occurrence', () => {
    const twice = (budget: string) => JSON.stringify({
      messages: [{ role: 'user', content: [
        { text: `<system-reminder>\nUSD budget: $0/$${budget}; $${budget} remaining\n</system-reminder>` },
        { text: `USD budget: $0/$${budget}; $${budget} remaining` },
      ] }],
    });
    expect(requestShapeSha256(twice('0.01'))).not.toBe(requestShapeSha256(twice('0.5')));
    const dates = (date: string) => JSON.stringify({
      messages: [{ role: 'user', content: [{ text: `Today's date is ${date}.` }, { text: `Today's date is ${date}.` }] }],
    });
    expect(requestShapeSha256(dates('2026-10-17'))).not.toBe(requestShapeSha256(dates('2026-10-18')));
  });

  it('normalizes host lines only inside an "# Environment" block', () => {
    const hostLines = (os: string, heading: string) => JSON.stringify({
      messages: [{ role: 'user', content: [{ text: `${heading} - Primary working directory: /tmp\n - Shell: bash\n - OS Version: ${os}` }] }],
    });
    expect(requestShapeSha256(hostLines('Linux 6.12.110', '# Environment\n')))
      .toBe(requestShapeSha256(hostLines('Linux 7.0.0', '# Environment\n')));
    expect(requestShapeSha256(hostLines('Linux 6.12.110', '# Not the SDK\n')))
      .not.toBe(requestShapeSha256(hostLines('Linux 7.0.0', '# Not the SDK\n')));
  });

  it('scans the second message only as the SDK system message under the mid-conversation beta', () => {
    expect(requestShapeSha256(systemContextRequest({ beta: [], budget: '0.01' })))
      .not.toBe(requestShapeSha256(systemContextRequest({ beta: [], budget: '0.5' })));
    expect(requestShapeSha256(systemContextRequest({ systemRole: 'assistant', budget: '0.01' })))
      .not.toBe(requestShapeSha256(systemContextRequest({ systemRole: 'assistant', budget: '0.5' })));
  });

  it('never scans later turns or the top-level system prompt', () => {
    const later = (date: string) => JSON.stringify({
      system: [{ text: `Today's date is ${date}.` }],
      messages: [{ role: 'user', content: [] }, { role: 'assistant', content: [] }, { role: 'user', content: [{ text: `Today's date is ${date}.` }] }],
    });
    expect(requestShapeSha256(later('2026-10-17'))).not.toBe(requestShapeSha256(later('2026-10-18')));
  });

  it('still covers the 0.3.220 reminder wrappers', () => {
    const legacy = (date: string, budget: string) => JSON.stringify({
      messages: [{ content: [
        { text: `<system-reminder>\nUSD budget: $0/$${budget}; $${budget} remaining\n</system-reminder>\n` },
        { text: `context\n# currentDate\nToday's date is ${date}.\n` },
      ] }],
      tools: [{ name: 'Bash' }],
    });
    expect(requestShapeSha256(legacy('2026-09-19', '0.01'))).toBe(requestShapeSha256(legacy('2030-01-02', '1')));
  });
});

const body = (changes = {}, request = {}) => JSON.stringify({
  metadata: { user_id: JSON.stringify({ ...identity, ...changes }) },
  messages: [{ content: [{ text: "Today's date is 2026-09-20." }] }],
  max_tokens: 32000, tools: [{ name: 'Read' }], ...request,
});

it('keeps a stable fingerprint across fresh anonymous probe containers', () => {
  expect(requestShapeSha256(body())).toBe(requestShapeSha256(body({ device_id: 'b'.repeat(64) })));
});

it.each([
  { account_uuid: 'another-account' }, { session_id: 'another-session' },
  { device_id: 'unexpected-format' }, { extra: 'new-sdk-field' },
])('retains changed identity contracts: %j', (change) => {
  expect(requestShapeSha256(body())).not.toBe(requestShapeSha256(body(change)));
});
