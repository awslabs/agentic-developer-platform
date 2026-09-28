/**
 * BedrockSummarizer — 3-level escalation summarizer using Bedrock.
 *
 * Levels:
 *   1. "normal" — standard summary with preservation rules
 *   2. "aggressive" — more aggressive compression
 *   3. "truncate" — deterministic truncation (no LLM call)
 */
import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { Summarizer } from './port';

const PRESERVATION_RULES = `
IMPORTANT PRESERVATION RULES:
- Preserve artifact IDs (art_*) and pre-signed URLs verbatim - never paraphrase them.
- Preserve tool-call structure: summarize intent, keep identifiers (file paths, command names) verbatim.
- Preserve error strings and exact command outputs when they establish state.
- Keep timestamps for key events.
`;

export class BedrockSummarizer implements Summarizer {
  private readonly client: BedrockRuntimeClient;

  constructor(
    private readonly model: string = 'global.anthropic.claude-sonnet-4-6',
    region: string = 'us-east-1',
  ) {
    this.client = new BedrockRuntimeClient({ region });
  }

  async summarize(input: {
    text: string;
    mode: 'normal' | 'aggressive' | 'truncate';
    previousSummary?: string;
    targetTokens: number;
  }): Promise<string> {
    if (input.mode === 'truncate') {
      return this.deterministicTruncate(input.text, input.targetTokens);
    }

    const systemPrompt = this.buildSystemPrompt(input.mode, input.targetTokens);
    const userContent = input.previousSummary
      ? `Previous summary:\n${input.previousSummary}\n\nNew content to incorporate:\n${input.text}`
      : input.text;

    try {
      const response = await this.client.send(
        new InvokeModelCommand({
          modelId: this.model,
          contentType: 'application/json',
          accept: 'application/json',
          body: JSON.stringify({
            anthropic_version: 'bedrock-2023-05-31',
            max_tokens: input.targetTokens * 5, // generous buffer for token estimation
            system: systemPrompt,
            messages: [{ role: 'user', content: userContent }],
          }),
        }),
      );

      const body = JSON.parse(new TextDecoder().decode(response.body));
      const text = body.content?.[0]?.text ?? '';
      return text.trim();
    } catch (err) {
      console.error(`[summarizer] Bedrock call failed: ${(err as Error).message}`);
      // Fall back to truncation on any error
      return this.deterministicTruncate(input.text, input.targetTokens);
    }
  }

  private buildSystemPrompt(mode: 'normal' | 'aggressive', targetTokens: number): string {
    const modeInstructions =
      mode === 'normal'
        ? `Summarize the following conversation excerpt. Target approximately ${targetTokens} tokens.
Capture the key decisions, outcomes, artifacts produced, and any open questions.`
        : `Aggressively compress the following conversation excerpt to approximately ${targetTokens} tokens.
Keep ONLY: final decisions, artifact IDs, error states, and critical path items.
Remove all pleasantries, reasoning chains, and intermediate steps.`;

    return `${modeInstructions}

${PRESERVATION_RULES}

Return ONLY the summary text, no preamble or explanation.`;
  }

  private deterministicTruncate(text: string, targetTokens: number): string {
    const targetChars = targetTokens * 4;
    if (text.length <= targetChars) return text;

    const halfTarget = Math.floor(targetChars / 2);
    const head = text.slice(0, halfTarget);
    const tail = text.slice(-halfTarget);
    return `${head}\n\n[... ${text.length - targetChars} chars truncated ...]\n\n${tail}`;
  }
}
