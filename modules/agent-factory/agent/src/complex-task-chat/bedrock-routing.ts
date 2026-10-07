import type { TaskPayload } from './sqs-client';

export async function withChatBedrockRouting<T>(_task: TaskPayload, _run: () => Promise<T>, _rawEnvelope?: string): Promise<T> {
  throw new Error('Credentialed chat model routing retired; restricted sandbox required');
}
