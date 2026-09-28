import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import AccessRequests from '@/pages/admin/AccessRequests';

vi.mock('@/services/onboarding', () => ({
  getAccessRequests: vi.fn(),
  approveAccessRequest: vi.fn(),
  denyAccessRequest: vi.fn(),
}));

import {
  getAccessRequests,
  approveAccessRequest,
  denyAccessRequest,
} from '@/services/onboarding';

const mockGetRequests = getAccessRequests as ReturnType<typeof vi.fn>;
const mockApprove = approveAccessRequest as ReturnType<typeof vi.fn>;
const mockDeny = denyAccessRequest as ReturnType<typeof vi.fn>;

const mockRequests = [
  {
    id: 'req-1',
    target_login: 'alice',
    proposed_tenant_id: 'alice-workspace',
    motivation: 'I need access to build stuff',
    avatar_url: 'https://github.com/alice.png',
    created_at: '2024-06-01T10:00:00Z',
  },
  {
    id: 'req-2',
    target_login: 'bob',
    proposed_tenant_id: 'bob-dev',
    motivation: 'For development purposes',
    avatar_url: null,
    created_at: '2024-06-02T10:00:00Z',
  },
];

function renderAccessRequests() {
  return render(
    <MemoryRouter>
      <AccessRequests />
    </MemoryRouter>,
  );
}

describe('AccessRequests Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetRequests.mockResolvedValue([...mockRequests]);
    mockApprove.mockResolvedValue(undefined);
    mockDeny.mockResolvedValue(undefined);
  });

  it('renders the page title', async () => {
    renderAccessRequests();

    expect(screen.getByText('Access Requests')).toBeInTheDocument();
    await waitFor(() => {
      expect(mockGetRequests).toHaveBeenCalledTimes(1);
    });
  });

  it('renders a table of pending requests', async () => {
    renderAccessRequests();

    await waitFor(() => {
      expect(screen.getByText('alice')).toBeInTheDocument();
      expect(screen.getByText('bob')).toBeInTheDocument();
    });

    expect(screen.getByText('alice-workspace')).toBeInTheDocument();
    expect(screen.getByText('bob-dev')).toBeInTheDocument();
  });

  it('shows empty state when no requests', async () => {
    mockGetRequests.mockResolvedValue([]);
    renderAccessRequests();

    await waitFor(() => {
      expect(screen.getByText('No pending access requests.')).toBeInTheDocument();
    });
  });

  it('approve button calls approveAccessRequest and removes row', async () => {
    const user = userEvent.setup();
    renderAccessRequests();

    await waitFor(() => {
      expect(screen.getByText('alice')).toBeInTheDocument();
    });

    const approveButtons = screen.getAllByRole('button', { name: /approve/i });
    // bob is first (sorted desc by created_at), alice second
    await user.click(approveButtons[1]); // approve alice (second row)

    await waitFor(() => {
      expect(mockApprove).toHaveBeenCalledWith('req-1');
    });

    await waitFor(() => {
      expect(screen.queryByText('alice')).not.toBeInTheDocument();
    });

    // Toast shown
    expect(screen.getByText(/Approved alice/)).toBeInTheDocument();
  });

  it('deny button opens modal and calls denyAccessRequest', async () => {
    const user = userEvent.setup();
    renderAccessRequests();

    await waitFor(() => {
      expect(screen.getByText('bob')).toBeInTheDocument();
    });

    const denyButtons = screen.getAllByRole('button', { name: /^deny$/i });
    await user.click(denyButtons[0]); // deny bob (first row, sorted desc)

    // Modal should appear
    await waitFor(() => {
      expect(screen.getByText('Deny Request')).toBeInTheDocument();
    });

    // Type a note
    const textarea = screen.getByPlaceholderText('Reason (optional)');
    await user.type(textarea, 'Not approved for this environment');

    // Confirm deny
    const confirmButton = screen.getAllByRole('button', { name: /^deny$/i });
    // The modal has a Deny button
    const modalDenyBtn = confirmButton[confirmButton.length - 1];
    await user.click(modalDenyBtn);

    await waitFor(() => {
      expect(mockDeny).toHaveBeenCalledWith('req-2', 'Not approved for this environment');
    });

    await waitFor(() => {
      expect(screen.queryByText('bob')).not.toBeInTheDocument();
    });

    expect(screen.getByText(/Denied bob/)).toBeInTheDocument();
  });

  it('renders motivation text safely (XSS)', async () => {
    mockGetRequests.mockResolvedValue([
      {
        id: 'req-xss',
        target_login: 'attacker',
        proposed_tenant_id: 'evil-corp',
        motivation: '<script>alert(1)</script>',
        avatar_url: null,
        created_at: '2024-06-03T10:00:00Z',
      },
    ]);

    renderAccessRequests();

    await waitFor(() => {
      expect(screen.getByText('attacker')).toBeInTheDocument();
    });

    // Script tag should be rendered as text, not executed
    const motivationCell = screen.getByText('<script>alert(1)</script>');
    expect(motivationCell).toBeInTheDocument();
    expect(document.querySelectorAll('script')).toHaveLength(0);
  });

  it('cancel button in deny modal closes modal without action', async () => {
    const user = userEvent.setup();
    renderAccessRequests();

    await waitFor(() => {
      expect(screen.getByText('bob')).toBeInTheDocument();
    });

    const denyButtons = screen.getAllByRole('button', { name: /^deny$/i });
    await user.click(denyButtons[0]);

    await waitFor(() => {
      expect(screen.getByText('Deny Request')).toBeInTheDocument();
    });

    const cancelButton = screen.getByRole('button', { name: /cancel/i });
    await user.click(cancelButton);

    await waitFor(() => {
      expect(screen.queryByText('Deny Request')).not.toBeInTheDocument();
    });

    expect(mockDeny).not.toHaveBeenCalled();
  });
});
