/**
 * Memory tool scope-isolation tests (#4074, sub-EPIC #4068 · D, finding #6).
 *
 * The memory tools used to accept `scope.user` / `scope.tenant` as free,
 * model-controlled fields and forward them verbatim into
 * `DynamoMemoryProvider.buildScopeKeys`, which turns them into the DynamoDB
 * partition key (`scope#user#<v>` / `scope#tenant#<v>`). A chat user could
 * therefore ask the agent to read or write another user's or tenant's memory.
 *
 * The fix closure-injects the authenticated scope (mirroring
 * `vault/tools.ts`, which does the same for credentials). These tests assert
 * the OUTCOME — the scope that actually reaches `provider.save` /
 * `provider.retrieve`, i.e. the value that becomes the partition key — not the
 * plumbing that gets it there.
 */
import { createMemoryTools } from './tools';
import { MemoryCapabilities, MemoryProvider, MemoryQuery, MemoryRecord, MemoryToolScope } from './types';
import { AgentTool } from '../context/types';

/** The authenticated session scope — what the closure supplies. */
const SESSION_SCOPE: MemoryToolScope = { user: 'alice', tenant: 'acme', persona: 'developer' };

/** What an attacker would try to make the model emit. */
const ATTACKER_SCOPE = { user: 'victim', tenant: 'other-tenant', component: 'evil', persona: 'admin' };

interface Captured {
  saves: Array<Omit<MemoryRecord, 'id' | 'createdAt'>>;
  retrieves: MemoryQuery[];
}

function mockProvider(): { provider: MemoryProvider; captured: Captured } {
  const captured: Captured = { saves: [], retrieves: [] };
  const provider: MemoryProvider = {
    async retrieve(input: MemoryQuery): Promise<MemoryRecord[]> {
      captured.retrieves.push(input);
      return [];
    },
    async save(record: Omit<MemoryRecord, 'id' | 'createdAt'>): Promise<MemoryRecord> {
      captured.saves.push(record);
      return { ...record, id: 'mem_test_1', createdAt: '2026-08-24T00:00:00.000Z' };
    },
    tools(): AgentTool[] {
      return [];
    },
    capabilities(): MemoryCapabilities {
      return {
        semanticSearch: false,
        keywordSearch: true,
        tagFiltering: false,
        scoping: ['user', 'component', 'persona', 'tenant'],
        delete: false,
        asyncExtraction: false,
        ttl: false,
      };
    },
  };
  return { provider, captured };
}

function toolNamed(tools: AgentTool[], name: string): AgentTool {
  const tool = tools.find(t => t.name === name);
  if (!tool) throw new Error(`tool ${name} not registered`);
  return tool;
}

