import { z } from 'zod';
import { ChatDataError, type ChatDataClient } from './gateway/chat-data-client';
import { modelOutputBlock, type ChatModelRequest } from './gateway/chat-model-contract';
import { CrewHostModelRouteError, invokeCrewHostAwsCredentials, invokeCrewHostGithubToken, invokeCrewHostModel, invokeCrewHostModelStream, invokeCrewHostOrchestration, invokeCrewHostResponses, invokeCrewHostTools } from './crew-host-model-route';

const replySchema = z.object({
  content: z.array(modelOutputBlock).max(64),
  stop_reason: z.enum(['end_turn', 'max_tokens', 'stop_sequence', 'tool_use']),
  usage: z.object({ input_tokens: z.number().int().nonnegative(), output_tokens: z.number().int().nonnegative() }).passthrough(),
}).passthrough();

type Route = Parameters<typeof invokeCrewHostModel>[0];

/** A local route failure as the SDK-facing `unavailable`, keeping the route's own message for the journal. */
function unavailable(cause: unknown): ChatDataError {
  return Object.assign(new ChatDataError('unavailable'), { cause });
}

function routeFailure(error: CrewHostModelRouteError): ChatDataError {
  return new ChatDataError(error.status === 409 ? 'scope_mismatch' : error.status === 429 ? 'rate_limited'
    : error.status >= 500 ? 'unavailable' : 'denied', error.status);
}

export function crewHostModelClient(route: Route, signal?: AbortSignal): ChatDataClient {
  const client = {
    withSignal: (next: AbortSignal) => crewHostModelClient(route, next),
    // #8380: the adapter's `/tools*` branch hands the shim's request here; it rides the same
    // signed host identity as a model call and is refused with it.
    requestTools: async (method: 'GET' | 'POST', path: string, body?: unknown) => {
      signal?.throwIfAborted();
      if (!/^\/tools(?:[/?]|$)/.test(path)) throw new ChatDataError('invalid_request');
      try {
        const reply = await invokeCrewHostTools(route, method, path, body, signal);
        signal?.throwIfAborted();
        return reply;
      } catch (error) {
        if (error instanceof ChatDataError) throw error;
        if (error instanceof CrewHostModelRouteError) throw routeFailure(error);
        throw unavailable(error);
      }
    },
    requestOrchestration: async (method: 'GET' | 'POST', path: string, body?: unknown) => {
      signal?.throwIfAborted();
      try {
        return await invokeCrewHostOrchestration(route, method, path, body, signal);
      } catch (error) {
        if (error instanceof CrewHostModelRouteError) throw routeFailure(error);
        throw unavailable(error);
      }
    },
    requestAwsCredentials: async (input: unknown) => {
      signal?.throwIfAborted();
      try {
        return await invokeCrewHostAwsCredentials(route, input, signal);
      } catch (error) {
        if (error instanceof CrewHostModelRouteError) throw routeFailure(error);
        throw unavailable(error);
      }
    },
    // #8555: the box's `gh`/`git` ask the adapter for the owner's GitHub token; it rides the same
    // signed host identity. The gateway's refusal status is kept so the adapter can name the step.
    requestGithubToken: async () => {
      signal?.throwIfAborted();
      try {
        const reply = await invokeCrewHostGithubToken(route, signal);
        signal?.throwIfAborted();
        return reply;
      } catch (error) {
        if (error instanceof ChatDataError) throw error;
        if (error instanceof CrewHostModelRouteError) throw routeFailure(error);
        throw unavailable(error);
      }
    },
    // The trailing `model` names the owner's per-call choice (Claude Code `/model`, Codex `-c model=`);
    // absent, the run's model. The sandbox client has no such parameter and ignores it.
    // Codex on GPT models (dev 2026-10-26): the Responses body goes to the gateway's OpenAI passthrough
    // untouched; the caller relays the reply bytes. Refusals keep the gateway status.
    requestOpenAIResponses: async (body: unknown, abort?: AbortSignal) => {
      signal?.throwIfAborted();
      try {
        return await invokeCrewHostResponses(route, body, abort ? AbortSignal.any([abort, ...(signal ? [signal] : [])]) : signal);
      } catch (error) {
        if (error instanceof ChatDataError) throw error;
        if (error instanceof CrewHostModelRouteError) throw routeFailure(error);
        throw unavailable(error);
      }
    },
    invokeModel: async (_operation: string, request: ChatModelRequest, _onText?: unknown, _deliver?: boolean, _evidence?: unknown, model?: string) => {
      signal?.throwIfAborted();
      try {
        const reply = replySchema.parse(await invokeCrewHostModel(route, {
          anthropic_version: 'bedrock-2023-05-31', ...request,
        }, signal, model));
        signal?.throwIfAborted();
        if ((reply.stop_reason === 'tool_use') !== reply.content.some(block => block.type === 'tool_use') ||
            reply.usage.output_tokens > request.max_tokens) throw new ChatDataError('invalid_response');
        return { modelId: model ?? route.modelId, stopReason: reply.stop_reason, content: reply.content,
          usage: { input_tokens: reply.usage.input_tokens, output_tokens: reply.usage.output_tokens } };
      } catch (error) {
        if (error instanceof ChatDataError) throw error;
        if (error instanceof CrewHostModelRouteError) throw routeFailure(error);
        throw unavailable(error);
      }
    },
    // Streaming twin: the gateway's SSE events are relayed as they arrive; a refusal before the first
    // byte surfaces on the first `next()` with the same ChatDataError mapping as a buffered call.
    invokeModelStream: async function* (_operation: string, request: ChatModelRequest, abort?: AbortSignal, model?: string) {
      signal?.throwIfAborted();
      const bounded = AbortSignal.any([...(signal ? [signal] : []), ...(abort ? [abort] : [])]);
      try {
        for await (const event of invokeCrewHostModelStream(route, { anthropic_version: 'bedrock-2023-05-31', ...request }, bounded, model)) {
          signal?.throwIfAborted();
          yield event;
        }
      } catch (error) {
        if (error instanceof ChatDataError) throw error;
        if (error instanceof CrewHostModelRouteError) throw routeFailure(error);
        throw unavailable(error);
      }
    },
  };
  return client as unknown as ChatDataClient;
}
