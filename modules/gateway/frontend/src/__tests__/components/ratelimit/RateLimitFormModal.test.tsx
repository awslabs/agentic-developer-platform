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
vi.mock('@/services/admin', () => ({
  getDepartments: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  getTeams: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
}));

import { createRatelimit, updateRatelimit } from '@/services/ratelimit';

const mockCreateRatelimit = createRatelimit as ReturnType<typeof vi.fn>;
const mockUpdateRatelimit = updateRatelimit as ReturnType<typeof vi.fn>;

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
