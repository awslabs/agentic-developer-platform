import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import Pending from '@/pages/onboarding/Pending';

// Mock navigate
const mockNavigate = vi.fn();
const mockLogout = vi.fn();

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ logout: mockLogout }),
}));

vi.mock('@/hooks/useAccessStatus', () => ({
  clearAccessStatusCache: vi.fn(),
}));

vi.mock('@/services/onboarding', () => ({
  getAccessStatus: vi.fn(),
}));

import { getAccessStatus } from '@/services/onboarding';
const mockGetStatus = getAccessStatus as ReturnType<typeof vi.fn>;

function renderPending() {
  return render(
    <MemoryRouter initialEntries={['/onboarding/pending']}>
      <Pending />
    </MemoryRouter>,
  );
}

describe('Pending Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    mockGetStatus.mockResolvedValue({ status: 'pending' });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders the pending message', () => {
    renderPending();

    expect(screen.getByText('Request Under Review')).toBeInTheDocument();
    expect(screen.getByText(/awaiting admin approval/)).toBeInTheDocument();
  });

  it('shows a check status button', () => {
    renderPending();

    expect(screen.getByRole('button', { name: /check status/i })).toBeInTheDocument();
  });

  it('shows a sign out button', () => {
    renderPending();

    expect(screen.getByRole('button', { name: /sign out/i })).toBeInTheDocument();
  });

  it('polls every 30 seconds', async () => {
    renderPending();

    expect(mockGetStatus).not.toHaveBeenCalled();

    // Advance 30 seconds
    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    expect(mockGetStatus).toHaveBeenCalledTimes(1);

    // Advance another 30 seconds
    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    expect(mockGetStatus).toHaveBeenCalledTimes(2);
  });

  it('redirects to dashboard when status becomes registered', async () => {
    mockGetStatus.mockResolvedValue({ status: 'registered' });
    renderPending();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    expect(mockNavigate).toHaveBeenCalledWith('/dashboard', { replace: true });
  });

  it('redirects to denied page when status becomes denied', async () => {
    mockGetStatus.mockResolvedValue({ status: 'denied' });
    renderPending();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    expect(mockNavigate).toHaveBeenCalledWith('/onboarding/denied', { replace: true });
  });

  it('manual refresh calls getAccessStatus', async () => {
    vi.useRealTimers();
    const user = userEvent.setup();
    renderPending();

    const button = screen.getByRole('button', { name: /check status/i });
    await user.click(button);

    expect(mockGetStatus).toHaveBeenCalledTimes(1);
  });

  it('sign out calls logout', async () => {
    vi.useRealTimers();
    const user = userEvent.setup();
    renderPending();

    const button = screen.getByRole('button', { name: /sign out/i });
    await user.click(button);

    expect(mockLogout).toHaveBeenCalled();
  });
});
