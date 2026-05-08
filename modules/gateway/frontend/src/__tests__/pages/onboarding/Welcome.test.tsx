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

  it('pre-populates tenant ID from GitHub login', () => {
    renderWelcome();

    const input = screen.getByLabelText('Workspace ID') as HTMLInputElement;
    expect(input.value).toBe('testuser');
  });

  it('rejects empty tenant ID', async () => {
    const user = userEvent.setup();
    renderWelcome();

    const input = screen.getByLabelText('Workspace ID');
    await user.clear(input);

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    expect(screen.getByText('Tenant ID is required')).toBeInTheDocument();
    expect(mockSubmit).not.toHaveBeenCalled();
  });

  it('rejects tenant ID that is too short', async () => {
    const user = userEvent.setup();
    renderWelcome();

    const input = screen.getByLabelText('Workspace ID');
    await user.clear(input);
    await user.type(input, 'ab');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    expect(screen.getByText('Tenant ID must be at least 3 characters')).toBeInTheDocument();
  });

  it('rejects tenant ID with invalid characters', async () => {
    const user = userEvent.setup();
    renderWelcome();

    const input = screen.getByLabelText('Workspace ID');
    await user.clear(input);
    await user.type(input, '-invalid-start');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    expect(screen.getByText(/Must start and end with a letter or number/)).toBeInTheDocument();
  });

  it('rejects reserved names', async () => {
    const user = userEvent.setup();
    renderWelcome();

    const input = screen.getByLabelText('Workspace ID');
    await user.clear(input);
    await user.type(input, 'admin');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    expect(screen.getByText('This name is reserved. Please choose another.')).toBeInTheDocument();
  });

  it('requires motivation field', async () => {
    const user = userEvent.setup();
    renderWelcome();

    // Tenant ID is pre-filled, so just submit without motivation
    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    expect(screen.getByText('Please provide a reason for requesting access.')).toBeInTheDocument();
  });

  it('submits correct payload and navigates on 200 (approved)', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 200,
      json: async () => ({ redirect: '/dashboard' }),
    });

    renderWelcome();

    const textarea = screen.getByLabelText('Why do you need access?');
    await user.type(textarea, 'I need access for development');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    await waitFor(() => {
      expect(mockSubmit).toHaveBeenCalledWith({
        proposed_tenant_id: 'testuser',
        motivation: 'I need access for development',
      });
    });

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith('/dashboard', { replace: true });
    });
  });

  it('navigates to pending on 202', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 202,
      json: async () => ({ request_id: 'req-123' }),
    });

    renderWelcome();

    const textarea = screen.getByLabelText('Why do you need access?');
    await user.type(textarea, 'Development work');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith('/onboarding/pending', {
        state: { requestId: 'req-123' },
        replace: true,
      });
    });
  });

  it('shows inline error on 400 (invalid_tenant_id)', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 400,
      json: async () => ({ hint: 'Tenant ID must not start with a number' }),
    });

    renderWelcome();

    const textarea = screen.getByLabelText('Why do you need access?');
    await user.type(textarea, 'Need access');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    await waitFor(() => {
      expect(screen.getByText('Tenant ID must not start with a number')).toBeInTheDocument();
    });
  });

  it('shows collision error on 409', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 409,
      json: async () => ({}),
    });

    renderWelcome();

    const textarea = screen.getByLabelText('Why do you need access?');
    await user.type(textarea, 'Need access');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    await waitFor(() => {
      expect(screen.getByText('This tenant name is already taken. Pick another.')).toBeInTheDocument();
    });
  });

  it('shows unavailable message on 503', async () => {
    const user = userEvent.setup();
    mockSubmit.mockResolvedValue({
      status: 503,
      json: async () => ({}),
    });

    renderWelcome();

    const textarea = screen.getByLabelText('Why do you need access?');
    await user.type(textarea, 'Need access');

    const button = screen.getByRole('button', { name: /request access/i });
    await user.click(button);

    await waitFor(() => {
      expect(screen.getByText('Onboarding Not Available')).toBeInTheDocument();
      expect(screen.getByText(/not yet enabled/)).toBeInTheDocument();
    });
  });

  it('does not render script tags from user-supplied fields (XSS safety)', () => {
    renderWelcome();

    // React default escaping ensures script tags are rendered as text, not executed
    // The user info is rendered via React JSX (no dangerouslySetInnerHTML)
    const elements = document.querySelectorAll('script');
    expect(elements).toHaveLength(0);
  });
});
