import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import OpenMyCrew from '@/components/chat/OpenMyCrew';
import { apiClient } from '@/services/api';
import { resolveCrewDashboardDestination } from '@/services/crewDashboard';

vi.mock('@/services/api', () => ({ apiClient: { post: vi.fn(), get: vi.fn() } }));
vi.mock('@/services/crewDashboard', () => ({ resolveCrewDashboardDestination: vi.fn() }));

const handoff = 'https://crew.example.test/crew/handoff';
const destination = 'https://crew.example.test/?token=single-use-link-12345';
const status = (state: string) => ({ state, target_kind: 'eks-pod', executor_generation: 1 });

function openTab() {
  const tab = { opener: window, location: { href: '' }, close: vi.fn() };
  const open = vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
  return { tab, open };
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.mocked(apiClient.post).mockReset().mockResolvedValue(status('starting'));
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(resolveCrewDashboardDestination).mockReset().mockResolvedValue(destination);
});

describe('Open my Crew', () => {
  it('reserves a tab on click, shows restoration, and only navigates after ready', async () => {
    const { tab, open } = openTab();
    vi.mocked(apiClient.get).mockResolvedValueOnce(status('restoring')).mockResolvedValueOnce(status('ready'));
    render(<OpenMyCrew handoffUrl={handoff} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open my Crew' }));
    expect(open).toHaveBeenCalledExactlyOnceWith('', '_blank');
    expect(tab.opener).toBeNull();
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Restoring Crew state'));
    expect(tab.location.href).toBe('');
    expect(resolveCrewDashboardDestination).not.toHaveBeenCalled();
    await waitFor(() => expect(tab.location.href).toBe(destination), { timeout: 3000 });
    expect(apiClient.post).toHaveBeenCalledWith('/crew/ensure', {}, expect.any(AbortSignal));
    expect(resolveCrewDashboardDestination).toHaveBeenCalledWith(handoff);
    expect(tab.close).not.toHaveBeenCalled();
  });

  it('closes a reserved tab and shows the sanitized gateway error with retry', async () => {
    const { tab } = openTab();
    vi.mocked(apiClient.get).mockRejectedValue({ detail: { error: 'crew_admission_unavailable' } });
    render(<OpenMyCrew handoffUrl={handoff} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open my Crew' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('crew_admission_unavailable'));
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(tab.close).toHaveBeenCalledOnce();
    expect(tab.location.href).toBe('');
  });

  it('refuses status from another executor generation without handing off', async () => {
    const { tab } = openTab();
    vi.mocked(apiClient.get).mockResolvedValue({ ...status('ready'), executor_generation: 2 });
    render(<OpenMyCrew handoffUrl={handoff} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open my Crew' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('crew_status_invalid'));
    expect(tab.close).toHaveBeenCalledOnce();
    expect(resolveCrewDashboardDestination).not.toHaveBeenCalled();
  });

  it('does not call ensure when the browser refuses the new tab', () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    render(<OpenMyCrew handoffUrl={handoff} />);
    fireEvent.click(screen.getByRole('button', { name: 'Open my Crew' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Allow pop-ups');
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
