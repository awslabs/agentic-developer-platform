import { assistantText, truncateUtf8 } from './reporting-text';

it('collects all authored text blocks without publishing private reasoning or tool payloads', () => {
  expect(assistantText([
    { type: 'thinking', text: 'PRIVATE' },
    { type: 'text', text: 'The gateway forwards chunks.' },
    { type: 'tool_result', text: 'RAW OUTPUT' },
    { type: 'text', text: '  ' },
    { type: 'text', text: 'An early event proves incremental delivery.' },
  ])).toBe('The gateway forwards chunks.\n\nAn early event proves incremental delivery.');
});

it('retains typeless text from legacy reporting callers', () => {
  expect(assistantText([{ text: 'Explanation' }])).toBe('Explanation');
});

it('bounds Unicode displays without splitting characters and discloses clipping', () => {
  const clipped = truncateUtf8('A🌍界'.repeat(100), 127, '\n[shortened]');
  expect(Buffer.byteLength(clipped)).toBeLessThanOrEqual(127);
  expect(clipped).not.toContain('\ufffd');
  expect(clipped.endsWith('\n[shortened]')).toBe(true);
  expect(truncateUtf8('An intact explanation.', 127, '[shortened]')).toBe('An intact explanation.');
});
