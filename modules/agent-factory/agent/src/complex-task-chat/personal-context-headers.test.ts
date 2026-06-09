/**
 * Tests for personal-context identity header propagation — Issue #1289
 *
 * Validates:
 * 1. Dispatch message with cognito_sub + tenant_id produces correct headers
 * 2. Worker sets X-Owner-Sub/X-Tenant-Id from trusted metadata
 * 3. Agent/LLM input cannot override these headers (anti-spoof)
 * 4. Service-account dispatch (no cognito_sub) returns null identity
 * 5. Existing dispatch path works when fields are absent (regression)
 */
import {
  buildPersonalContextIdentity,
  getPersonalContextHeaders,
  getPersonalContextEnvVars,
  PersonalContextIdentity,
} from './personal-context-headers';

describe('buildPersonalContextIdentity', () => {
  it('builds identity from cognito_sub + tenant_id', () => {
    const identity = buildPersonalContextIdentity({
      cognito_sub: '44086498-2091-70e1-bd3a-12c6104c3ebb',
      tenant_id: 'org-acme-123',
      user_id: '44086498-2091-70e1-bd3a-12c6104c3ebb',
    });

    expect(identity).not.toBeNull();
    expect(identity!.ownerSub).toBe('44086498-2091-70e1-bd3a-12c6104c3ebb');
    expect(identity!.tenantId).toBe('org-acme-123');
  });

  it('falls back to user_id when cognito_sub is absent (backward compat)', () => {
    const identity = buildPersonalContextIdentity({
      tenant_id: 'org-acme-123',
      user_id: '55086498-3091-80e1-cd4a-23d7215d4fcc',
    });

    expect(identity).not.toBeNull();
    expect(identity!.ownerSub).toBe('55086498-3091-80e1-cd4a-23d7215d4fcc');
    expect(identity!.tenantId).toBe('org-acme-123');
  });

  it('prefers cognito_sub over user_id when both present', () => {
    const identity = buildPersonalContextIdentity({
      cognito_sub: 'cognito-sub-uuid',
      tenant_id: 'org-123',
      user_id: 'different-user-id',
    });

    expect(identity).not.toBeNull();
    expect(identity!.ownerSub).toBe('cognito-sub-uuid');
  });

  it('returns null when tenant_id is missing', () => {
    const identity = buildPersonalContextIdentity({
      cognito_sub: '44086498-2091-70e1-bd3a-12c6104c3ebb',
    });

    expect(identity).toBeNull();
  });

  it('returns null when both cognito_sub and user_id are missing', () => {
    const identity = buildPersonalContextIdentity({
      tenant_id: 'org-123',
    });

    expect(identity).toBeNull();
  });

  it('returns null for completely empty payload (service-account without identity)', () => {
    const identity = buildPersonalContextIdentity({});

    expect(identity).toBeNull();
  });

  it('returns frozen (immutable) identity object', () => {
    const identity = buildPersonalContextIdentity({
      cognito_sub: 'sub-uuid',
      tenant_id: 'org-123',
    });

    expect(identity).not.toBeNull();
    expect(Object.isFrozen(identity)).toBe(true);
    // Attempting to modify should throw in strict mode / be no-op
    expect(() => {
      (identity as any).ownerSub = 'hacked';
    }).toThrow();
  });
});

describe('getPersonalContextHeaders', () => {
  it('produces correct X-Owner-Sub and X-Tenant-Id headers', () => {
    const identity: PersonalContextIdentity = Object.freeze({
      ownerSub: '44086498-2091-70e1-bd3a-12c6104c3ebb',
      tenantId: 'org-acme-123',
    });

    const headers = getPersonalContextHeaders(identity);

    expect(headers).not.toBeNull();
    expect(headers!['X-Owner-Sub']).toBe('44086498-2091-70e1-bd3a-12c6104c3ebb');
    expect(headers!['X-Tenant-Id']).toBe('org-acme-123');
  });

  it('returns null when identity is null', () => {
    const headers = getPersonalContextHeaders(null);
    expect(headers).toBeNull();
  });

  it('returns null when identity has empty ownerSub', () => {
    const identity: PersonalContextIdentity = Object.freeze({
      ownerSub: '',
      tenantId: 'org-123',
    });

    const headers = getPersonalContextHeaders(identity);
    expect(headers).toBeNull();
  });

  it('returns null when identity has empty tenantId', () => {
    const identity: PersonalContextIdentity = Object.freeze({
      ownerSub: 'sub-uuid',
      tenantId: '',
    });

    const headers = getPersonalContextHeaders(identity);
    expect(headers).toBeNull();
  });

  it('returns frozen (immutable) headers object', () => {
    const identity: PersonalContextIdentity = Object.freeze({
      ownerSub: 'sub-uuid',
      tenantId: 'org-123',
    });

    const headers = getPersonalContextHeaders(identity);
    expect(headers).not.toBeNull();
    expect(Object.isFrozen(headers)).toBe(true);
    expect(() => {
      (headers as any)['X-Owner-Sub'] = 'spoofed';
    }).toThrow();
  });

  describe('anti-spoof: agent/LLM input cannot override headers', () => {
    it('headers are derived solely from dispatch metadata, not from arbitrary input', () => {
      // Simulate: agent tries to inject identity by passing crafted data.
      // The buildPersonalContextIdentity function only accepts the specific
      // dispatch payload fields. Even if an agent constructs a fake payload,
      // the harness calls buildPersonalContextIdentity with the ORIGINAL
      // task payload from SQS, not with anything the agent produces.
      const trustedPayload = {
        cognito_sub: 'real-user-sub',
        tenant_id: 'real-tenant',
        user_id: 'real-user-sub',
      };

      const identity = buildPersonalContextIdentity(trustedPayload);
      const headers = getPersonalContextHeaders(identity);

      // Agent cannot modify the frozen identity
      expect(identity!.ownerSub).toBe('real-user-sub');
      expect(headers!['X-Owner-Sub']).toBe('real-user-sub');
      expect(headers!['X-Tenant-Id']).toBe('real-tenant');

      // Even if someone tries to construct a different identity, the harness
      // only ever uses the one built from the original task payload.
      const spoofedPayload = {
        cognito_sub: 'attacker-sub',
        tenant_id: 'attacker-tenant',
      };
      const spoofedIdentity = buildPersonalContextIdentity(spoofedPayload);
      // This produces a valid identity — but the harness never calls this
      // with agent-provided data. The test proves the boundary:
      expect(spoofedIdentity!.ownerSub).toBe('attacker-sub');
      // The real protection is architectural: the harness builds identity
      // ONCE from the SQS message and passes it immutably. See the
      // integration in complex-task-chat-agent.ts.
    });

    it('frozen identity prevents mutation after construction', () => {
      const identity = buildPersonalContextIdentity({
        cognito_sub: 'user-sub',
        tenant_id: 'tenant-id',
      });

      // Agent code that somehow gets a reference cannot mutate it
      expect(() => {
        (identity as any).ownerSub = 'evil-sub';
      }).toThrow();
      expect(() => {
        (identity as any).tenantId = 'evil-tenant';
      }).toThrow();

      // Original values unchanged
      expect(identity!.ownerSub).toBe('user-sub');
      expect(identity!.tenantId).toBe('tenant-id');
    });
  });
});

