/**
 * Tests for user attachment injection into system prompt (Stage C, #186).
 *
 * The attachment block construction is tested independently — the agent
 * module calls composeSystemPrompt() with the block appended to the base.
 */

import { composeSystemPrompt } from './persona-loader';

describe('attachment injection into system prompt (#186)', () => {
  const BASE_PROMPT = 'You are a developer.';

  function buildAttachmentBlock(attachmentIds: string[]): string {
    if (attachmentIds.length === 0) return '';
    const lines = attachmentIds.map(id => `  <attachment id="${id}" />`);
    return (
      '\n\n<user-attachments>\n' +
      'The user attached files to this message. Use the fetch_artifact tool with the artifact ID to read each file.\n' +
      lines.join('\n') + '\n' +
      '</user-attachments>'
    );
  }

  it('injects <user-attachments> block when attachments are present', () => {
    const block = buildAttachmentBlock(['art_abc123', 'art_def456']);
    const result = composeSystemPrompt({
      base: BASE_PROMPT + block,
      personaLearnings: [],
      memories: [],
    });

    expect(result).toContain('<user-attachments>');
    expect(result).toContain('art_abc123');
    expect(result).toContain('art_def456');
    expect(result).toContain('fetch_artifact');
  });

  it('does not inject attachment block when list is empty', () => {
    const block = buildAttachmentBlock([]);
    const result = composeSystemPrompt({
      base: BASE_PROMPT + block,
      personaLearnings: [],
      memories: [],
    });

    expect(result).not.toContain('<user-attachments>');
  });

  it('attachment block is placed inside <persona> tags', () => {
    const block = buildAttachmentBlock(['art_abc123']);
    const result = composeSystemPrompt({
      base: BASE_PROMPT + block,
      personaLearnings: [],
      memories: [],
    });

    // The composeSystemPrompt wraps base in <persona> tags
    const personaStart = result.indexOf('<persona>');
    const personaEnd = result.indexOf('</persona>');
    const attachStart = result.indexOf('<user-attachments>');

    expect(attachStart).toBeGreaterThan(personaStart);
    expect(attachStart).toBeLessThan(personaEnd);
  });

  it('each attachment gets its own element', () => {
    const ids = ['art_001', 'art_002', 'art_003'];
    const block = buildAttachmentBlock(ids);

    for (const id of ids) {
      expect(block).toContain(`<attachment id="${id}" />`);
    }
  });
});
