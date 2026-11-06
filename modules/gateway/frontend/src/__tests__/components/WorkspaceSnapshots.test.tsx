import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import WorkspaceSnapshots from '@/components/devbox/WorkspaceSnapshots';
import { apiClient } from '@/services/api';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn() } }));

const createdAt = '2026-10-21T12:00:00Z';
const observedAt = '2026-10-22T13:00:00Z';
const snapshot = { snapshot_id: 'snap-private-123', size_gb: 100, state: 'present', created_at: createdAt, observed_at: observedAt };
let items: Array<typeof snapshot> = [];
let unavailable = false;

beforeEach(() => {
  items = [];
  unavailable = false;
  vi.mocked(apiClient.get).mockReset().mockImplementation(async endpoint => {
    if (endpoint !== '/cloud-desktops/owned/snapshots') throw new Error(`Unexpected endpoint: ${endpoint}`);
    if (unavailable) throw { status: 503 };
    return { items };
  });
});

it('distinguishes snapshot creation from observation time without exposing provider IDs', async () => {
  items = [snapshot, { ...snapshot, snapshot_id: 'snap-other', size_gb: null, state: 'pending', created_at: '', observed_at: '' }];
  render(<WorkspaceSnapshots desktopId="owned" />);
  const panel = screen.getByRole('region', { name: 'Workspace snapshots' });
  await waitFor(() => expect(within(panel).getAllByRole('listitem')).toHaveLength(2));
  expect(within(panel).getAllByRole('listitem')[0]).toHaveTextContent('100 GB');
  expect(within(panel).getAllByRole('listitem')[0]).toHaveTextContent('State: present');
  const timestamps = within(panel).getAllByRole('listitem')[0].querySelectorAll('time');
  expect(timestamps[0]).toHaveAttribute('dateTime', createdAt);
  expect(timestamps[1]).toHaveAttribute('dateTime', observedAt);
  expect(panel).toHaveTextContent('Creation time unavailable');
  expect(panel).toHaveTextContent('Size unavailable');
  expect(panel).toHaveTextContent('Last checked: unavailable');
  expect(panel).toHaveTextContent('including secrets');
  expect(panel).toHaveTextContent('during Replace');
  expect(panel.textContent).not.toContain('snap-private');
  expect(within(panel).queryByRole('button')).not.toBeInTheDocument();
  expect(within(panel).queryByRole('link')).not.toBeInTheDocument();
});

it('does not fetch without an owned box and never claims a failed list is empty', async () => {
  const view = render(<WorkspaceSnapshots desktopId={null} />);
  expect(vi.mocked(apiClient.get)).not.toHaveBeenCalled();
  expect(screen.getByText('No workspace snapshots yet.')).toBeInTheDocument();
  unavailable = true;
  view.rerender(<WorkspaceSnapshots desktopId="owned" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Snapshot status unavailable');
  expect(screen.queryByText('No workspace snapshots yet.')).not.toBeInTheDocument();
  view.rerender(<WorkspaceSnapshots desktopId={null} />);
  expect(screen.getByText('No workspace snapshots yet.')).toBeInTheDocument();
});

it('refreshes snapshot metadata on focus rather than polling', async () => {
  render(<WorkspaceSnapshots desktopId="owned" />);
  await screen.findByText('No workspace snapshots yet.');
  items = [snapshot];
  fireEvent.focus(window);
  await screen.findByText('State: present');
  expect(vi.mocked(apiClient.get).mock.calls).toHaveLength(2);
});
