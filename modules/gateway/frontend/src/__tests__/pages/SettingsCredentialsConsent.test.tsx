import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import SettingsCredentials from '@/pages/settings/SettingsCredentials';
import {
  getToolsConsent, listCredentials, setCredentialAgentConsent, setToolsConsent, setToolsConsentDefault,
  type CredentialItem,
} from '@/services/credentials';

vi.mock('@/services/credentials', () => ({
  listCredentials: vi.fn(),
  deleteCredential: vi.fn(),
  extractCredentialError: () => 'Save failed',
  getToolsConsent: vi.fn(),
  setCredentialAgentConsent: vi.fn(),
  setToolsConsent: vi.fn(),
  setToolsConsentDefault: vi.fn(),
}));
vi.mock('@/components/aws/AwsConnectionRow', () => ({
  AwsConnectionRow: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock('@/components/aws/BedrockAccountSelector', () => ({ BedrockAccountSelector: () => null }));

const credential: CredentialItem = {
  id: 'cred-1', label: 'Production role', service: 'aws', credential_type: 'aws_role',
  scope: 'user', scopes: null, expires_at: null, last_used_at: null,
  strict: false, agent_consent: false, created_at: '2026-01-01T00:00:00Z', updated_at: null,
};

function showSettings() {
  return render(<MemoryRouter><SettingsCredentials /></MemoryRouter>);
}

describe('agent consent settings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listCredentials).mockResolvedValue([credential]);
    vi.mocked(getToolsConsent).mockResolvedValue({
      allowed_tools: ['websearch.search'], consented_tools: [], revision: 1,
    });
  });

  it('renders saved credential consent and persists changes from the response', async () => {
    vi.mocked(setCredentialAgentConsent).mockResolvedValueOnce({ ...credential, agent_consent: true });
    const user = userEvent.setup();
    showSettings();
    const toggle = await screen.findByRole('checkbox', { name: 'Available to agents: Production role' });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(toggle).toBeChecked());
    expect(setCredentialAgentConsent).toHaveBeenCalledWith('cred-1', true);
  });

  it('renders entitled tools and saves only selected consent', async () => {
    vi.mocked(setToolsConsent).mockResolvedValueOnce({
      allowed_tools: ['websearch.search'], consented_tools: ['websearch.search'], revision: 2,
    });
    const user = userEvent.setup();
    showSettings();
    await user.click(screen.getByRole('tab', { name: 'Tools' }));
    const toggle = await screen.findByRole('checkbox', { name: 'Available to agents: websearch.search' });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(toggle).toBeChecked());
    expect(setToolsConsent).toHaveBeenCalledWith(['websearch.search'], 1);
  });

  it('does not display a rejected credential choice as saved', async () => {
    vi.mocked(setCredentialAgentConsent).mockRejectedValueOnce(new Error('save failed'));
    const user = userEvent.setup();
    showSettings();
    const toggle = await screen.findByRole('checkbox', { name: 'Available to agents: Production role' });
    await user.click(toggle);
    await screen.findByText('Save failed');
    expect(toggle).not.toBeChecked();
  });

  it('does not display a rejected tool choice as saved', async () => {
    vi.mocked(setToolsConsent).mockRejectedValueOnce(new Error('save failed'));
    const user = userEvent.setup();
    showSettings();
    await user.click(screen.getByRole('tab', { name: 'Tools' }));
    const toggle = await screen.findByRole('checkbox', { name: 'Available to agents: websearch.search' });
    await user.click(toggle);
    await screen.findByText('Save failed');
    expect(toggle).not.toBeChecked();
  });

  it('reloads the observed consent revision after a concurrent edit', async () => {
    vi.mocked(setToolsConsent).mockRejectedValueOnce({ status: 409, detail: 'tool policy changed; reload and retry' });
    vi.mocked(getToolsConsent).mockResolvedValueOnce({ allowed_tools: ['websearch.search'], consented_tools: [], revision: 1 })
      .mockResolvedValueOnce({ allowed_tools: ['websearch.search'], consented_tools: ['websearch.search'], revision: 2 });
    const user = userEvent.setup();
    showSettings();
    await user.click(screen.getByRole('tab', { name: 'Tools' }));
    const toggle = await screen.findByRole('checkbox', { name: 'Available to agents: websearch.search' });
    await user.click(toggle);
    await waitFor(() => expect(getToolsConsent).toHaveBeenCalledTimes(2));
    expect(toggle).toBeChecked();
    expect(setToolsConsent).toHaveBeenCalledWith(['websearch.search'], 1);
    expect(screen.getByText('Save failed')).toBeInTheDocument();
  });
});

