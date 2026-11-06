import { beforeEach, describe, expect, it } from 'vitest';
import { rememberProductConnection, takeProductConnectionReturn } from '@/services/productConnectionSetup';

const actor = { id: 'user-1', orgId: 'org-1' };
beforeEach(() => sessionStorage.clear());

describe('Product connection callback navigation', () => {
  it('returns to the originating Product and service once', () => {
    rememberProductConnection({ productId: 'product', projectId: 'project' }, 'slack', actor);
    expect(takeProductConnectionReturn(actor, 'slack')).toBe('/products/product/projects/project?connection_setup=slack');
    expect(takeProductConnectionReturn(actor, 'slack')).toBeNull();
  });
  it.each([
    { userId: 'someone-else' }, { orgId: 'another-org' }, { service: 'jira' },
    { productId: '//evil.example' }, { projectId: '../../settings' },
    { expiresAt: 1 }, { expiresAt: Date.now() + 60 * 60 * 1000 },
  ])('rejects a changed or expired return context: %j', change => {
    rememberProductConnection({ productId: 'product', projectId: 'project' }, 'github', actor);
    const saved = JSON.parse(sessionStorage.getItem('adp_product_connection_return')!);
    sessionStorage.setItem('adp_product_connection_return', JSON.stringify({ ...saved, ...change }));
    expect(takeProductConnectionReturn(actor, 'github')).toBeNull();
  });
  it('does not return a different provider’s callback to the saved Product', () => {
    rememberProductConnection({ productId: 'product', projectId: 'project' }, 'github', actor);
    expect(takeProductConnectionReturn(actor, 'slack')).toBeNull();
  });
  it('clears an old Product return when starting setup from Settings', () => {
    rememberProductConnection({ productId: 'product', projectId: 'project' }, 'slack', actor);
    rememberProductConnection(undefined, 'slack', actor);
    expect(takeProductConnectionReturn(actor, 'slack')).toBeNull();
  });
});
