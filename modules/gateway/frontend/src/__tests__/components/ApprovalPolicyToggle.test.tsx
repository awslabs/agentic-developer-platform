/**
 * ApprovalPolicyToggle component tests.
 *
 * Issue #2984: Auto-join default ON with org-admin toggle.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApprovalPolicyToggle } from '@/components/org/ApprovalPolicyToggle';

// Mock toast
const mockSuccess = vi.fn();
const mockError = vi.fn();
vi.mock('@/contexts/ToastContext', () => ({
  useToast: () => ({ success: mockSuccess, error: mockError }),
}));

// Mock API client
const mockPut = vi.fn();
vi.mock('@/services/api', () => ({
  apiClient: { put: (...args: unknown[]) => mockPut(...args) },
}));

describe('ApprovalPolicyToggle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPut.mockResolvedValue({});
  });

  it('renders with auto-approve ON state', () => {
    render(
      <ApprovalPolicyToggle orgId="org-1" currentPolicy="auto_approve_org_members" canManage />,
    );

    expect(screen.getByText('Auto-join for org members')).toBeInTheDocument();
    expect(
      screen.getByText(/GitHub org members are automatically approved/),
    ).toBeInTheDocument();

    const toggle = screen.getByRole('switch');
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('renders with admin-approval state', () => {
    render(
      <ApprovalPolicyToggle orgId="org-1" currentPolicy="require_admin_approval" canManage />,
    );

    const toggle = screen.getByRole('switch');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(
      screen.getByText(/New members require admin approval/),
    ).toBeInTheDocument();
  });

  it('toggles from auto to require_admin_approval on click', async () => {
    const user = userEvent.setup();
    render(
      <ApprovalPolicyToggle orgId="org-1" currentPolicy="auto_approve_org_members" canManage />,
    );

    const toggle = screen.getByRole('switch');
    await user.click(toggle);

    expect(mockPut).toHaveBeenCalledWith('/admin/organizations/org-1', {
      member_approval_policy: 'require_admin_approval',
    });

    await waitFor(() => {
      expect(mockSuccess).toHaveBeenCalledWith(
        expect.stringContaining('Admin approval required'),
      );
    });
  });

  it('toggles from require_admin_approval to auto on click', async () => {
    const user = userEvent.setup();
    render(
      <ApprovalPolicyToggle orgId="org-1" currentPolicy="require_admin_approval" canManage />,
    );

    const toggle = screen.getByRole('switch');
    await user.click(toggle);

    expect(mockPut).toHaveBeenCalledWith('/admin/organizations/org-1', {
      member_approval_policy: 'auto_approve_org_members',
    });

    await waitFor(() => {
      expect(mockSuccess).toHaveBeenCalledWith(
        expect.stringContaining('Auto-join enabled'),
      );
    });
  });

  it('is disabled when canManage is false', async () => {
    const user = userEvent.setup();
    render(
      <ApprovalPolicyToggle
        orgId="org-1"
        currentPolicy="auto_approve_org_members"
        canManage={false}
      />,
    );

    const toggle = screen.getByRole('switch');
    expect(toggle).toBeDisabled();

    await user.click(toggle);
    expect(mockPut).not.toHaveBeenCalled();
  });

  it('shows error toast on API failure', async () => {
    mockPut.mockRejectedValue(new Error('Network error'));
    const user = userEvent.setup();

    render(
      <ApprovalPolicyToggle orgId="org-1" currentPolicy="auto_approve_org_members" canManage />,
    );

    const toggle = screen.getByRole('switch');
    await user.click(toggle);

    await waitFor(() => {
      expect(mockError).toHaveBeenCalledWith('Network error');
    });
  });
});
