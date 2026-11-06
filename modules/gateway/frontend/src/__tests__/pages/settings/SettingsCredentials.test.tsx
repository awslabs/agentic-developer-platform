import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import SettingsCredentials from '@/pages/settings/SettingsCredentials';
import { deleteCredential, listCredentials } from '@/services/credentials';

vi.mock('@/services/credentials', () => ({
  listCredentials: vi.fn(), deleteCredential: vi.fn(),
  extractCredentialError: (error: Error) => error.message,
  getToolsConsent: vi.fn(), setCredentialAgentConsent: vi.fn(), setToolsConsent: vi.fn(),
}));
vi.mock('@/components/aws/BedrockAccountSelector', () => ({ BedrockAccountSelector: () => null }));

const pat = {
  id: 'legacy-pat', service: 'github', label: 'github-pat', credential_type: 'bearer',
  scope: 'user', scopes: null, expires_at: null, last_used_at: null,
  strict: true, agent_consent: false, created_at: '2026-01-01', updated_at: null,
};
function renderPage() {
  return render(<MemoryRouter><SettingsCredentials /></MemoryRouter>);
}

describe('Credentials and service connections', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listCredentials).mockResolvedValue([pat]);
  });

  it('keeps any stored bearer credential manageable in the ordinary list', async () => {
    renderPage();
    await screen.findByText('Other Credentials');
    expect(screen.queryByText('Legacy GitHub tokens')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Connections' })).toHaveAttribute('href', '/settings/connections');
    expect(screen.queryByRole('button', { name: /Connect GitHub|Migrate/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove' })).toBeVisible();
    expect(screen.getByRole('checkbox', { name: 'Available to agents: github-pat' })).toBeVisible();
    expect(deleteCredential).not.toHaveBeenCalled();
  });

  it('does not offer tokens or duplicate connection controls to new users', async () => {
    vi.mocked(listCredentials).mockResolvedValue([]);
    renderPage();
    await screen.findByText(/No credentials registered/);
    expect(screen.queryByText('Legacy GitHub tokens')).not.toBeInTheDocument();
    expect(screen.queryByText('GitHub Personal Access Token')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /GitHub/ })).not.toBeInTheDocument();
  });

  it('manages OAuth on Connections without deleting another stored credential', async () => {
    vi.mocked(listCredentials).mockResolvedValue([
      { ...pat, id: 'user-oauth', label: 'AgentCore OAuth', credential_type: 'oauth_token', provider: 'agentcore-identity' }, pat,
    ]);
    renderPage();
    await screen.findByText('Other Credentials');
    expect(screen.getByText('github-pat')).toBeVisible();
    expect(screen.queryByText('AgentCore OAuth')).not.toBeInTheDocument();
    expect(screen.queryByText(/Remove your old PAT when ready/)).not.toBeInTheDocument();
    expect(deleteCredential).not.toHaveBeenCalled();
  });

  it('removes only the explicitly selected credential', async () => {
    vi.mocked(listCredentials).mockResolvedValue([pat, { ...pat, id: 'second-pat' }]);
    vi.mocked(deleteCredential).mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    await screen.findByText('Other Credentials');
    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]);
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(1));
    expect(deleteCredential).toHaveBeenCalledExactlyOnceWith('legacy-pat');
    confirm.mockRestore();
  });
});
