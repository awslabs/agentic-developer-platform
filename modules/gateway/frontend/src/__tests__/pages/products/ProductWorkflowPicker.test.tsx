import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ProductWorkflowPicker from '@/pages/products/ProductWorkflowPicker';
import { inspectProductPipelines, type ProductSource, type SourceResource } from '@/services/products';

vi.mock('@/services/products', () => ({ inspectProductPipelines: vi.fn() }));

const connection: ProductSource = { connection_ref: { kind: 'github_installation', id: 'connection-1' }, provider: 'github',
  label: 'Engineering', health: 'healthy', coverage: 'complete', capabilities: ['repository', 'pipeline'] };
const repository: SourceResource = { kind: 'repository', name: 'Delivery', native_id: '10', namespace: '20', source_revision: 'repo-rev' };
const workflows = Array.from({ length: 135 }, (_, index): SourceResource => ({ kind: 'pipeline', name: `Release ${index + 1}`,
  native_id: String(1000 + index), repository_id: '10', namespace: '20', source_revision: `revision-${index}` }));
const props = { productId: 'product-1', projectId: 'project-1', connection, repository, busy: false, pending: false,
  feedback: null, isLinked: (resource: SourceResource) => resource.native_id === '1000', onLink: vi.fn(), onClose: vi.fn() };

describe('Product workflow picker', () => {
  beforeEach(() => vi.resetAllMocks());

  it('searches beyond the first provider page, limits visible rows, and links only the chosen workflow', async () => {
    vi.mocked(inspectProductPipelines).mockImplementation(async (_product, _project, _connection, _repository, cursor) => {
      const page = Number(cursor ?? 1);
      return { items: [{ ...connection, resources: workflows.slice((page - 1) * 50, page * 50), resource_cursor: page < 3 ? String(page + 1) : undefined }] };
    });
    render(<ProductWorkflowPicker {...props} />);
    await screen.findByText('1–10 of 135 workflows');
    const list = screen.getByRole('list', { name: 'Repository workflows' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(10);
    expect(within(list).getByRole('button', { name: 'Linked', exact: true })).toBeDisabled();
    expect(props.onLink).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(screen.getByText('11–20 of 135 workflows')).toBeInTheDocument();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search workflows' }), { target: { value: 'release 125' } });
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
    fireEvent.click(within(list).getByRole('button', { name: 'Link to project' }));
    expect(props.onLink).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ resources: workflows }), workflows[124]);
    expect(vi.mocked(inspectProductPipelines).mock.calls.map(call => call[4])).toEqual([undefined, '2', '3']);
    expect(screen.getByText(/running or deploying is a separate action/)).toBeInTheDocument();
  });

  it('withdraws stale choices when a later discovery page is denied and can retry', async () => {
    let finish!: (result: { items: ProductSource[] }) => void;
    vi.mocked(inspectProductPipelines).mockResolvedValueOnce({ items: [{ ...connection, resources: workflows.slice(0, 50), resource_cursor: '2' }] })
      .mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    render(<ProductWorkflowPicker {...props} />);
    await screen.findByText('Release 1');
    await act(async () => finish({ items: [{ ...connection, coverage: 'denied' }] }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read the workflows');
    expect(screen.queryByText('Release 1')).not.toBeInTheDocument();
    expect(props.onLink).not.toHaveBeenCalled();
    vi.mocked(inspectProductPipelines).mockResolvedValue({ items: [{ ...connection, resources: [workflows[124]] }] });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Release 125')).toBeInTheDocument();
  });

  it('ignores a late response from another project and stops reading its remaining pages', async () => {
    let finish!: (result: { items: ProductSource[] }) => void;
    vi.mocked(inspectProductPipelines).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }))
      .mockResolvedValue({ items: [{ ...connection, resources: [] }] });
    const view = render(<ProductWorkflowPicker {...props} />);
    await waitFor(() => expect(inspectProductPipelines).toHaveBeenCalledTimes(1));
    view.rerender(<ProductWorkflowPicker {...props} projectId="project-2" />);
    await screen.findByText('No workflows are available in this repository.');
    await act(async () => finish({ items: [{ ...connection, resources: workflows.slice(0, 50), resource_cursor: '2' }] }));
    expect(screen.queryByText('Release 1')).not.toBeInTheDocument();
    expect(inspectProductPipelines).toHaveBeenCalledTimes(2);
  });
});