describe('memory tools — scope is closure-injected, never model-controlled (#4074 #6)', () => {
  describe('T1: save_fact ignores attacker-supplied scope', () => {
    it('writes to the SESSION partition key, not the attacker-supplied one', async () => {
      const { provider, captured } = mockProvider();
      const tools = createMemoryTools(provider, SESSION_SCOPE);

      await toolNamed(tools, 'save_fact').handler({
        content: 'the deploy key rotates monthly',
        // Attacker input: the model was steered into emitting another tenant's scope.
        scope: ATTACKER_SCOPE,
      });

      expect(captured.saves).toHaveLength(1);
      const scope = captured.saves[0].scope;
      // The denial: the write landed in alice/acme, not victim/other-tenant.
      expect(scope.user).toBe('alice');
      expect(scope.tenant).toBe('acme');
      expect(scope.user).not.toBe('victim');
      expect(scope.tenant).not.toBe('other-tenant');
      // No attacker-chosen dimension leaked into the partition key at all.
      expect(scope.component).toBeUndefined();
      expect(scope.persona).toBe('developer');
    });

    it('holds regardless of how the attacker varies the input shape', async () => {
      const variants: unknown[] = [
        { user: 'victim' },
        { tenant: 'other-tenant' },
        { user: 'victim', tenant: 'other-tenant', component: 'x', persona: 'y' },
        {},
        undefined,
        'not-an-object',
        null,
      ];

      for (const variant of variants) {
        const { provider, captured } = mockProvider();
        const tools = createMemoryTools(provider, SESSION_SCOPE);
        await toolNamed(tools, 'save_fact').handler({ content: 'c', scope: variant });

        expect(captured.saves[0].scope).toEqual(SESSION_SCOPE);
      }
    });
  });

  describe('T2: recall_memory (read side) and save_learning', () => {
    it('recall_memory queries the SESSION scope, so cross-tenant reads are impossible', async () => {
      const { provider, captured } = mockProvider();
      const tools = createMemoryTools(provider, SESSION_SCOPE);

      await toolNamed(tools, 'recall_memory').handler({
        query: 'deploy keys',
        scope: ATTACKER_SCOPE,
      });

      expect(captured.retrieves).toHaveLength(1);
      const scope = captured.retrieves[0].scope;
      expect(scope).toBeDefined();
      expect(scope!.user).toBe('alice');
      expect(scope!.tenant).toBe('acme');
      expect(scope!.user).not.toBe('victim');
      expect(scope!.tenant).not.toBe('other-tenant');
    });

    it('save_learning writes to the SESSION scope', async () => {
      const { provider, captured } = mockProvider();
      const tools = createMemoryTools(provider, SESSION_SCOPE);

      await toolNamed(tools, 'save_learning').handler({
        content: 'always run npm ci --include=dev',
        scope: ATTACKER_SCOPE,
      });

      expect(captured.saves[0].scope).toEqual(SESSION_SCOPE);
      expect(captured.saves[0].kind).toBe('learning');
    });
  });

  describe('T2b: save_preference ignores an attacker-supplied user', () => {
    it('saves the preference against the SESSION user', async () => {
      const { provider, captured } = mockProvider();
      const tools = createMemoryTools(provider, SESSION_SCOPE);

      await toolNamed(tools, 'save_preference').handler({
        content: 'prefers terse answers',
        // `user` used to be a required, top-level, model-supplied string.
        user: 'victim',
      });

      expect(captured.saves).toHaveLength(1);
      expect(captured.saves[0].scope.user).toBe('alice');
      expect(captured.saves[0].scope.user).not.toBe('victim');
      expect(captured.saves[0].kind).toBe('preference');
    });
  });

  describe('T3: identity fields are absent from the model-visible schema', () => {
    const IDENTITY_FIELDS = ['user', 'tenant', 'component', 'persona'];

    it('no tool lets the model express a scope dimension at all', () => {
      const { provider } = mockProvider();
      const tools = createMemoryTools(provider, SESSION_SCOPE);

      expect(tools.map(t => t.name).sort()).toEqual([
        'recall_memory',
        'save_fact',
        'save_learning',
        'save_preference',
      ]);

      for (const tool of tools) {
        const keys = Object.keys(tool.inputSchema);
        // No `scope` object...
        expect(keys).not.toContain('scope');
        // ...and no identity field hoisted to the top level either.
        for (const field of IDENTITY_FIELDS) {
          expect(keys).not.toContain(field);
        }
      }
    });

    it('the legitimate parameters are still exposed', () => {
      const { provider } = mockProvider();
      const tools = createMemoryTools(provider, SESSION_SCOPE);

      expect(Object.keys(toolNamed(tools, 'recall_memory').inputSchema).sort()).toEqual(['limit', 'query']);
      expect(Object.keys(toolNamed(tools, 'save_fact').inputSchema).sort()).toEqual(['content', 'kind']);
      expect(Object.keys(toolNamed(tools, 'save_preference').inputSchema)).toEqual(['content']);
      expect(Object.keys(toolNamed(tools, 'save_learning').inputSchema)).toEqual(['content']);
    });
  });

  /**
   * T5 — regression GUARD, not a gate (this passes pre-fix too).
   *
   * `response/handler.py` re-enqueues to the same FIFO queue without any
   * identity fields, so on that path `user_id` / `tenant_id` are undefined.
   * `DynamoMemoryProvider.save` THROWS when the scope has no dimensions at
   * all, so a fix that dropped `persona` from the injected scope would turn
   * every save on the re-enqueue path into a hard tool error. These assertions
   * pin the degrade-predictably behaviour.
   */
  describe('T5 (guard): degrades predictably when identity is absent', () => {
    const NO_IDENTITY: MemoryToolScope = { user: undefined, tenant: undefined, persona: 'developer' };

    it('save_fact resolves and retains the sanitized persona dimension', async () => {
      const { provider, captured } = mockProvider();
      const tools = createMemoryTools(provider, NO_IDENTITY);

      await expect(
        toolNamed(tools, 'save_fact').handler({ content: 'still recorded' }),
      ).resolves.toBeDefined();

      // persona survives, so buildScopeKeys stays non-empty and save() won't throw.
      expect(captured.saves[0].scope.persona).toBe('developer');
    });

    it('recall_memory does not throw when identity is absent', async () => {
      const { provider } = mockProvider();
      const tools = createMemoryTools(provider, NO_IDENTITY);

      await expect(
        toolNamed(tools, 'recall_memory').handler({ query: 'anything' }),
      ).resolves.toBeDefined();
    });
  });
});
