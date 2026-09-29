import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { GitHubAppImportGuide } from '@/pages/settings/components/GitHubAppImportGuide';
import { getGitHubAppSetupGuide } from '@/services/connections';
vi.mock('@/services/connections', () => ({ getGitHubAppSetupGuide: vi.fn() }));
const guide = {
  homepage_url: 'https://preprod.example', callback_url: 'https://broker.example/callback',
  setup_url: 'https://preprod.example/install', webhook_url: 'https://hooks.example/github',
  permissions: { members: 'read', contents: 'write' }, events: ['pull_request_review'],
};
beforeEach(() => { vi.resetAllMocks(); });
describe('GitHub App import guide', () => {
  it('loads on demand and copies the server-provided callback', async () => {
    const user = userEvent.setup();
    vi.mocked(getGitHubAppSetupGuide).mockResolvedValue(guide);
    render(<GitHubAppImportGuide />);
    expect(getGitHubAppSetupGuide).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'How to configure your GitHub App' }));
    expect(await screen.findByLabelText('Callback URL')).toHaveValue(guide.callback_url);
    expect(screen.getByText('Organization → Members')).toBeInTheDocument();
    expect(screen.getByText('Pull request review')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Copy Callback URL' }));
    expect(await navigator.clipboard.readText()).toBe(guide.callback_url);
    expect(screen.getByText('Callback URL copied.')).toBeInTheDocument();
  });
  it('follows GitHub registration field order and explains both redirects', async () => {
    const user = userEvent.setup();
    vi.mocked(getGitHubAppSetupGuide).mockResolvedValue(guide);
    render(<GitHubAppImportGuide />);
    await user.click(screen.getByRole('button', { name: 'How to configure your GitHub App' }));
    const fields = await screen.findByRole('list', { name: 'GitHub App setup fields' });
    const names = Array.from(fields.children).map(field => field.querySelector('strong, label')?.textContent);
    expect(names).toEqual([
      'GitHub App name', 'Description', 'Homepage URL', 'Callback URL',
      'Expire user authorization tokens', 'Request user authorization (OAuth) during installation',
      'Enable Device Flow', 'Setup URL', 'Redirect on update', 'Webhook → Active',
      'Webhook URL', 'Webhook secret', 'SSL verification', 'Permissions',
      'Subscribe to events', 'Where can this GitHub App be installed?', 'Create GitHub App',
    ]);
    expect(screen.getByLabelText('Callback URL')).toHaveAccessibleDescription(/OAuth redirect URI/);
    expect(screen.getByLabelText('Setup URL')).toHaveAccessibleDescription(/connect the GitHub installation to their workspace/);
  });
  it('retries failed reads and never invents missing URLs', async () => {
    const user = userEvent.setup();
    vi.mocked(getGitHubAppSetupGuide).mockRejectedValueOnce(new Error('Unavailable'))
      .mockResolvedValueOnce({ ...guide, webhook_url: '' });
    render(<GitHubAppImportGuide />);
    await user.click(screen.getByRole('button', { name: 'How to configure your GitHub App' }));
    await user.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(await screen.findByText(/Not available. Ask your deployment administrator/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Copy Webhook URL' })).not.toBeInTheDocument();
  });
});
