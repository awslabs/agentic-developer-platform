/**
 * RateLimitFormModal Component Tests
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Tests for the rate limit form modal create, edit, and validation flows.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RateLimitFormModal } from '@/components/ratelimit/RateLimitFormModal';
import { ToastProvider } from '@/contexts/ToastContext';
import { EntityType } from '@/types';

// Mock the ratelimit service
vi.mock('@/services/ratelimit', () => ({
  createRatelimit: vi.fn(),
  updateRatelimit: vi.fn(),
}));

// Mock the admin service for entity fetching - return empty results to force manual input
// Issue #4948: the entity pickers read the platform's tenancy tables. `getOrgTeams` is
// T1's ORG-WIDE team route (PR #4917), which replaces the department-scoped `getTeams` —
// a rate limit may target any team in the org.
vi.mock('@/services/admin', () => ({
  getOrganizations: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  getDepartments: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  getOrgTeams: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  getOrgUsers: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
}));

import { createRatelimit, updateRatelimit } from '@/services/ratelimit';
import { getOrganizations, getOrgTeams, getOrgUsers } from '@/services/admin';

const mockCreateRatelimit = createRatelimit as ReturnType<typeof vi.fn>;
const mockUpdateRatelimit = updateRatelimit as ReturnType<typeof vi.fn>;
const mockGetOrganizations = getOrganizations as ReturnType<typeof vi.fn>;
const mockGetOrgTeams = getOrgTeams as ReturnType<typeof vi.fn>;

const renderComponent = (props: Partial<Parameters<typeof RateLimitFormModal>[0]> = {}) => {
  const defaultProps = {
    isOpen: true,
    onClose: vi.fn(),
    onSuccess: vi.fn(),
    orgId: 'org-001',
  };

  return render(
    <ToastProvider>
      <RateLimitFormModal {...defaultProps} {...props} />
    </ToastProvider>
  );
};

describe('RateLimitFormModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCreateRatelimit.mockResolvedValue({
      entityType: EntityType.TEAM,
      entityId: 'team-001',
      rpm: 100,
      tpm: 10000,
      concurrentRequests: 5,
      updatedAt: new Date().toISOString(),
    });
    mockUpdateRatelimit.mockResolvedValue({
      entityType: EntityType.TEAM,
      entityId: 'team-001',
      rpm: 200,
      tpm: 20000,
      concurrentRequests: 10,
      updatedAt: new Date().toISOString(),
    });
  });

  describe('Modal Rendering', () => {
    it('renders create modal when no editData provided', async () => {
      renderComponent();

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByRole('heading', { name: /create rate limit/i })).toBeInTheDocument();
    });

    it('renders edit modal when editData provided', async () => {
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          rpm: 100,
          tpm: 10000,
          concurrentRequests: 5,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByRole('heading', { name: /edit rate limit/i })).toBeInTheDocument();
    });

    it('does not render when closed', () => {
      renderComponent({ isOpen: false });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  describe('Form Fields', () => {
    it('shows form labels in create mode', async () => {
      renderComponent();

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Check for form element labels
      expect(screen.getByText('Entity Type')).toBeInTheDocument();
      expect(screen.getByText('Requests Per Minute (RPM)')).toBeInTheDocument();
      expect(screen.getByText('Tokens Per Minute (TPM)')).toBeInTheDocument();
      expect(screen.getByText('Concurrent Requests')).toBeInTheDocument();
    });

    it('shows entity values as read-only in edit mode', async () => {
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          rpm: 100,
          tpm: 10000,
          concurrentRequests: 5,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // In edit mode, entity type and ID are shown as static text
      expect(screen.getByText('team')).toBeInTheDocument();
      expect(screen.getByText('team-001')).toBeInTheDocument();
    });
  });

  describe('Edit Flow', () => {
    it('pre-fills form with edit data', async () => {
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          rpm: 100,
          tpm: 10000,
          concurrentRequests: 5,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Check that inputs are pre-filled
      expect(screen.getByDisplayValue('100')).toBeInTheDocument();
      expect(screen.getByDisplayValue('10000')).toBeInTheDocument();
      expect(screen.getByDisplayValue('5')).toBeInTheDocument();
    });

    it('submits edit form with updated data', async () => {
      const user = userEvent.setup();
      const onSuccess = vi.fn();
      renderComponent({
        onSuccess,
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          rpm: 100,
          tpm: 10000,
          concurrentRequests: 5,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Update RPM
      const rpmInput = screen.getByDisplayValue('100');
      await user.clear(rpmInput);
      await user.type(rpmInput, '200');

      // Submit the form
      const submitButton = screen.getByRole('button', { name: /save changes/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(mockUpdateRatelimit).toHaveBeenCalledWith(
          'org-001',
          EntityType.TEAM,
          'team-001',
          expect.objectContaining({
            rpm: 200,
          })
        );
      });
    });

    it('calls onSuccess after successful edit', async () => {
      const user = userEvent.setup();
      const onSuccess = vi.fn();
      renderComponent({
        onSuccess,
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          rpm: 100,
          tpm: 10000,
          concurrentRequests: 5,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Submit the form
      const submitButton = screen.getByRole('button', { name: /save changes/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(onSuccess).toHaveBeenCalled();
      });
    });
  });

  describe('Modal Actions', () => {
    it('calls onClose when cancel button is clicked', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      renderComponent({ onClose });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      const cancelButton = screen.getByRole('button', { name: /cancel/i });
      await user.click(cancelButton);

      expect(onClose).toHaveBeenCalled();
    });
  });

  /**
   * Issue #4948. The entity picker now lists every org the caller administers, which
   * makes the write partition a real choice rather than a coincidence.
   *
   * The limiter loads a config by `(org_id, entity_type, entity_id)`. Before this issue
   * both columns happened to agree because the picker could only ever offer the caller's
   * own org. Offering the full list breaks that coincidence: if the POST keeps going to
   * the caller's own `/organizations/{orgId}/ratelimits`, the row is stored where the
   * picked org's members are never looked up — configured on screen, inert in
   * production. That is worse than the gap this issue reports, so it is asserted here at
   * the wire and not only at the callback.
   */
  describe('Write partition follows the picked org (#4948)', () => {
    beforeEach(() => {
      mockGetOrganizations.mockResolvedValue({
        items: [
          { id: 'org-001', name: 'Acme Corp' },
          { id: 'sophos-it', name: 'Sophos IT' },
        ],
        total: 2,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });
      mockGetOrgTeams.mockResolvedValue({
        items: [{ id: 'team-sophos-1', name: 'Helpdesk', departmentId: 'dept-1' }],
        total: 1,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });
    });

    async function pickTeamInOtherOrgAndSubmit() {
      const user = userEvent.setup();
      renderComponent();

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

      const entityTypeSelect = screen.getAllByRole('combobox')[0];
      await user.selectOptions(entityTypeSelect, EntityType.TEAM);

      await waitFor(() => expect(screen.getByRole('option', { name: /Sophos IT/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[1], 'sophos-it');

      await waitFor(() => expect(screen.getByRole('option', { name: /Helpdesk/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[2], 'team-sophos-1');

      // The form rejects a submit with no limit set, so give it one.
      await user.type(screen.getByPlaceholderText('e.g., 60'), '100');

      await user.click(screen.getByRole('button', { name: /create rate limit/i }));
      return user;
    }

    it('posts to the picked org, not the caller\'s own', async () => {
      await pickTeamInOtherOrgAndSubmit();

      await waitFor(() => {
        expect(mockCreateRatelimit).toHaveBeenCalledWith(
          'sophos-it',
          expect.objectContaining({ entity_type: EntityType.TEAM, entity_id: 'team-sophos-1' }),
        );
      });
      expect(mockCreateRatelimit).not.toHaveBeenCalledWith('org-001', expect.anything());
    });

    it('returns to the roster org after switching a foreign team limit to a user limit', async () => {
      vi.mocked(getOrgUsers).mockResolvedValueOnce({
        items: [{ id: 'user-operator', email: 'operator@test.com', name: 'Operator',
          cognitoSub: 'sub-operator', role: 'member' }],
        total: 1, page: 1, pageSize: 100, hasMore: false,
      });
      const user = userEvent.setup();
      renderComponent();
      await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.TEAM);
      await screen.findByRole('option', { name: /Sophos IT/ });
      await user.selectOptions(screen.getAllByRole('combobox')[1], 'sophos-it');
      await screen.findByRole('option', { name: /Helpdesk/ });
      await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.USER);
      const person = await screen.findByRole('option', { name: /Operator/ });
      await user.selectOptions(screen.getAllByRole('combobox')[1], person);
      await user.type(screen.getByPlaceholderText('e.g., 60'), '100');
      await user.click(screen.getByRole('button', { name: /create rate limit/i }));

      await waitFor(() => expect(mockCreateRatelimit).toHaveBeenCalledWith(
        'org-001', expect.objectContaining({ entity_type: EntityType.USER, entity_id: 'sub-operator' }),
      ));
    });

    it('leaves a single-org admin posting to their own org', async () => {
      // The default path must be untouched: nobody who never opens the org picker should
      // see any change in where their config lands.
      const user = userEvent.setup();
      renderComponent();

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

      await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.TEAM);
      await waitFor(() => expect(screen.getByRole('option', { name: /Helpdesk/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[2], 'team-sophos-1');
      await user.type(screen.getByPlaceholderText('e.g., 60'), '100');

      await user.click(screen.getByRole('button', { name: /create rate limit/i }));

      await waitFor(() => {
        expect(mockCreateRatelimit).toHaveBeenCalledWith('org-001', expect.anything());
      });
    });

    it('does not carry a previously picked org into the next create', async () => {
      // The modal is reused. A partition left over from the last create would send the
      // next config somewhere the operator is no longer looking.
      const user = userEvent.setup();
      const { unmount } = render(
        <ToastProvider>
          <RateLimitFormModal isOpen onClose={vi.fn()} onSuccess={vi.fn()} orgId="org-001" />
        </ToastProvider>,
      );

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.TEAM);
      await waitFor(() => expect(screen.getByRole('option', { name: /Sophos IT/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[1], 'sophos-it');
      unmount();

      renderComponent();
      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.TEAM);
      await waitFor(() => expect(screen.getByRole('option', { name: /Helpdesk/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[2], 'team-sophos-1');
      await user.type(screen.getByPlaceholderText('e.g., 60'), '100');
      await user.click(screen.getByRole('button', { name: /create rate limit/i }));

      await waitFor(() => {
        expect(mockCreateRatelimit).toHaveBeenCalledWith('org-001', expect.anything());
      });
    });
  });

  describe('Error Handling', () => {
    it('handles update failure gracefully', async () => {
      mockUpdateRatelimit.mockRejectedValue(new Error('Update failed'));

      const user = userEvent.setup();
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          rpm: 100,
          tpm: 10000,
          concurrentRequests: 5,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Submit the form
      const submitButton = screen.getByRole('button', { name: /save changes/i });
      await user.click(submitButton);

      // Wait for the error to be handled
      await waitFor(() => {
        expect(mockUpdateRatelimit).toHaveBeenCalled();
      });
    });
  });
});
