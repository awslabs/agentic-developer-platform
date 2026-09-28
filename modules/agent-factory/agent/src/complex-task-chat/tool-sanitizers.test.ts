/**
 * Tests for buildToolSanitizers (#4208).
 *
 * The property under test is the one the old inline loop got wrong: the map must
 * be built from EVERY tool handed to the model, not just the vault subset.
 */
import { buildToolSanitizers } from './tool-sanitizers';
import { AgentTool } from './context/types';

function tool(name: string, sanitizer?: (i: Record<string, unknown>) => Record<string, unknown>): AgentTool {
  const t: AgentTool = {
    name,
    description: name,
    inputSchema: {},
    handler: async () => ({ content: [{ type: 'text', text: 'ok' }] }),
  };
  if (sanitizer) {
    (t as AgentTool & { inputSummarySanitizer?: typeof sanitizer }).inputSummarySanitizer = sanitizer;
  }
  return t;
}

describe('buildToolSanitizers', () => {
  it('collects sanitizers from non-vault tools too', () => {
    const strip = (i: Record<string, unknown>) => ({ ...i, secret: '[redacted]' });
    const tools = [
      tool('get_user_credential_raw', strip),
      tool('some_other_tool', strip),
      tool('update_draft'),
    ];

    const sanitizers = buildToolSanitizers(tools);

    // The regression this guards: a sanitizer on a tool outside the vault list
    // must still be registered.
    expect(sanitizers.has('some_other_tool')).toBe(true);
    expect(sanitizers.has('get_user_credential_raw')).toBe(true);
  });

  it('omits tools that declare no sanitizer', () => {
    const sanitizers = buildToolSanitizers([tool('update_draft'), tool('publish_artifact')]);
    expect(sanitizers.size).toBe(0);
  });

  it('registers the actual sanitizer function, not a stub', () => {
    const strip = (i: Record<string, unknown>) => ({ ...i, body: '[stripped]' });
    const sanitizers = buildToolSanitizers([tool('http_request_with_credential', strip)]);

    const result = sanitizers.get('http_request_with_credential')!({ url: 'u', body: 'secret' });
    expect(result).toEqual({ url: 'u', body: '[stripped]' });
  });

  it('handles an empty tool list', () => {
    expect(buildToolSanitizers([]).size).toBe(0);
  });
});
