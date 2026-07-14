/**
 * RegisterGitHubPat component tests.
 *
 * Issue #3389: PAT onboarding flow (C2 — vault UI).
 *
 * Coverage:
 *   - Form rendering (permission table, inputs, buttons)
 *   - Password input type and autocomplete=off (security attributes)
 *   - Submit flow (calls registerGitHubPat, clears input, calls onSuccess)
 *   - Error states (real {detail:{error,message}} shape — F1)
 *   - Disabled state during submission
 *   - Cancel behavior
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RegisterGitHubPat from '@/components/RegisterGitHubPat';

// Mock the credentials service
vi.mock('@/services/credentials', () => ({
  registerGitHubPat: vi.fn(),
  extractCredentialError: vi.fn((err: unknown) => {
    const detail = (err as { detail?: { error?: string; message?: string } | string })?.detail;
    if (detail && typeof detail === 'object') {
      return detail.message || detail.error || 'An unexpected error occurred';
    }
    if (typeof detail === 'string') return detail;
    const message = (err as { message?: string })?.message;
    if (message) return message;
    return 'An unexpected error occurred';
  }),
}));

import { registerGitHubPat } from '@/services/credentials';

const mockRegisterGitHubPat = registerGitHubPat as ReturnType<typeof vi.fn>;

describe('RegisterGitHubPat', () => {
  const onSuccess = vi.fn();
  const onCancel = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderComponent() {
    return render(<RegisterGitHubPat onSuccess={onSuccess} onCancel={onCancel} />);
  }

  // -------------------------------------------------------------------------
  // Form rendering
  // -------------------------------------------------------------------------

  describe('Form rendering', () => {
    it('renders the form title', () => {
      renderComponent();
      expect(screen.getByText('Register GitHub Personal Access Token')).toBeInTheDocument();
    });

    it('renders the permission guidance table with required permissions', () => {
      renderComponent();
      expect(screen.getByText('Contents')).toBeInTheDocument();
      expect(screen.getByText('Issues')).toBeInTheDocument();
      expect(screen.getByText('Pull requests')).toBeInTheDocument();
      expect(screen.getByText('Metadata')).toBeInTheDocument();
    });

    it('renders the PAT input field', () => {
      renderComponent();
      expect(screen.getByLabelText('Personal Access Token')).toBeInTheDocument();
    });

    it('renders the optional expiry date field', () => {
      renderComponent();
      expect(screen.getByLabelText(/Expiry Date/)).toBeInTheDocument();
    });

    it('renders the self-review limitation notice', () => {
      renderComponent();
      expect(screen.getByText(/Self-review limitation/)).toBeInTheDocument();
      expect(screen.getByText(/cannot approve your own PRs/)).toBeInTheDocument();
    });

    it('renders a link to GitHub PAT creation page', () => {
      renderComponent();
      const link = screen.getByRole('link', { name: /fine-grained PAT/ });
      expect(link).toHaveAttribute('href', 'https://github.com/settings/personal-access-tokens/new');
      expect(link).toHaveAttribute('target', '_blank');
      expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    });
  });

  // -------------------------------------------------------------------------
  // Security attributes
  // -------------------------------------------------------------------------

  describe('Security attributes', () => {
    it('uses password type for PAT input', () => {
      renderComponent();
      const input = screen.getByLabelText('Personal Access Token');
      expect(input).toHaveAttribute('type', 'password');
    });

    it('has autocomplete=off on PAT input', () => {
      renderComponent();
      const input = screen.getByLabelText('Personal Access Token');
      expect(input).toHaveAttribute('autocomplete', 'off');
    });
  });

  // -------------------------------------------------------------------------
  // Submit flow
  // -------------------------------------------------------------------------

  describe('Submit flow', () => {
    it('disables submit button when PAT is empty', () => {
      renderComponent();
      const submitBtn = screen.getByRole('button', { name: /Register PAT/ });
      expect(submitBtn).toBeDisabled();
    });

    it('enables submit button when PAT is entered', async () => {
      const user = userEvent.setup();
      renderComponent();

      await user.type(screen.getByLabelText('Personal Access Token'), 'github_pat_abc123');

      const submitBtn = screen.getByRole('button', { name: /Register PAT/ });
      expect(submitBtn).toBeEnabled();
    });

    it('calls registerGitHubPat with correct payload on submit', async () => {
      const user = userEvent.setup();
      mockRegisterGitHubPat.mockResolvedValueOnce({ id: 'cred-1', service: 'github', label: 'github-pat' });
      renderComponent();

      await user.type(screen.getByLabelText('Personal Access Token'), 'github_pat_abc123');
      await user.click(screen.getByRole('button', { name: /Register PAT/ }));

      await waitFor(() => {
        expect(mockRegisterGitHubPat).toHaveBeenCalledWith({
          pat: 'github_pat_abc123',
        });
      });
    });

    it('calls onSuccess after successful registration', async () => {
      const user = userEvent.setup();
      mockRegisterGitHubPat.mockResolvedValueOnce({ id: 'cred-1' });
      renderComponent();

      await user.type(screen.getByLabelText('Personal Access Token'), 'github_pat_abc123');
      await user.click(screen.getByRole('button', { name: /Register PAT/ }));

      await waitFor(() => {
        expect(onSuccess).toHaveBeenCalled();
      });
    });

    it('shows loading state during submission', async () => {
      const user = userEvent.setup();
      // Never resolves during this test
      mockRegisterGitHubPat.mockReturnValue(new Promise(() => {}));
      renderComponent();

      await user.type(screen.getByLabelText('Personal Access Token'), 'github_pat_abc123');
      await user.click(screen.getByRole('button', { name: /Register PAT/ }));

      expect(screen.getByRole('button', { name: /Registering/ })).toBeDisabled();
    });
  });

  // -------------------------------------------------------------------------
  // Error handling (F1: real error shape)
  // -------------------------------------------------------------------------

  describe('Error handling', () => {
    it('displays error from {detail:{error,message}} shape', async () => {
      const user = userEvent.setup();
      mockRegisterGitHubPat.mockRejectedValueOnce({
        detail: { error: 'duplicate_credential', message: 'A credential with this service and label already exists.' },
      });
      renderComponent();

      await user.type(screen.getByLabelText('Personal Access Token'), 'github_pat_abc123');
      await user.click(screen.getByRole('button', { name: /Register PAT/ }));

      await waitFor(() => {
        expect(screen.getByText('A credential with this service and label already exists.')).toBeInTheDocument();
      });
    });

    it('displays fallback for unknown error shapes', async () => {
      const user = userEvent.setup();
      mockRegisterGitHubPat.mockRejectedValueOnce(new Error('Network error'));
      renderComponent();

      await user.type(screen.getByLabelText('Personal Access Token'), 'github_pat_abc123');
      await user.click(screen.getByRole('button', { name: /Register PAT/ }));

      await waitFor(() => {
        expect(screen.getByText('Network error')).toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Cancel
  // -------------------------------------------------------------------------

  describe('Cancel', () => {
    it('calls onCancel when cancel button is clicked', async () => {
      const user = userEvent.setup();
      renderComponent();

      await user.click(screen.getByRole('button', { name: /Cancel/ }));

      expect(onCancel).toHaveBeenCalled();
    });
  });
});
