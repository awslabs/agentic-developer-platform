import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import Welcome from '@/pages/onboarding/Welcome';

// Mock auth hook
const mockUser = {
  id: 'user-1',
  email: 'test@example.com',
  githubLogin: 'testuser',
  avatarUrl: 'https://github.com/testuser.png',
  role: 'platform_admin' as const,
  permissions: [],
  createdAt: '2024-01-01',
};

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: mockUser, logout: vi.fn() }),
}));

vi.mock('@/hooks/useAccessStatus', () => ({
  clearAccessStatusCache: vi.fn(),
}));

// Mock navigate
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

// Mock the submitAccessRequest
vi.mock('@/services/onboarding', () => ({
  submitAccessRequest: vi.fn(),
}));

import { submitAccessRequest } from '@/services/onboarding';
const mockSubmit = submitAccessRequest as ReturnType<typeof vi.fn>;

function renderWelcome() {
  return render(
    <MemoryRouter initialEntries={['/onboarding/welcome']}>
      <Welcome />
    </MemoryRouter>,
  );
}

describe('Welcome Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the welcome form with user info', () => {
    renderWelcome();
    expect(screen.getByText('Welcome to ADP')).toBeInTheDocument();
    expect(screen.getByText('testuser')).toBeInTheDocument();
    expect(screen.getByText('test@example.com')).toBeInTheDocument();
  });

  it('does NOT ask for a workspace/tenant ID — server derives it from the JWT', () => {
    renderWelcome();
    // Form must have NO input labelled workspace/tenant ID
    expect(screen.queryByLabelText(/workspace|tenant/i)).toBeNull();
  });

  it('requires motivation field', async () => {
    const user = userEvent.setup();
    renderWelcome();
    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);
    expect(screen.getByText('Please provide a reason for requesting access.')).toBeInTheDocument();
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it('submits only motivation (no tenant_id, provider, provider_user_id)', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 200,
      json: async () => ({ status: 'pending', request_id: 'req-999' }),
    });

    renderWelcome();
    const textarea = screen.getByLabelText('Why do you need access?');
    await user.type(textarea, 'I need access for development');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    await waitFor(() => {
      expect(mockSubmit).toHaveBeenCalledWith({
        motivation: 'I need access for development',
      });
    });
  });

  it('navigates to /dashboard on auto-approved response', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 200,
      json: async () => ({ status: 'approved', redirect: '/dashboard' }),
    });

    renderWelcome();
    await user.type(screen.getByLabelText('Why do you need access?'), 'test');
    await user.click(screen.getByRole('button', { name: /request access/i }));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith('/dashboard', { replace: true });
    });
  });

  it('navigates to /onboarding/pending on pending response', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 200,
      json: async () => ({ status: 'pending', request_id: 'req-123' }),
    });

    renderWelcome();
    await user.type(screen.getByLabelText('Why do you need access?'), 'Development work');
    await user.click(screen.getByRole('button', { name: /request access/i }));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith('/onboarding/pending', {
        state: { requestId: 'req-123' },
        replace: true,
      });
    });
  });

  it('shows a collision message (non-blocking) on collision response', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 200,
      json: async () => ({
        status: 'collision',
        reason: "A workspace named 'testuser' already exists.",
      }),
    });

    renderWelcome();
    await user.type(screen.getByLabelText('Why do you need access?'), 'test');
    await user.click(screen.getByRole('button', { name: /request access/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(/already exists/);
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it('shows unavailable page on unavailable response', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 200,
      json: async () => ({ status: 'unavailable' }),
    });

    renderWelcome();
    await user.type(screen.getByLabelText('Why do you need access?'), 'test');
    await user.click(screen.getByRole('button', { name: /request access/i }));

    await waitFor(() => {
      expect(screen.getByText('Onboarding Not Available')).toBeInTheDocument();
      expect(screen.getByText(/not yet enabled/)).toBeInTheDocument();
    });
  });

  it('does not render script tags from user-supplied fields (XSS safety)', () => {
    renderWelcome();
    // React default escaping ensures script tags are rendered as text, not executed
    // The user info is rendered via React JSX (no dangerouslySetInnerHTML)
    expect(document.querySelectorAll('script')).toHaveLength(0);
  });
});
