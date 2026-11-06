import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import useProductSourceLabels from '@/pages/products/services/useProductSourceLabels';
import { inspectProductSource, listProductSources, type ProductSource, type RecordReference } from '@/services/products';

vi.mock('@/services/products', () => ({ inspectProductSource: vi.fn(), listProductSources: vi.fn() }));
const ref = (id: string, type = 'repository_commit', connection = 'github'): RecordReference => ({ tenant_id: 'tenant', project_id: 'project',
  source_connection_id: connection, record_type: type, record_id: id, record_revision: 'saved-revision' });
const github: ProductSource = { connection_ref: { kind: 'github_installation', id: 'github' }, provider: 'github', label: 'Example', health: 'healthy', coverage: 'complete', capabilities: ['repository'] };
const aws: ProductSource = { ...github, connection_ref: { kind: 'credential', id: 'aws' }, provider: 'aws', label: 'Development' };
const repo = (id: string, name: string) => ({ kind: 'repository' as const, native_id: id, full_name: name, name, namespace: 'example', source_revision: 'live-metadata-revision' });
const account = (id = '123456789012', revision = 'saved-revision') => ({ kind: 'environment' as const, native_id: 'us-east-1', name: 'Development', namespace: id, source_revision: revision,
  verified_environment: { account_id: id, regions: ['us-east-1'], role_ref: `arn:aws:iam::${id}:role/example` } });
const mount = (refs: RecordReference[]) => renderHook(() => useProductSourceLabels('product', 'project', 'tenant', refs));
beforeEach(() => { vi.resetAllMocks(); vi.mocked(listProductSources).mockResolvedValue({ items: [github, aws], next_cursor: null }); });

it('resolves multiple repositories and documents across source and resource pages', async () => {
  vi.mocked(listProductSources).mockResolvedValueOnce({ items: [aws], next_cursor: 'next-source' }).mockResolvedValueOnce({ items: [github], next_cursor: null });
  vi.mocked(inspectProductSource).mockResolvedValueOnce({ items: [{ ...github, resources: [repo('11', 'example/first')], resource_cursor: 'next-resource' }] })
    .mockResolvedValueOnce({ items: [{ ...github, resources: [repo('22', 'example/second')] }] });
  const first = ref('11'), second = ref('22'), doc = ref('22:docs/architecture.md', 'document');
  const { result } = mount([first, second, doc]);
  await waitFor(() => expect(result.current.sourceName(second)).toBe('example/second'));
  expect(result.current.sourceName(first)).toBe('example/first');
  expect(result.current.sourceName(doc)).toBe('example/second / docs/architecture.md');
  expect(inspectProductSource).toHaveBeenLastCalledWith('product', 'project', github.connection_ref, 'next-resource');
  expect(first.record_revision).toBe('saved-revision');
});

it('shows each verified account ID and connection name, including different connections to the same account', async () => {
  const second = { ...aws, label: 'Production', connection_ref: { ...aws.connection_ref, id: 'aws-two' } };
  vi.mocked(listProductSources).mockResolvedValue({ items: [aws, second], next_cursor: null });
  vi.mocked(inspectProductSource).mockImplementation(async (_p, _j, connection) => ({ items: [{ ...(connection.id === 'aws' ? aws : second), coverage: 'partial', resources: [account()] }] }));
  const firstRef = ref('aws', 'connection', 'aws'), secondRef = ref('aws-two', 'connection', 'aws-two');
  const { result } = mount([firstRef, secondRef]);
  await waitFor(() => expect(result.current.accountName(firstRef)).toBe('123456789012 · Development'));
  expect(result.current.accountName(secondRef)).toBe('123456789012 · Production');
});

it.each(['wrong-revision', 'ambiguous', 'denied', 'foreign-connection'])('does not present an unverified account (%s)', async reason => {
  vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...aws,
    ...(reason === 'foreign-connection' ? { connection_ref: { ...aws.connection_ref, id: 'foreign' } } : {}),
    coverage: reason === 'denied' ? 'denied' : 'partial', resources: reason === 'ambiguous' ? [account(), account('999999999999')] : [account('123456789012', reason === 'wrong-revision' ? 'old' : 'saved-revision')],
  }] });
  const selected = ref('aws', 'connection', 'aws'); const { result } = mount([selected]);
  await waitFor(() => expect(inspectProductSource).toHaveBeenCalled());
  expect(result.current.accountName(selected)).toBe('Account ID unavailable · Development');
});

it('keeps successful account metadata when repository lookup fails and exposes an explicit fallback', async () => {
  vi.mocked(inspectProductSource).mockImplementation(async (_p, _j, connection) => {
    if (connection.id === 'github') throw new Error('unavailable');
    return { items: [{ ...aws, resources: [account()] }] };
  });
  const source = ref('11'), selected = ref('aws', 'connection', 'aws'); const { result } = mount([source, selected]);
  await waitFor(() => expect(result.current.sourceName(source)).toBe('Repository name unavailable · ID 11'));
  expect(result.current.accountName(selected)).toBe('123456789012 · Development');
});

it('does not use a same-ID repository from another connection or inspect foreign-scope references', async () => {
  vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...github, connection_ref: { ...github.connection_ref, id: 'other' }, resources: [repo('11', 'other/private')] }] });
  const source = ref('11'); const { result } = mount([source, { ...ref('22'), tenant_id: 'foreign' }]);
  await waitFor(() => expect(result.current.sourceName(source)).toBe('Repository name unavailable · ID 11'));
  expect(inspectProductSource).toHaveBeenCalledTimes(1);
  const foreign = mount([{ ...ref('22'), project_id: 'foreign' }]);
  expect(foreign.result.current.sourceName(ref('22'))).not.toContain('other/private');
  expect(listProductSources).toHaveBeenCalledTimes(1);
});

it('discards late metadata when the Product changes', async () => {
  let finish!: (value: { items: ProductSource[] }) => void;
  vi.mocked(inspectProductSource).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const source = ref('11');
  const { result, rerender } = renderHook(({ product }) => useProductSourceLabels(product, 'project', 'tenant', [source]), { initialProps: { product: 'first' } });
  await waitFor(() => expect(inspectProductSource).toHaveBeenCalledTimes(1));
  vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
  rerender({ product: 'second' });
  await act(async () => { finish({ items: [{ ...github, resources: [repo('11', 'first/private')] }] }); });
  await waitFor(() => expect(result.current.sourceName(source)).toBe('Repository name unavailable · ID 11'));
});

it('retries metadata on Reload setup even when saved references have not changed', async () => {
  vi.mocked(listProductSources).mockRejectedValueOnce(new Error('temporary failure'));
  const source = ref('11');
  const { result, rerender } = renderHook(({ reload }) => useProductSourceLabels('product', 'project', 'tenant', [source], reload), { initialProps: { reload: {} } });
  await waitFor(() => expect(result.current.sourceName(source)).toBe('Repository name unavailable · ID 11'));
  vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...github, resources: [repo('11', 'example/application')] }] });
  rerender({ reload: {} });
  await waitFor(() => expect(result.current.sourceName(source)).toBe('example/application'));
});
