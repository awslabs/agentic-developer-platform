/**
 * Summarizer port — compresses conversation text into summaries.
 *
 * Implementations: BedrockSummarizer
 */
export interface Summarizer {
  summarize(input: {
    text: string;
    mode: 'normal' | 'aggressive' | 'truncate';
    previousSummary?: string;
    targetTokens: number;
  }): Promise<string>;
}
