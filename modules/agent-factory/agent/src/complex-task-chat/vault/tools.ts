/**
 * Vault MCP Tools — agent-facing credential tools for the chat-agent flow.
 *
 * Four tools:
 *   1. list_user_credentials — discover available credentials (no params)
 *   2. http_request_with_credential — proxy HTTP via vault (cred never enters process)
 *   3. materialize_user_credential — write file-type cred to per-task tmpfs
 *   4. get_user_credential_raw — escape hatch (auto-registers with scrubber)
 *
 * All tools closure-inject userId/agentId/taskId from the task payload so the
 * agent cannot read another user's credentials by manipulating tool input.
 *
 * Gated by ENABLE_USER_CREDENTIALS env var AND userId presence in task payload.
 *
 * Issue #137: Vault Phase 4
 */

import { z } from 'zod';
import { AgentTool, AgentToolResult } from '../context/types';
import { Scrubber } from '../context/scrubber';
import { VaultGatewayClient } from './gateway-client';

export interface VaultToolsConfig {
  userId: string;
  agentId: string;
  taskId: string;
  invocationId?: string;
  scrubber: Scrubber;
  client: VaultGatewayClient;
}

/**
 * Extended AgentTool that includes an inputSummarySanitizer for AG-UI event
 * sanitization. The orchestrator uses this to strip credential-bearing fields
 * from TOOL_CALL_ARGS events.
 */
export interface SanitizableAgentTool extends AgentTool {
  inputSummarySanitizer?: (input: Record<string, unknown>) => Record<string, unknown>;
}

/**
 * Factory function matching the `artifacts.toolsForTurn(...)` pattern.
 * Returns vault tools closed over the current task's identity.
 */
