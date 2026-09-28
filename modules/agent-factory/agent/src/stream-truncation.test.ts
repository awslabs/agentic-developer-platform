import { isTruncatedStreamResult, TRUNCATED_STREAM_MARKERS } from './stream-truncation';

describe('isTruncatedStreamResult (#4450 false-completion guard)', () => {
  it('detects the exact SDK connection-drop sentinel', () => {
    const result =
      'API Error: Connection closed mid-response. The response above may be incomplete.';
    expect(isTruncatedStreamResult(result)).toBe(true);
  });

  it('detects the sentinel embedded in a longer final response', () => {
    const result = [
      'I made the following changes to the dispatch driver...',
      '',
      'API Error: Connection closed mid-response. The response above may be incomplete.',
    ].join('\n');
    expect(isTruncatedStreamResult(result)).toBe(true);
  });

  it('detects when only the "incomplete" marker is present', () => {
    expect(isTruncatedStreamResult('...The response above may be incomplete')).toBe(true);
  });

  it('does not flag a normal successful completion summary', () => {
    const result =
      '## Summary\nAdded test_ops_dispatch_bounds.py with six asserts and wired script-tests.yml. All checks green.';
    expect(isTruncatedStreamResult(result)).toBe(false);
  });

  it('is safe on empty / null / undefined input', () => {
    expect(isTruncatedStreamResult('')).toBe(false);
    expect(isTruncatedStreamResult(null)).toBe(false);
    expect(isTruncatedStreamResult(undefined)).toBe(false);
  });

  it('exposes the markers it matches on', () => {
    expect(TRUNCATED_STREAM_MARKERS.length).toBeGreaterThan(0);
    // Every declared marker must actually be detected.
    for (const marker of TRUNCATED_STREAM_MARKERS) {
      expect(isTruncatedStreamResult(`prefix ${marker} suffix`)).toBe(true);
    }
  });
});
