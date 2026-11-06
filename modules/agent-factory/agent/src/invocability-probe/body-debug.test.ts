import { canonicalJson, normalizeRequestShape } from './canonical-json';
import { PROBE_BODY_CHUNK_CHARS, printCanonicalProbeBody, probeBodyPrintEnabled } from './body-debug';

describe('bounded probe body diagnostics', () => {
  it('is off unless ADP_PROBE_PRINT_BODY is exactly "1"', () => {
    expect(probeBodyPrintEnabled({})).toBe(false);
    expect(probeBodyPrintEnabled({ ADP_PROBE_PRINT_BODY: 'true' })).toBe(false);
    expect(probeBodyPrintEnabled({ ADP_PROBE_PRINT_BODY: '1' })).toBe(true);
  });

  it('prints the normalized canonical body in reassemblable log-safe chunks', () => {
    const body = {
      messages: [{ role: 'user', content: [
        { text: '<system-reminder>\nUSD budget: $0/$0.5; $0.5 remaining\n</system-reminder>' },
        { text: 'x'.repeat(PROBE_BODY_CHUNK_CHARS * 2) },
      ] }],
      tools: [{ name: 'Read' }],
    };
    const lines: string[] = [];
    printCanonicalProbeBody('invocability-probe:body:model', body, (line) => lines.push(line));
    expect(lines).toHaveLength(3);
    expect(lines.map((line) => line.split(' ')[0])).toEqual([
      '[invocability-probe:body:model:1/3]', '[invocability-probe:body:model:2/3]', '[invocability-probe:body:model:3/3]',
    ]);
    const reassembled = lines.map((line) => line.slice(line.indexOf('] ') + 2)).join('');
    expect(reassembled).toBe(canonicalJson(normalizeRequestShape(body)));
    expect(reassembled).toContain('<ADP_PROBE_BUDGET>');
    expect(reassembled).not.toContain('0.5');
  });
});
