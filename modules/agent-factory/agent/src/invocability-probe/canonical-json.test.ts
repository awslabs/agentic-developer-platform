import { canonicalJson, requestShapeSha256 } from './canonical-json';

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

  it('normalizes only the reviewed Claude Code current-date reminder path', () => {
    const body = (date: string, suffix = '') => JSON.stringify({
      messages: [{ content: [{}, {}, {}, {
        text: `context\n# currentDate\nToday's date is ${date}.\n${suffix}`,
      }] }],
      tools: [{ name: 'Bash' }],
      thinking: { type: 'enabled', budget_tokens: 1024 },
    });
    expect(requestShapeSha256(body('2026-09-19'))).toBe(requestShapeSha256(body('2030-01-02')));
    expect(requestShapeSha256(body('2026-09-19', 'changed')))
      .not.toBe(requestShapeSha256(body('2030-01-02')));
    expect(requestShapeSha256(JSON.stringify({
      messages: [{ content: [] }, { content: [{
        text: "# currentDate\nToday's date is 2026-09-19.",
      }] }],
    }))).not.toBe(requestShapeSha256(JSON.stringify({
      messages: [{ content: [] }, { content: [{
        text: "# currentDate\nToday's date is 2030-01-02.",
      }] }],
    })));
  });
});

const identity = {
  device_id: 'a'.repeat(64), account_uuid: '',
  session_id: '00000000-0000-4000-8000-000000000002',
};

describe('initial SDK budget reminder', () => {
  const reminder = (total: string, remaining = total, spent = '0') =>
    `<system-reminder>\nUSD budget: $${spent}/$${total}; $${remaining} remaining\n</system-reminder>\n`;
  const request = (text: string, max_tokens = 32000) => JSON.stringify({
    messages: [{ content: [{ text }] }], max_tokens,
  });
  it('accepts the configured budget without changing provider token limits', () => {
    expect(requestShapeSha256(request(reminder('0.01')))).toBe(requestShapeSha256(request(reminder('1'))));
    expect(requestShapeSha256(request(reminder('1'), 1))).not.toBe(requestShapeSha256(request(reminder('1'))));
  });
  it.each([reminder('1', '0.5'), reminder('1', '1', '0.5'), reminder('0'), 'USD budget: $0/$1; $1 remaining']) (
    'retains an unrecognized budget context %j', (text) => {
      expect(requestShapeSha256(request(text))).not.toBe(requestShapeSha256(request(reminder('1'))));
    },
  );
});
const body = (changes = {}, request = {}) => JSON.stringify({
  metadata: { user_id: JSON.stringify({ ...identity, ...changes }) },
  messages: [{ content: [{ text: "# currentDate\nToday's date is 2026-09-20." }] }],
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

it.each([{ max_tokens: 1 }, { tools: [{ name: 'Bash' }] }, { model: 'other-model' }])(
  'retains provider request changes: %j', (change) => {
    expect(requestShapeSha256(body())).not.toBe(requestShapeSha256(body({}, change)));
  },
);
