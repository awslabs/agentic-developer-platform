import { formatSummaryXml, parseSessionFromSummaryId } from './summary-format';

describe('formatSummaryXml', () => {
  it('renders correct XML structure', () => {
    const xml = formatSummaryXml({
      summaryId: 'sum_test123_abc',
      kind: 'leaf',
      depth: 0,
      earliestAt: '2026-04-19T09:12:00Z',
      latestAt: '2026-04-19T09:48:00Z',
      content: 'Summary of the conversation.',
    });
    expect(xml).toContain('id="sum_test123_abc"');
    expect(xml).toContain('kind="leaf"');
    expect(xml).toContain('depth="0"');
    expect(xml).toContain('Summary of the conversation.');
    expect(xml).toContain('Expand for details');
  });
});

describe('parseSessionFromSummaryId', () => {
  it('extracts session ID', () => {
    expect(parseSessionFromSummaryId('sum_mysession_abc123')).toBe('mysession');
  });

  it('handles session IDs with underscores', () => {
    expect(parseSessionFromSummaryId('sum_my_session_abc123')).toBe('my_session');
  });

  it('returns null for invalid format', () => {
    expect(parseSessionFromSummaryId('invalid')).toBeNull();
    expect(parseSessionFromSummaryId('msg_test_abc')).toBeNull();
  });
});