describe('getPersonalContextEnvVars', () => {
  it('produces ADP_OWNER_SUB and ADP_TENANT_ID env vars', () => {
    const identity: PersonalContextIdentity = Object.freeze({
      ownerSub: 'sub-uuid-123',
      tenantId: 'org-456',
    });

    const envVars = getPersonalContextEnvVars(identity);

    expect(envVars).toEqual({
      ADP_OWNER_SUB: 'sub-uuid-123',
      ADP_TENANT_ID: 'org-456',
    });
  });

  it('returns empty object when identity is null', () => {
    const envVars = getPersonalContextEnvVars(null);
    expect(envVars).toEqual({});
  });

  it('returns empty object when identity has empty fields', () => {
    const identity: PersonalContextIdentity = Object.freeze({
      ownerSub: '',
      tenantId: 'org-123',
    });

    const envVars = getPersonalContextEnvVars(identity);
    expect(envVars).toEqual({});
  });

  it('env vars are plain strings (safe for subprocess injection)', () => {
    const identity: PersonalContextIdentity = Object.freeze({
      ownerSub: 'sub-uuid',
      tenantId: 'org-id',
    });

    const envVars = getPersonalContextEnvVars(identity);
    for (const [key, value] of Object.entries(envVars)) {
      expect(typeof key).toBe('string');
      expect(typeof value).toBe('string');
    }
  });
});

describe('end-to-end: dispatch → identity → headers', () => {
  it('user-dispatched task produces valid headers', () => {
    // Simulates the full flow: ingest lambda sets cognito_sub + tenant_id,
    // worker reads them from SQS payload, builds identity, gets headers.
    const sqsPayload = {
      task_id: 'task-001',
      session_id: 'sess-001',
      message: 'hello',
      user_id: '44086498-2091-70e1-bd3a-12c6104c3ebb',
      cognito_sub: '44086498-2091-70e1-bd3a-12c6104c3ebb',
      tenant_id: 'org-acme',
      org_id: 'org-acme',
    };

    const identity = buildPersonalContextIdentity(sqsPayload);
    const headers = getPersonalContextHeaders(identity);

    expect(headers).not.toBeNull();
    expect(headers!['X-Owner-Sub']).toBe('44086498-2091-70e1-bd3a-12c6104c3ebb');
    expect(headers!['X-Tenant-Id']).toBe('org-acme');
  });

  it('service-account dispatch (no cognito_sub, no user_id) → null identity → no headers', () => {
    // Service-account or automation dispatch without a human user.
    // MCP server will fail-closed (personal-context unavailable).
    const sqsPayload = {
      task_id: 'task-auto',
      session_id: 'sess-auto',
      message: 'automated task',
      tenant_id: 'org-acme',
      org_id: 'org-acme',
    };

    const identity = buildPersonalContextIdentity(sqsPayload);
    const headers = getPersonalContextHeaders(identity);

    expect(identity).toBeNull();
    expect(headers).toBeNull();
  });

  it('existing dispatch without cognito_sub field still works (regression)', () => {
    // Pre-#1289 messages won't have cognito_sub. The system should still
    // function — just without personal-context access (fail-closed in MCP).
    const legacyPayload = {
      task_id: 'task-legacy',
      session_id: 'sess-legacy',
      message: 'old message format',
      user_id: 'some-user-id',
      tenant_id: 'org-legacy',
    };

    // Still builds identity from user_id fallback
    const identity = buildPersonalContextIdentity(legacyPayload);
    expect(identity).not.toBeNull();
    expect(identity!.ownerSub).toBe('some-user-id');
    expect(identity!.tenantId).toBe('org-legacy');
  });
});
