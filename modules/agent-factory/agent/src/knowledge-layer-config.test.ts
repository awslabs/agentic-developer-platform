/**
 * Unit tests for Knowledge Layer MCP configuration.
 * Issue #1592.
 */
import {
  KNOWLEDGE_LAYER_ENABLED,
  buildKnowledgeLayerHeaders,
  getKnowledgeLayerMcpConfig,
  KNOWLEDGE_LAYER_SERVER_NAME,
  KNOWLEDGE_LAYER_TOOLS,
  KNOWLEDGE_LAYER_PROMPT,
} from './knowledge-layer-config';

describe('knowledge-layer-config', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    // Reset process.env to a clean state before each test.
    jest.resetModules();
    process.env = { ...originalEnv };
  });

  afterAll(() => {
    process.env = originalEnv;
  });

  describe('KNOWLEDGE_LAYER_ENABLED', () => {
    it('defaults to false when env var is not set', () => {
      // The module-level const is evaluated at import time with the test env's
      // KNOWLEDGE_LAYER_ENABLED (unset → defaults to '0' → false).
      expect(KNOWLEDGE_LAYER_ENABLED).toBe(false);
    });
  });

  describe('buildKnowledgeLayerHeaders', () => {
    it('returns empty object when no identity env vars are set', () => {
      delete process.env.ADP_GITHUB_LOGIN;
      delete process.env.ADP_GITHUB_TEAMS;
      delete process.env.ADP_OWNER_SUB;
      delete process.env.ADP_TENANT_ID;

      const headers = buildKnowledgeLayerHeaders();
      expect(headers).toEqual({});
    });

    it('never passes dispatch identity to the Door, even when configured', () => {
      process.env.ADP_GITHUB_LOGIN = 'victim';
      process.env.ADP_GITHUB_TEAMS = 'team-a';
      process.env.ADP_OWNER_SUB = 'victim-sub';
      process.env.ADP_TENANT_ID = 'victim-tenant';
      expect(buildKnowledgeLayerHeaders()).toEqual({});
    });

    it('does not include empty-string env vars', () => {
      process.env.ADP_GITHUB_LOGIN = '';

      const headers = buildKnowledgeLayerHeaders();
      expect(headers).not.toHaveProperty('X-GitHub-Login');
    });
  });

  describe('getKnowledgeLayerMcpConfig', () => {
    it('returns http type config with default URL when CONTEXT_MCP_SERVER_URL is not set', () => {
      delete process.env.CONTEXT_MCP_SERVER_URL;
      // Re-import to pick up the env change for URL (module-level const).
      // Since URL is computed at module load time, we test the default behavior.
      const config = getKnowledgeLayerMcpConfig();
      expect(config.type).toBe('http');
      expect(config.url).toContain('/mcp/');
      expect(typeof config.headers).toBe('object');
    });

    it('uses the gateway bridge without mutable identity headers', () => {
      process.env.ADP_GITHUB_LOGIN = 'myuser';
      process.env.ADP_OWNER_SUB = 'mysub';

      const config = getKnowledgeLayerMcpConfig();
      expect(config.headers).toEqual({});
      expect(config.url).toContain('/__run/knowledge/mcp/');
    });

    it('has correct type shape for McpHttpServerConfig', () => {
      const config = getKnowledgeLayerMcpConfig();
      expect(config).toHaveProperty('type', 'http');
      expect(config).toHaveProperty('url');
      expect(config).toHaveProperty('headers');
      expect(typeof config.url).toBe('string');
      expect(config.url.endsWith('/mcp/')).toBe(true);
    });
  });

  describe('KNOWLEDGE_LAYER_SERVER_NAME', () => {
    it('is knowledge-layer', () => {
      expect(KNOWLEDGE_LAYER_SERVER_NAME).toBe('knowledge-layer');
    });
  });

  describe('KNOWLEDGE_LAYER_TOOLS', () => {
    it('contains all 6 Door tools with correct prefix', () => {
      expect(KNOWLEDGE_LAYER_TOOLS).toHaveLength(6);
      for (const tool of KNOWLEDGE_LAYER_TOOLS) {
        expect(tool).toMatch(/^mcp__knowledge-layer__/);
      }
    });

    it('includes search, understand, impact, browse, remember, experience', () => {
      const toolNames = KNOWLEDGE_LAYER_TOOLS.map(t => t.replace('mcp__knowledge-layer__', ''));
      expect(toolNames).toContain('search');
      expect(toolNames).toContain('understand');
      expect(toolNames).toContain('impact');
      expect(toolNames).toContain('browse');
      expect(toolNames).toContain('remember');
      expect(toolNames).toContain('experience');
    });
  });

  describe('KNOWLEDGE_LAYER_PROMPT', () => {
    it('contains knowledge-layer XML tags', () => {
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('<knowledge-layer>');
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('</knowledge-layer>');
    });

    it('mentions all 6 tools', () => {
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('search');
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('understand');
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('impact');
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('browse');
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('remember');
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('experience');
    });

    it('warns about structural targets for understand', () => {
      expect(KNOWLEDGE_LAYER_PROMPT).toContain('STRUCTURAL targets');
    });
  });
});
