import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentModels from '@/pages/settings/AgentModels';
import * as personaModels from '@/services/personaModels';
import type {
  ManageableServicePrincipals,
  ModelCatalogue,
  PersonaCatalogue,
  PreferenceDetail,
  PreferenceList,
} from '@/services/personaModels';

vi.mock('@/services/personaModels', async (original) => ({
  ...await original<typeof import('@/services/personaModels')>(),
  getManageableServicePrincipals: vi.fn(),
  getPersonaCatalogue: vi.fn(),
  getPreferences: vi.fn(),
  getModelCatalogue: vi.fn(),
  setPreference: vi.fn(),
  resetPreference: vi.fn(),
}));

const personas: PersonaCatalogue = {
  personas: [
    {
      key: 'brand-new-persona',
      display_name: 'Brand New Persona',
      purpose: 'Proves personas are server-driven.',
      configurable: true,
      not_configurable_reason: null,
      compatibility_class: 'claude-agent-sdk',
    },
    {
      key: 'pt-superpower',
      display_name: 'PT Superpower',
      purpose: 'Runs penetration testing workflows.',
      configurable: false,
      not_configurable_reason: 'dispatches_without_persona_identity',
      compatibility_class: 'claude-agent-sdk',
    },
  ],
};

const preferences: PreferenceList = {
  principal_kind: 'human',
  principal_id: 'user-1',
  entries: [
    {
      persona_key: 'brand-new-persona',
      persona_display_name: 'Brand New Persona',
      configurable: true,
      effective_model_id: 'model-default',
      source: 'system-default',
      status: 'not-configured',
      saved_model_id: null,
      requested_alias: null,
      revision: null,
      updated_at: null,
    },
    {
      persona_key: 'pt-superpower',
      persona_display_name: 'PT Superpower',
      configurable: false,
      effective_model_id: 'model-default',
      source: 'system-default',
      status: 'not-configured',
      saved_model_id: null,
      requested_alias: null,
      revision: null,
      updated_at: null,
    },
  ],
};

const modelCatalogue: ModelCatalogue = {
  persona_key: 'brand-new-persona',
  compatibility_class: 'claude-agent-sdk',
  models: [
    {
      canonical_model_id: 'model-default',
      model_family: 'Sonnet',
      canonical_version: '4.6',
      selectable: false,
      reason: 'not_yet_certified',
      permitted: true,
      invocable: null,
      evidence: null,
      compatibility_class: 'claude-agent-sdk',
      harness_contract_revision: '2026-09-01',
      retired: false,
      price_context: null,
    },
    {
      canonical_model_id: 'model-disallowed',
      model_family: 'Opus',
      canonical_version: '4.6',
      selectable: false,
      reason: 'not_permitted',
      permitted: false,
      invocable: true,
      evidence: null,
      compatibility_class: 'claude-agent-sdk',
      harness_contract_revision: '2026-09-01',
      retired: false,
      price_context: null,
    },
    {
      canonical_model_id: 'model-certified',
      model_family: 'Haiku',
      canonical_version: '4.5',
      selectable: true,
      reason: null,
      permitted: true,
      invocable: true,
      evidence: {
        account_id: '123456789012',
        region: 'eu-west-1',
        verified_at: '2026-09-19T00:00:00Z',
        expires_at: '2026-09-26T00:00:00Z',
        stale: false,
      },
      compatibility_class: 'claude-agent-sdk',
      harness_contract_revision: '2026-09-01',
      retired: false,
      price_context: { input_per_million_tokens: 1, output_per_million_tokens: 5 },
    },
  ],
};

const noPrincipals: ManageableServicePrincipals = { principals: [] };

function detail(overrides: Partial<PreferenceDetail> = {}): PreferenceDetail {
  return {
    persona_key: 'brand-new-persona',
    effective_model_id: 'model-certified',
    source: 'principal-mapping',
    status: 'configured',
    saved_model_id: 'model-certified',
    requested_alias: 'model-certified',
    revision: 1,
    updated_at: '2026-09-19T01:00:00Z',
    default_model_id: 'model-default',
    default_source: 'claude-agent-sdk',
    ...overrides,
  };
}

