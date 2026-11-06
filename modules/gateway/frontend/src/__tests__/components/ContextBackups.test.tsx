import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import ContextBackups from '@/components/devbox/ContextBackups';
import { apiClient } from '@/services/api';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn() } }));

const timestamp = new Date(Date.now() - 10 * 60_000).toISOString();
const checkpoint = (sequence: number, status = 'verified') => ({
  id: `private-${sequence}`, sequence, status, captured_at: timestamp, archive_size: 2_097_152,
  failure_class: status === 'failed' ? 'verification_failed' : null, is_latest_verified: sequence === 6,
});
let checkpoints: ReturnType<typeof checkpoint>[];
let selectedId: string | null;
let failList: boolean;
let failSelection: boolean;

beforeEach(() => {
  checkpoints = [];
  selectedId = null;
  failList = false;
  failSelection = false;
  vi.mocked(apiClient.get).mockReset().mockImplementation(async endpoint => {
    if (endpoint === '/v1/crew/checkpoints') {
      if (failList) throw { status: 503 };
      return { checkpoints };
    }
    if (endpoint === '/v1/crew/checkpoints/selection') {
      if (failSelection) throw { status: 503 };
      return { checkpoint_id: selectedId };
    }
    throw new Error(`Unexpected endpoint ${endpoint}`);
  });
});

describe('owner context backups', () => {
  it('shows exactly five newest verified rows with absolute and relative time and size, using selection not latest flag', async () => {
    checkpoints = [checkpoint(6), checkpoint(5), checkpoint(4), checkpoint(3), checkpoint(2), checkpoint(1), checkpoint(7, 'failed')]
      .map(row => ({ ...row, archive_size: row.sequence * 1_048_576 })).reverse();
    selectedId = 'private-4';
    render(<ContextBackups />);
    const panel = screen.getByRole('region', { name: 'Context backups' });
    await waitFor(() => expect(within(panel).getAllByRole('listitem')).toHaveLength(5));
    const rows = within(panel).getAllByRole('listitem');
    expect(rows.map(row => within(row).getByText(/MB/).textContent)).toEqual(['6.0 MB', '5.0 MB', '4.0 MB', '3.0 MB', '2.0 MB']);
    expect(rows[0].querySelector('time')).toHaveAttribute('dateTime', timestamp);
    expect(rows[0]).toHaveTextContent(new Date(timestamp).toLocaleString());
    expect(rows[0]).toHaveTextContent('minutes ago');
    expect(rows[2]).toHaveTextContent('Restore point');
    expect(rows[0]).not.toHaveTextContent('Restore point');
    expect(panel).toHaveTextContent('The newest context backup failed at');
    expect(panel.textContent).not.toMatch(/private-[0-9]+/);
    expect(within(panel).queryByRole('button')).not.toBeInTheDocument();
    expect(within(panel).queryByRole('link')).not.toBeInTheDocument();
  });

  it('shows an empty warning and remains readable when no checkpoints exist', async () => {
    render(<ContextBackups />);
    expect(await screen.findByText(/No backups yet/)).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Context backups' })).toHaveTextContent('Captured every 15 minutes');
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  it('shows unavailable after list refusal rather than claiming backups are safe', async () => {
    failList = true;
    render(<ContextBackups />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Backup status unavailable');
    expect(screen.queryByText(/Restore point/)).not.toBeInTheDocument();
  });

  it('shows unavailable after a network failure', async () => {
    vi.mocked(apiClient.get).mockRejectedValue(new TypeError('Network request failed'));
    render(<ContextBackups />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Backup status unavailable');
  });

  it('keeps older verified backups visible when the newest capture failed', async () => {
    checkpoints = [checkpoint(3, 'failed'), checkpoint(2), checkpoint(1)];
    checkpoints[0] = { ...checkpoints[0], captured_at: '2026-10-23T12:34:00Z' };
    render(<ContextBackups />);
    const warning = await screen.findByText(/The newest context backup failed at/);
    expect(warning.querySelector('time')).toHaveAttribute('dateTime', checkpoints[0].captured_at);
    expect(warning).toHaveTextContent(new Date(checkpoints[0].captured_at).toLocaleString());
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('excludes uploading and failed captures from the verified list', async () => {
    checkpoints = [checkpoint(4, 'uploading'), checkpoint(3, 'failed'), checkpoint(2), checkpoint(1)];
    render(<ContextBackups />);
    const panel = screen.getByRole('region', { name: 'Context backups' });
    await waitFor(() => expect(within(panel).getAllByRole('listitem')).toHaveLength(2));
    expect(within(panel).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('uses the latest-verified fallback only if selection cannot be read', async () => {
    checkpoints = [checkpoint(6), checkpoint(5)];
    failSelection = true;
    render(<ContextBackups />);
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(2));
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('Restore point');
  });

  it.each([null, 'private-unknown'])('does not badge a row when selection returns %s', async selection => {
    checkpoints = [checkpoint(6), checkpoint(5)];
    selectedId = selection;
    render(<ContextBackups />);
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(2));
    expect(screen.queryByText('Restore point')).not.toBeInTheDocument();
  });

  it.each([
    [Math.floor(512 * 1_048_576 * 0.8), false],
    [Math.ceil(512 * 1_048_576 * 0.8), true],
    [450 * 1_048_576, true],
  ] as const)('warns only when the newest verified archive exceeds the cap threshold (%s bytes)', async (size, expectedWarning) => {
    checkpoints = [{ ...checkpoint(5), archive_size: size }, { ...checkpoint(4), archive_size: 500 * 1_048_576 }];
    render(<ContextBackups />);
    const panel = screen.getByRole('region', { name: 'Context backups' });
    await waitFor(() => expect(within(panel).getAllByRole('listitem')).toHaveLength(2));
    const warning = within(panel).queryByRole('alert');
    if (expectedWarning) {
      expect(warning).toHaveTextContent(`${(size / 1_048_576).toFixed(1)} MB`);
      expect(warning).toHaveTextContent('512 MB');
      expect(warning).toHaveTextContent(/captures above the cap will be refused/i);
    } else {
      expect(warning).not.toBeInTheDocument();
    }
  });

  it('moves between empty, failed, unavailable and recovered states on focus', async () => {
    render(<ContextBackups />);
    await screen.findByText(/No backups yet/);
    checkpoints = [checkpoint(3, 'failed'), checkpoint(2)];
    selectedId = 'private-2';
    fireEvent.focus(window);
    await screen.findByText(/The newest context backup failed at/);
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText('Restore point')).toBeInTheDocument();
    failList = true;
    fireEvent.focus(window);
    await screen.findByText(/Backup status unavailable/);
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
    failList = false;
    checkpoints = [checkpoint(4), checkpoint(2)];
    selectedId = 'private-4';
    fireEvent.focus(window);
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('Restore point');
    expect(vi.mocked(apiClient.get).mock.calls.filter(([endpoint]) => endpoint === '/v1/crew/checkpoints')).toHaveLength(4);
  });

  it('refreshes on focus without polling continuously', async () => {
    render(<ContextBackups />);
    await screen.findByText(/No backups yet/);
    checkpoints = [checkpoint(6)];
    selectedId = 'private-6';
    fireEvent.focus(window);
    await screen.findByText('Restore point');
    expect(vi.mocked(apiClient.get).mock.calls.filter(([endpoint]) => endpoint === '/v1/crew/checkpoints')).toHaveLength(2);
  });
});
