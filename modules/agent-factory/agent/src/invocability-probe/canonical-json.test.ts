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
