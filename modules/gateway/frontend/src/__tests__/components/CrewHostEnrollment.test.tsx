import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import CrewHostEnrollment from '@/components/chat/CrewHostEnrollment';
import { apiClient } from '@/services/api';
import { enrollCrewHost } from '@/services/crewHostEnrollment';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn() } }));
vi.mock('@/services/crewHostEnrollment', () => ({ enrollCrewHost: vi.fn() }));
vi.mock('@/hooks/useCrewRuntime', async () => {
  const actual = await vi.importActual<typeof import('@/hooks/useCrewRuntime')>('@/hooks/useCrewRuntime');
  return { ...actual, useCrewRuntime: () => ({
    choice: 'adp-assistant', allowed: false, hostAllowed: true, loading: false, setChoice: vi.fn(),
    options: actual.RUNTIME_OPTIONS.filter(option => option.requires !== 'eks-pod'),
  }) };
});

const current = { desktop_id: 'current-box', profile: 'crew', status: 'running' };

beforeEach(() => {
  vi.mocked(apiClient.get).mockReset().mockResolvedValue({ contract_version: 1, items: [
    { desktop_id: 'old-box', profile: 'crew', status: 'released' },
    { desktop_id: 'other-profile', profile: 'dev', status: 'running' },
    current,
  ] });
  vi.mocked(enrollCrewHost).mockReset().mockResolvedValue(undefined);
});

describe('owner-initiated Crew box enrollment', () => {
  it('does not discover a host until an explicit settings-page action, and submits only the active Crew box', async () => {
    render(<CrewHostEnrollment />);
    expect(apiClient.get).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Enroll my Crew box' }));
    await waitFor(() => expect(enrollCrewHost).toHaveBeenCalledWith('current-box', expect.any(AbortSignal)));
    expect(apiClient.get).toHaveBeenCalledExactlyOnceWith('/cloud-desktops', expect.any(AbortSignal));
    expect(enrollCrewHost).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status')).toHaveTextContent('Enrollment submitted. Waiting for verified host setup.');
    expect(screen.queryByText(/dashboard ready/i)).toHaveTextContent('Submission does not make its dashboard ready.');
    expect(screen.getByRole('button', { name: 'Enroll my Crew box' })).toBeDisabled();
  });

  it('refuses ambiguous or missing current boxes instead of choosing a foreign or released box', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({ contract_version: 1, items: [current,
      { ...current, desktop_id: 'another-box' }] });
    const view = render(<CrewHostEnrollment />);
    fireEvent.click(screen.getByRole('button', { name: 'Enroll my Crew box' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Enrollment unavailable'));
    expect(enrollCrewHost).not.toHaveBeenCalled();
    view.unmount();
    vi.mocked(apiClient.get).mockResolvedValueOnce({ contract_version: 1, items: [
      { ...current, status: 'released' },
    ] });
    render(<CrewHostEnrollment />);
    fireEvent.click(screen.getByRole('button', { name: 'Enroll my Crew box' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Enrollment unavailable'));
    expect(enrollCrewHost).not.toHaveBeenCalled();
  });

  it('keeps host and API errors out of the page, allowing explicit retry', async () => {
    vi.mocked(enrollCrewHost).mockRejectedValueOnce(new Error('secret host response'));
    render(<CrewHostEnrollment />);
    fireEvent.click(screen.getByRole('button', { name: 'Enroll my Crew box' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Enrollment unavailable'));
    expect(screen.queryByText('secret host response')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Enroll my Crew box' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Enrollment submitted'));
    expect(enrollCrewHost).toHaveBeenCalledTimes(2);
  });

  it('prevents duplicate concurrent submissions and aborts when leaving the page', async () => {
    let finish!: () => void;
    vi.mocked(apiClient.get).mockImplementationOnce(() => new Promise(resolve => {
      finish = () => resolve({ contract_version: 1, items: [current] });
    }));
    const view = render(<CrewHostEnrollment />);
    fireEvent.click(screen.getByRole('button', { name: 'Enroll my Crew box' }));
    const signal = vi.mocked(apiClient.get).mock.calls[0][1];
    expect(screen.getByRole('button', { name: 'Enroll my Crew box' })).toBeDisabled();
    expect(apiClient.get).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(signal?.aborted).toBe(true);
    finish();
    await Promise.resolve();
    expect(enrollCrewHost).not.toHaveBeenCalled();
  });
});
