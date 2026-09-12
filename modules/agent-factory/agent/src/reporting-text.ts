/** Only intentional assistant text belongs in human-facing explanations. */
export function assistantText(content: ReadonlyArray<{ type?: unknown; text?: unknown }>): string {
  return content
    .filter(block => (block.type === undefined || block.type === 'text') && typeof block.text === 'string')
    .map(block => (block.text as string).trim())
    .filter(Boolean)
    .join('\n\n');
}

/** Bound a display by UTF-8 bytes, keeping complete characters and a visible notice. */
export function truncateUtf8(text: string, maxBytes: number, notice: string): string {
  const bytes = Buffer.from(text, 'utf8');
  if (bytes.length <= maxBytes) return text;
  const budget = maxBytes - Buffer.byteLength(notice, 'utf8');
  if (budget < 0) throw new RangeError('Truncation notice exceeds the display budget');
  let end = budget;
  while (end > 0 && (bytes[end] & 0xc0) === 0x80) end--;
  return bytes.subarray(0, end).toString('utf8') + notice;
}
