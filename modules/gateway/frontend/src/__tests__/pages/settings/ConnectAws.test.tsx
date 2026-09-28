/**
 * ConnectAws page component tests.
 *
 * Issue #562: Self-serve AWS account connect UI.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ConnectAws from '@/pages/settings/ConnectAws';

// Mock the credentials service
vi.mock('@/services/credentials', () => ({
  startAwsConnect: vi.fn(),
  verifyAwsConnect: vi.fn(),
  listCredentials: vi.fn(),
  deleteCredential: vi.fn(),
}));

import { startAwsConnect, verifyAwsConnect } from '@/services/credentials';

const mockStartAwsConnect = startAwsConnect as ReturnType<typeof vi.fn>;
const mockVerifyAwsConnect = verifyAwsConnect as ReturnType<typeof vi.fn>;

// Mock window.open
const mockWindowOpen = vi.fn();
Object.defineProperty(window, 'open', { value: mockWindowOpen });

function renderConnectAws() {
  return render(
    <MemoryRouter initialEntries={['/settings/credentials/aws/connect']}>
      <ConnectAws />
    </MemoryRouter>,
  );
}

describe('ConnectAws Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('Form validation', () => {
    it('renders the form with all fields', () => {
      renderConnectAws();

      expect(screen.getByText('Connect an AWS Account')).toBeInTheDocument();
      expect(screen.getByLabelText(/Nickname/)).toBeInTheDocument();
      expect(screen.getByLabelText(/AWS Account ID/)).toBeInTheDocument();
      expect(screen.getByLabelText(/Role Name/)).toBeInTheDocument();
    });

    it('shows validation error for invalid account ID', async () => {
      const user = userEvent.setup();
      renderConnectAws();

      const accountInput = screen.getByLabelText(/AWS Account ID/);
      await user.type(accountInput, '12345');

      expect(screen.getByText('AWS account IDs are 12 digits')).toBeInTheDocument();
    });

    it('disables launch button when form is incomplete', () => {
      renderConnectAws();

      const launchBtn = screen.getByRole('button', { name: /Launch CloudFormation/i });
      expect(launchBtn).toBeDisabled();
    });

    it('enables launch button when form is valid', async () => {
      const user = userEvent.setup();
      renderConnectAws();

      await user.type(screen.getByLabelText(/Nickname/), 'my-role');
      await user.type(screen.getByLabelText(/AWS Account ID/), '123456789012');

      const launchBtn = screen.getByRole('button', { name: /Launch CloudFormation/i });
      expect(launchBtn).toBeEnabled();
    });
  });

  describe('Launch flow', () => {
    it('calls startAwsConnect and opens URL on launch', async () => {
      const user = userEvent.setup();
      mockStartAwsConnect.mockResolvedValue({
        credential_id: 'cred-123',
        launch_url: 'https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?stackName=test',
      });

      renderConnectAws();

      await user.type(screen.getByLabelText(/Nickname/), 'prod');
      await user.type(screen.getByLabelText(/AWS Account ID/), '111222333444');
      await user.click(screen.getByRole('button', { name: /Launch CloudFormation/i }));

      await waitFor(() => {
        expect(mockStartAwsConnect).toHaveBeenCalledWith({
          nickname: 'prod',
          account_id: '111222333444',
          role_name: 'ADP-Agent-Role',
        });
      });

      expect(mockWindowOpen).toHaveBeenCalledWith(
        expect.stringContaining('console.aws.amazon.com'),
        '_blank',
      );
    });

    it('shows verify button after successful launch', async () => {
      const user = userEvent.setup();
      mockStartAwsConnect.mockResolvedValue({
        credential_id: 'cred-123',
        launch_url: 'https://example.com/cfn',
      });

      renderConnectAws();

      await user.type(screen.getByLabelText(/Nickname/), 'test');
      await user.type(screen.getByLabelText(/AWS Account ID/), '123456789012');
      await user.click(screen.getByRole('button', { name: /Launch CloudFormation/i }));

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Verify & Save/i })).toBeInTheDocument();
      });
    });
  });

  describe('Verify flow', () => {
    it('calls verifyAwsConnect and shows success', async () => {
      const user = userEvent.setup();
      mockStartAwsConnect.mockResolvedValue({
        credential_id: 'cred-123',
        launch_url: 'https://example.com/cfn',
      });
      mockVerifyAwsConnect.mockResolvedValue({ status: 'verified' });

      renderConnectAws();

      // Fill form and launch
      await user.type(screen.getByLabelText(/Nickname/), 'test');
      await user.type(screen.getByLabelText(/AWS Account ID/), '123456789012');
      await user.click(screen.getByRole('button', { name: /Launch CloudFormation/i }));

      // Wait for verify button and click
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Verify & Save/i })).toBeInTheDocument();
      });
      await user.click(screen.getByRole('button', { name: /Verify & Save/i }));

      await waitFor(() => {
        expect(mockVerifyAwsConnect).toHaveBeenCalledWith({ credential_id: 'cred-123' });
      });

      await waitFor(() => {
        expect(screen.getByText(/Verified! AWS account connected/)).toBeInTheDocument();
      });
    });

    it('shows failure reason when verify fails', async () => {
      const user = userEvent.setup();
      mockStartAwsConnect.mockResolvedValue({
        credential_id: 'cred-456',
        launch_url: 'https://example.com/cfn',
      });
      mockVerifyAwsConnect.mockResolvedValue({
        status: 'failed',
        reason: 'The IAM role has not been created yet.',
      });

      renderConnectAws();

      await user.type(screen.getByLabelText(/Nickname/), 'test');
      await user.type(screen.getByLabelText(/AWS Account ID/), '123456789012');
      await user.click(screen.getByRole('button', { name: /Launch CloudFormation/i }));

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /Verify & Save/i })).toBeInTheDocument();
      });
      await user.click(screen.getByRole('button', { name: /Verify & Save/i }));

      await waitFor(() => {
        expect(screen.getByText(/The IAM role has not been created yet/)).toBeInTheDocument();
      });

      // Should show retry button
      expect(screen.getByRole('button', { name: /Retry Verify/i })).toBeInTheDocument();
    });
  });
});