describe('organization-default consent (#8387)', () => {
  const granted = {
    allowed_tools: ['websearch.search', 'browser.start'], consented_tools: ['websearch.search', 'browser.start'], revision: 3,
    consent_mode: 'default' as const, opted_out_tools: [], tenant_consent_default: 'granted' as const,
    entitled_tools: ['websearch.search', 'browser.start'],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listCredentials).mockResolvedValue([credential]);
  });

  it('shows entitled tools on and records a switch-off as an opt-out', async () => {
    vi.mocked(getToolsConsent).mockResolvedValue(granted);
    vi.mocked(setToolsConsentDefault).mockResolvedValueOnce({ ...granted, consented_tools: ['websearch.search'], opted_out_tools: ['browser.start'], revision: 4 });
    const user = userEvent.setup();
    showSettings();
    await user.click(screen.getByRole('tab', { name: 'Tools' }));
    const browser = await screen.findByRole('checkbox', { name: 'Available to agents: browser.start' });
    expect(browser).toBeChecked();
    expect(screen.getByText(/on by default/)).toBeInTheDocument();
    await user.click(browser);
    await waitFor(() => expect(browser).not.toBeChecked());
    expect(setToolsConsentDefault).toHaveBeenCalledWith(['browser.start'], 3);
    expect(setToolsConsent).not.toHaveBeenCalled();
  });

  it('switches to explicit management keeping what is on today, and back to the organization default', async () => {
    vi.mocked(getToolsConsent).mockResolvedValue(granted);
    vi.mocked(setToolsConsent).mockResolvedValueOnce({ ...granted, consent_mode: 'explicit', revision: 4 });
    vi.mocked(setToolsConsentDefault).mockResolvedValueOnce({ ...granted, revision: 5 });
    const user = userEvent.setup();
    showSettings();
    await user.click(screen.getByRole('tab', { name: 'Tools' }));
    await user.click(await screen.findByRole('button', { name: 'Manage consent explicitly' }));
    await waitFor(() => expect(setToolsConsent).toHaveBeenCalledWith(['websearch.search', 'browser.start'], 3));
    const back = await screen.findByRole('button', { name: 'Use organization defaults' });
    await user.click(back);
    await waitFor(() => expect(setToolsConsentDefault).toHaveBeenCalledWith([], 4));
    expect(await screen.findByRole('button', { name: 'Manage consent explicitly' })).toBeInTheDocument();
  });

  it('offers no organization default when the tenant requires explicit consent', async () => {
    vi.mocked(getToolsConsent).mockResolvedValue({
      allowed_tools: ['websearch.search'], consented_tools: [], revision: 1,
      consent_mode: 'explicit', opted_out_tools: [], tenant_consent_default: 'explicit', entitled_tools: ['websearch.search'],
    });
    const user = userEvent.setup();
    showSettings();
    await user.click(screen.getByRole('tab', { name: 'Tools' }));
    await screen.findByRole('checkbox', { name: 'Available to agents: websearch.search' });
    expect(screen.queryByRole('button', { name: /organization defaults|consent explicitly/ })).not.toBeInTheDocument();
  });
});