describe('Agent Models page — issue #5422', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(personaModels.getManageableServicePrincipals).mockResolvedValue(noPrincipals);
    vi.mocked(personaModels.getPersonaCatalogue).mockResolvedValue(personas);
    vi.mocked(personaModels.getPreferences).mockResolvedValue(preferences);
    vi.mocked(personaModels.getModelCatalogue).mockResolvedValue(modelCatalogue);
    vi.mocked(personaModels.setPreference).mockResolvedValue(detail());
    vi.mocked(personaModels.resetPreference).mockResolvedValue(detail({
      effective_model_id: 'model-default',
      source: 'system-default',
      status: 'not-configured',
      saved_model_id: null,
      requested_alias: null,
      revision: null,
    }));
  });

  it('renders server-defined personas for an ordinary user and requests self only', async () => {
    render(<AgentModels />);

    expect(await screen.findByText('Brand New Persona')).toBeInTheDocument();
    expect(screen.getByText('Proves personas are server-driven.')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Configuration scope' })).not.toBeInTheDocument();
    expect(screen.getByTestId('not-configurable-pt-superpower')).toHaveTextContent('dispatches without persona identity');
    expect(personaModels.getPreferences).toHaveBeenCalledWith({ kind: 'self' }, expect.any(AbortSignal));
    expect(personaModels.getPreferences).toHaveBeenCalledTimes(1);
  });

  it('explains disabled choices and keeps uncertified distinct from unavailable', async () => {
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('Not yet certified')).toBeInTheDocument();
    expect(within(row).getByText('No certification probe has run.')).toBeInTheDocument();
    expect(within(row).getByText('Not permitted by your organization.')).toBeInTheDocument();
    expect(within(row).getByRole('radio', { name: /Sonnet 4.6/ })).toBeDisabled();
    expect(within(row).getByRole('radio', { name: /Opus 4.6/ })).toBeDisabled();
    expect(within(row).getByRole('radio', { name: /Haiku 4.5/ })).toBeEnabled();
  });

  it('saves and resets a row from server responses without optimistic state', async () => {
    render(<AgentModels />);
    const row = await screen.findByTestId('persona-row-brand-new-persona');
    fireEvent.click(within(row).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(personaModels.setPreference).toHaveBeenCalledWith(
      { kind: 'self' },
      'brand-new-persona',
      'model-certified',
      undefined,
    ));
    expect(await within(row).findByText('Personal mapping')).toBeInTheDocument();
    expect(within(row).getAllByText('model-certified')).toHaveLength(2);

    fireEvent.click(within(row).getByRole('button', { name: 'Reset' }));
    await waitFor(() => expect(personaModels.resetPreference).toHaveBeenCalledWith(
      { kind: 'self' },
      'brand-new-persona',
    ));
    expect(await within(row).findByText('Platform default')).toBeInTheDocument();
  });

  it('uses only a server-returned opaque ID for managed-service requests', async () => {
    vi.mocked(personaModels.getManageableServicePrincipals).mockResolvedValue({
      principals: [{
        canonical_principal_id: 'opaque/service:id',
        principal_kind: 'service_account',
        display_name: 'Nightly triage',
        tenant_label: 'Acme',
        source: 'github-actions',
        manageable: true,
      }],
    });
    render(<AgentModels />);

    const managed = await screen.findByRole('radio', { name: /Nightly triage \(Acme, github-actions\)/ });
    fireEvent.click(managed);
    await waitFor(() => expect(personaModels.getPreferences).toHaveBeenCalledWith(
      { kind: 'service', canonicalPrincipalId: 'opaque/service:id' },
      expect.any(AbortSignal),
    ));
    expect(await screen.findByText(/Changes below apply to Nightly triage in Acme/)).toBeInTheDocument();
  });

  it('does not replace known state after a refusal and offers an explicit conflict reload', async () => {
    vi.mocked(personaModels.setPreference)
      .mockRejectedValueOnce({ detail: { reason: 'not_invocable', message: 'Evidence expired.' } })
      .mockRejectedValueOnce({
        persona_key: 'brand-new-persona',
        current_model_id: 'model-other',
        current_revision: 2,
        effective_model_id: 'model-other',
        default_model_id: 'model-default',
      });
    render(<AgentModels />);
    const row = await screen.findByTestId('persona-row-brand-new-persona');
    const choice = within(row).getByRole('radio', { name: /Haiku 4.5/ });

    fireEvent.click(choice);
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));
    expect(await within(row).findByText(/Evidence expired\. Nothing was changed/)).toBeInTheDocument();
    expect(within(row).getByText('Platform default')).toBeInTheDocument();

    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));
    expect(await within(row).findByText(/changed elsewhere/)).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Reload current value' }));
    await waitFor(() => expect(personaModels.getPreferences).toHaveBeenCalledTimes(2));
  });

  it('renders an honest retry state without claiming a default', async () => {
    vi.mocked(personaModels.getPersonaCatalogue).mockRejectedValueOnce(new Error('catalogue offline'));
    render(<AgentModels />);

    expect(await screen.findByText(/catalogue offline/)).toBeInTheDocument();
    expect(screen.getByText(/not a statement that the platform default is in effect/)).toBeInTheDocument();
    expect(screen.queryByText('Platform default')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Brand New Persona')).toBeInTheDocument();
  });

  it('keeps stale evidence visible and never labels it verified', async () => {
    vi.mocked(personaModels.getModelCatalogue).mockResolvedValue({
      ...modelCatalogue,
      models: modelCatalogue.models.map((model) => model.canonical_model_id === 'model-default'
        ? {
            ...model,
            evidence: {
              account_id: '123456789012',
              region: 'eu-west-1',
              verified_at: '2026-09-01T00:00:00Z',
              expires_at: '2026-09-08T00:00:00Z',
              stale: true,
            },
          }
        : model),
    });
    render(<AgentModels />);
    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('Evidence stale')).toBeInTheDocument();
    expect(within(row).queryByText('Verified')).not.toBeInTheDocument();
  });

  it('keeps a successful row when a different row save is refused', async () => {
    const secondPersona = {
      ...personas.personas[0],
      key: 'reviewer',
      display_name: 'Reviewer',
      purpose: 'Reviews changes.',
    };
    vi.mocked(personaModels.getPersonaCatalogue).mockResolvedValue({
      personas: [personas.personas[0], secondPersona],
    });
    vi.mocked(personaModels.getPreferences).mockResolvedValue({
      ...preferences,
      entries: [
        preferences.entries[0],
        { ...preferences.entries[0], persona_key: 'reviewer', persona_display_name: 'Reviewer' },
      ],
    });
    vi.mocked(personaModels.getModelCatalogue).mockImplementation(async (key) => ({
      ...modelCatalogue,
      persona_key: key,
    }));
    vi.mocked(personaModels.setPreference)
      .mockResolvedValueOnce(detail())
      .mockRejectedValueOnce({ detail: { reason: 'not_permitted', message: 'Reviewer model is disallowed.' } });

    render(<AgentModels />);
    const first = await screen.findByTestId('persona-row-brand-new-persona');
    const second = await screen.findByTestId('persona-row-reviewer');

    fireEvent.click(within(first).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(first).getByRole('button', { name: 'Save' }));
    expect(await within(first).findByText('Personal mapping')).toBeInTheDocument();

    fireEvent.click(within(second).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(second).getByRole('button', { name: 'Save' }));
    expect(await within(second).findByText(/Reviewer model is disallowed/)).toBeInTheDocument();
    expect(within(first).getByText('Personal mapping')).toBeInTheDocument();
    expect(screen.queryByText(/all rows saved/i)).not.toBeInTheDocument();
  });
});