export function vaultToolsForTurn(config: VaultToolsConfig): SanitizableAgentTool[] {
  const { userId, agentId, taskId, invocationId, scrubber, client } = config;

  return [
    // 1. list_user_credentials
    {
      name: 'list_user_credentials',
      description:
        'List the services and labels the current user has stored credentials for. ' +
        'Returns metadata only — never values. Use this at the start of a turn to ' +
        'decide which services are available before calling other vault tools.',
      inputSchema: {},
      inputSummarySanitizer: (input) => input,
      handler: async (): Promise<AgentToolResult> => {
        try {
          const credentials = await client.listCredentials(userId, invocationId);
          return text(JSON.stringify({ credentials }, null, 2));
        } catch (err) {
          return error(`Failed to list credentials: ${(err as Error).message}`);
        }
      },
    },

    // 2. http_request_with_credential
    {
      name: 'http_request_with_credential',
      description:
        'Call an HTTP endpoint using a credential stored in the user\'s vault. ' +
        'The raw credential never enters this process — the gateway injects it into ' +
        'the outbound request.',
      inputSchema: {
        service: z.string().describe('Service name, e.g. "github", "jira"'),
        label: z.string().optional().describe('Optional credential label; defaults to primary'),
        method: z.enum(['GET', 'POST', 'PATCH', 'DELETE', 'PUT']).describe('HTTP method'),
        url: z.string().describe('Target URL'),
        headers: z.record(z.string(), z.string()).optional().describe('Additional HTTP headers'),
        body: z.string().optional().describe('Request body (string)'),
      },
      inputSummarySanitizer: (input) => ({
        service: input.service,
        label: input.label,
        method: input.method,
        url: input.url,
        // NEVER include body or headers (may contain Authorization)
      }),
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        try {
          const resp = await client.proxyRequest({
            user_id: userId,
            agent_id: agentId,
            task_id: taskId,
            service: input.service as string,
            label: input.label as string | undefined,
            method: input.method as string,
            url: input.url as string,
            headers: input.headers as Record<string, string> | undefined,
            body: input.body as string | undefined,
            invocation_id: invocationId,
          });
          return text(JSON.stringify({
            status: resp.status,
            headers: resp.headers,
            body: resp.body,
            provenance_id: resp.provenance_id,
          }, null, 2));
        } catch (err) {
          return error(`Proxy request failed: ${(err as Error).message}`);
        }
      },
    },

    // 3. materialize_user_credential
    {
      name: 'materialize_user_credential',
      description:
        'Materialize a file-type credential (ssh_key, certificate, config_file) to a ' +
        'task-scoped tmpfs path. Returns a presigned URL the agent can fetch to get the file content. ' +
        'The presigned URL is short-lived (5 minutes).',
      inputSchema: {
        service: z.string().describe('Service name'),
        label: z.string().optional().describe('Optional credential label'),
      },
      inputSummarySanitizer: (input) => input,
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        try {
          const resp = await client.materialize({
            user_id: userId,
            agent_id: agentId,
            task_id: taskId,
            service: input.service as string,
            label: input.label as string | undefined,
            invocation_id: invocationId,
          });
          return text(JSON.stringify({
            materialize_url: resp.materialize_url,
            expires_at: resp.expires_at,
            provenance_id: resp.provenance_id,
          }, null, 2));
        } catch (err) {
          return error(`Materialize failed: ${(err as Error).message}`);
        }
      },
    },

    // 4. assume_user_aws_role
    {
      name: 'assume_user_aws_role',
      description:
        'Assume an AWS role stored in the user\'s vault. Returns short-lived temp ' +
        'credentials written to an AWS profile; subsequent aws/terraform/kubectl calls ' +
        'can use --profile <name>. The raw credentials are registered with the scrubber ' +
        'and NOT returned in the tool output — only profile_name, expiration, and region.',
      inputSchema: {
        service: z.string().optional().default('aws').describe('Service name, typically "aws"'),
        label: z.string().optional().describe('Credential label, e.g. "prod", "staging"'),
        purpose: z.string().optional().describe('Free-text reason for audit'),
      },
      inputSummarySanitizer: (input) => input,
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        try {
          const resp = await client.assumeRole({
            user_id: userId,
            agent_id: agentId,
            task_id: taskId,
            service: (input.service as string) || 'aws',
            label: input.label as string | undefined,
            purpose: input.purpose as string | undefined,
            invocation_id: invocationId,
          });
          // CRITICAL: register sensitive values with scrubber BEFORE returning.
          const labelStr = (input.label as string) ?? 'default';
          scrubber.registerSensitiveValue(
            resp.secret_access_key,
            `<<redacted:aws:${labelStr}:secret>>`,
          );
          scrubber.registerSensitiveValue(
            resp.session_token,
            `<<redacted:aws:${labelStr}:session>>`,
          );
          // Return only safe metadata — raw creds stay out of agent memory.
          return text(JSON.stringify({
            profile_name: resp.profile_name,
            expiration: resp.expiration,
            region: resp.region,
          }, null, 2));
        } catch (err) {
          return error(`Assume role failed: ${(err as Error).message}`);
        }
      },
    },

    // 5. get_user_credential_raw
    {
      name: 'get_user_credential_raw',
      description:
        'Return a raw credential value. Use only when proxying or materializing won\'t work. ' +
        'Every call is audited; the returned value is automatically redacted from chat ' +
        'history and memory writes.',
      inputSchema: {
        service: z.string().describe('Service name'),
        label: z.string().optional().describe('Optional credential label'),
        purpose: z.string().optional().describe('Why the raw value is needed (for audit)'),
      },
      inputSummarySanitizer: (input) => ({
        service: input.service,
        label: input.label,
        purpose: input.purpose,
        // value is NEVER included in summary
      }),
      handler: async (input: Record<string, unknown>): Promise<AgentToolResult> => {
        try {
          const resp = await client.rawRead({
            user_id: userId,
            agent_id: agentId,
            task_id: taskId,
            service: input.service as string,
            label: input.label as string | undefined,
            purpose: input.purpose as string | undefined,
            invocation_id: invocationId,
          });
          // CRITICAL: register with scrubber BEFORE returning
          const replacement = `<<redacted:${input.service as string}:${(input.label as string) ?? 'default'}>>`;
          scrubber.registerSensitiveValue(resp.value, replacement);
          return text(JSON.stringify({
            value: resp.value,
            credential_type: resp.credential_type,
            provenance_id: resp.provenance_id,
          }, null, 2));
        } catch (err) {
          return error(`Raw read failed: ${(err as Error).message}`);
        }
      },
    },
  ];
}

function text(s: string): AgentToolResult {
  return { content: [{ type: 'text', text: s }] };
}

function error(s: string): AgentToolResult {
  return { content: [{ type: 'text', text: s }], isError: true };
}
