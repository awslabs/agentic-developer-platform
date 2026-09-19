import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentModels from '@/pages/settings/AgentModels';
import * as selfApi from '@/services/personaModelsSelf';
import * as adminApi from '@/services/personaModelsAdmin';
import type {
  ManageableServicePrincipals,
  ModelCatalogue,
  PersonaCatalogue,
  PreferenceDetail,
  PreferenceList,
} from '@/services/personaModels';

vi.mock('@/services/personaModelsSelf', async (original) => ({
  ...await original<typeof import('@/services/personaModelsSelf')>(),
  getManageableServicePrincipals: vi.fn(),
  getPersonaCatalogue: vi.fn(),
  getPreferences: vi.fn(),
  getModelCatalogue: vi.fn(),
  setPreference: vi.fn(),
  resetPreference: vi.fn(),
}));

vi.mock('@/services/personaModelsAdmin', async (original) => ({
  ...await original<typeof import('@/services/personaModelsAdmin')>(),
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
      compatibility_class: 'claude-agent-sdk',
      harness_contract_revision: '2026-09-15',
      effective_model_id: 'model-default',
      effective_is_candidate: true,
      class_default_status: 'candidate',
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
      compatibility_class: 'claude-agent-sdk',
      harness_contract_revision: '2026-09-15',
      effective_model_id: 'model-default',
      effective_is_candidate: true,
      class_default_status: 'candidate',
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
      aliases: ['claude-sonnet-4-6'],
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
      price_context: { input_per_million_tokens: 2, output_per_million_tokens: 10 },
    },
    {
      canonical_model_id: 'model-disallowed',
      aliases: ['claude-opus-4-6'],
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
      aliases: ['claude-haiku-4-5'],
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
const managedPrincipal = {
  canonical_service_principal_id: 'service-1',
  principal_kind: 'service_account' as const,
  display_name: 'Nightly triage',
  tenant_label: 'Acme',
  source: 'github-actions',
  manageable: true,
};

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function detail(overrides: Partial<PreferenceDetail> = {}): PreferenceDetail {
  return {
    persona_key: 'brand-new-persona',
    compatibility_class: 'claude-agent-sdk',
    harness_contract_revision: '2026-09-15',
    effective_model_id: 'model-certified',
    effective_is_candidate: false,
    // A saved mapping is effective, so the server reports no class-default proof
    // state for it — the default's state says nothing about the person's choice.
    class_default_status: null,
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
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue(noPrincipals);
    vi.mocked(selfApi.getPersonaCatalogue).mockResolvedValue(personas);
    vi.mocked(selfApi.getPreferences).mockResolvedValue(preferences);
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
    }));
    vi.mocked(selfApi.setPreference).mockResolvedValue(detail());
    vi.mocked(selfApi.resetPreference).mockResolvedValue(detail({
      effective_model_id: 'model-default',
      effective_is_candidate: true,
      class_default_status: 'candidate',
      source: 'system-default',
      status: 'not-configured',
      saved_model_id: null,
      requested_alias: null,
      revision: null,
    }));
    vi.mocked(adminApi.getPreferences).mockResolvedValue(preferences);
    vi.mocked(adminApi.getModelCatalogue).mockImplementation(async (_principalId, personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
    }));
    vi.mocked(adminApi.setPreference).mockResolvedValue(detail());
    vi.mocked(adminApi.resetPreference).mockResolvedValue(detail({
      effective_model_id: 'model-default',
      effective_is_candidate: true,
      class_default_status: 'candidate',
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
    expect(selfApi.getPreferences).toHaveBeenCalledWith(expect.any(AbortSignal));
    expect(selfApi.getPreferences).toHaveBeenCalledTimes(1);
    expect(selfApi.getModelCatalogue).toHaveBeenCalledWith(
      'pt-superpower',
      expect.any(AbortSignal),
    );
    // Self path never touches the admin module
    expect(adminApi.getPreferences).not.toHaveBeenCalled();
    expect(adminApi.getModelCatalogue).not.toHaveBeenCalled();
    const nonConfigurable = screen.getByTestId('persona-row-pt-superpower');
    expect(within(nonConfigurable).getByText('Sonnet 4.6')).toBeInTheDocument();
    // Effective provenance comes from the preference response, not the model-catalogue row's 2026-09-01 revision.
    expect(within(nonConfigurable).getByText('Harness revision 2026-09-15')).toBeInTheDocument();
    expect(within(nonConfigurable).getByText('$2 input / $10 output per 1M tokens')).toBeInTheDocument();
    expect(within(nonConfigurable).getByText('Not yet certified')).toBeInTheDocument();
    expect(within(nonConfigurable).getByText('claude-agent-sdk class default (candidate)')).toBeInTheDocument();
    expect(screen.queryByText(/platform default/i)).not.toBeInTheDocument();
  });

  it('reports an absent class default as unconfigured instead of proven', async () => {
    // Approved design decision 4: a missing class default is an actionable platform
    // readiness gap, never a fallback and never describable as proven. The server
    // signals it with class_default_status null AND no effective model. Deriving
    // proof from effective_is_candidate (false here) reported this as "proven".
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: preferences.entries.map((entry) => ({
        ...entry,
        effective_model_id: null,
        effective_is_candidate: false,
        class_default_status: null,
        source: 'system-default' as const,
        status: 'not-configured' as const,
      })),
    });
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByTestId('effective-source-brand-new-persona')).toHaveTextContent(
      'No claude-agent-sdk class default is configured',
    );
    // It must not claim any proof state for a default that does not exist.
    expect(within(row).queryByText(/class default \(proven\)/)).not.toBeInTheDocument();
    expect(within(row).queryByText(/class default \(candidate\)/)).not.toBeInTheDocument();
    expect(screen.queryByText(/platform default/i)).not.toBeInTheDocument();
    expect(within(row).getByText('No effective model')).toBeInTheDocument();
  });

  it('does not upgrade an unstated class-default proof state to proven', async () => {
    // A default model is in effect but the server did not state its proof state.
    // The honest rendering is uncertainty, not "proven".
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: preferences.entries.map((entry) => ({
        ...entry,
        effective_is_candidate: false,
        class_default_status: undefined,
      })),
    });
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByTestId('effective-source-brand-new-persona')).toHaveTextContent(
      'claude-agent-sdk class default (proof state unknown)',
    );
    expect(within(row).queryByText(/class default \(proven\)/)).not.toBeInTheDocument();
  });

  it('keys the default label to each persona own compatibility class', async () => {
    // Defaults are per compatibility class, so two personas in different classes must
    // not share one label. A single platform-wide default label is forbidden.
    vi.mocked(selfApi.getPersonaCatalogue).mockResolvedValue({
      personas: [
        { ...personas.personas[0] },
        {
          ...personas.personas[1],
          key: 'codex-persona',
          display_name: 'Codex Persona',
          configurable: true,
          not_configurable_reason: null,
          compatibility_class: 'codex-sdk',
        },
      ],
    });
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: [
        { ...preferences.entries[0], class_default_status: 'proven' as const, effective_is_candidate: false },
        {
          ...preferences.entries[1],
          persona_key: 'codex-persona',
          persona_display_name: 'Codex Persona',
          configurable: true,
          compatibility_class: 'codex-sdk',
          class_default_status: 'candidate' as const,
          effective_is_candidate: true,
        },
      ],
    });
    render(<AgentModels />);

    await screen.findByTestId('persona-row-brand-new-persona');
    expect(screen.getByTestId('effective-source-brand-new-persona')).toHaveTextContent(
      'claude-agent-sdk class default (proven)',
    );
    expect(screen.getByTestId('effective-source-codex-persona')).toHaveTextContent(
      'codex-sdk class default (candidate)',
    );
    expect(screen.queryByText(/platform default/i)).not.toBeInTheDocument();
  });

  it('renders a server-proven default distinctly from a candidate default', async () => {
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: preferences.entries.map((entry) => ({
        ...entry,
        effective_is_candidate: false,
        class_default_status: 'proven' as const,
      })),
    });
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
      models: modelCatalogue.models.map((model) => model.canonical_model_id === 'model-default'
        ? {
            ...model,
            selectable: true,
            reason: null,
            invocable: true,
            evidence: {
              account_id: '123456789012',
              region: 'eu-west-1',
              verified_at: '2026-09-19T00:00:00Z',
              expires_at: '2026-09-26T00:00:00Z',
              stale: false,
            },
          }
        : model),
    }));
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('claude-agent-sdk class default (proven)')).toBeInTheDocument();
    expect(within(row).getByText('Verified')).toBeInTheDocument();
    const provenance = within(row).getByTestId('evidence-provenance');
    const timeElements = provenance.querySelectorAll('time');
    expect(timeElements).toHaveLength(2);
    expect(timeElements[0].getAttribute('datetime')).toBe('2026-09-19T00:00:00Z');
    expect(timeElements[1].getAttribute('datetime')).toBe('2026-09-26T00:00:00Z');
    expect(within(row).queryByText(/class default \(candidate\)/)).not.toBeInTheDocument();
    expect(screen.queryByText(/platform default/i)).not.toBeInTheDocument();
  });

  it('treats a top-level access_denied managed-principal response as an ordinary-user result', async () => {
    vi.mocked(selfApi.getManageableServicePrincipals).mockRejectedValue({
      error: 'access_denied',
      message: 'Human administration capability is not present.',
    });
    render(<AgentModels />);

    expect(await screen.findByText('Brand New Persona')).toBeInTheDocument();
    await waitFor(() => expect(selfApi.getManageableServicePrincipals).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('group', { name: 'Configuration scope' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Managed service accounts could not be loaded/)).not.toBeInTheDocument();
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

  it('never labels a retired effective model with fresh evidence as verified', async () => {
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
      models: modelCatalogue.models.map((model) => model.canonical_model_id === 'model-default'
        ? {
            ...model,
            retired: true,
            reason: 'retired',
            selectable: false,
            invocable: true,
            evidence: {
              account_id: '123456789012',
              region: 'eu-west-1',
              verified_at: '2026-09-19T00:00:00Z',
              expires_at: '2026-09-26T00:00:00Z',
              stale: false,
            },
          }
        : model),
    }));
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('Retired')).toBeInTheDocument();
    expect(within(row).queryByText('Verified')).not.toBeInTheDocument();
  });

  it('uses preference and policy state instead of calling a disallowed mapping uncertified', async () => {
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: preferences.entries.map((entry) => entry.persona_key === 'brand-new-persona'
        ? { ...entry, effective_model_id: 'model-disallowed', status: 'disallowed' }
        : entry),
    });
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('Not permitted')).toBeInTheDocument();
    expect(within(row).queryByText('Not yet certified')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'No model has been certified yet' })).not.toBeInTheDocument();
  });

  it('does not claim certification is missing when every model is blocked for a different reason', async () => {
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
      models: [
        { ...modelCatalogue.models[0], selectable: false, permitted: false, reason: 'not_permitted' },
        { ...modelCatalogue.models[1], selectable: false, permitted: true, retired: true, reason: 'retired' },
        { ...modelCatalogue.models[2], selectable: false, permitted: true, invocable: false, reason: 'not_invocable' },
      ],
    }));
    render(<AgentModels />);

    expect(await screen.findByText('Brand New Persona')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'No model has been certified yet' })).not.toBeInTheDocument();
  });

  it('shows the certification alert only when eligible models are genuinely unproven', async () => {
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
      models: [{
        ...modelCatalogue.models[0],
        selectable: false,
        permitted: true,
        invocable: null,
        retired: false,
        reason: 'probing_disabled',
        evidence: null,
      }],
    }));
    render(<AgentModels />);

    expect(await screen.findByRole('heading', { name: 'No model has been certified yet' })).toBeInTheDocument();
  });

  it('renders stale evidence timestamps without claiming that certification never happened', async () => {
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: preferences.entries.map((entry) => ({
        ...entry,
        effective_model_id: 'model-stale',
        effective_is_candidate: false,
        status: 'stale',
      })),
    });
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
      models: [{
        ...modelCatalogue.models[0],
        canonical_model_id: 'model-stale',
        selectable: false,
        permitted: true,
        invocable: true,
        retired: false,
        reason: 'evidence_stale',
        evidence: {
          account_id: '123456789012',
          region: 'eu-west-1',
          verified_at: '2026-09-01T12:00:00Z',
          expires_at: '2026-09-08T12:00:00Z',
          stale: true,
        },
      }],
    }));
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('Evidence stale')).toBeInTheDocument();
    const provenance = within(row).getByTestId('evidence-provenance');
    const timeElements = provenance.querySelectorAll('time');
    expect(timeElements).toHaveLength(2);
    expect(timeElements[0].getAttribute('datetime')).toBe('2026-09-01T12:00:00Z');
    expect(timeElements[1].getAttribute('datetime')).toBe('2026-09-08T12:00:00Z');
    expect(provenance).toHaveTextContent(/Last verified/);
    expect(provenance).toHaveTextContent(/expired/);
    expect(screen.queryByRole('heading', { name: 'No model has been certified yet' })).not.toBeInTheDocument();
  });

  it('saves and resets a row from server responses without optimistic state', async () => {
    render(<AgentModels />);
    const row = await screen.findByTestId('persona-row-brand-new-persona');
    fireEvent.click(within(row).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(selfApi.setPreference).toHaveBeenCalledWith(
      'brand-new-persona',
      'model-certified',
      undefined,
    ));
    expect(await within(row).findByText('Your choice')).toBeInTheDocument();
    expect(within(row).getAllByText('model-certified')).toHaveLength(2);

    fireEvent.click(within(row).getByRole('button', { name: 'Reset' }));
    await waitFor(() => expect(selfApi.resetPreference).toHaveBeenCalledWith(
      'brand-new-persona',
      1,
    ));
    expect(await within(row).findByText('claude-agent-sdk class default (candidate)')).toBeInTheDocument();
    expect(screen.queryByText(/platform default/i)).not.toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Reset' })).toBeDisabled();
    fireEvent.click(within(row).getByRole('button', { name: 'Reset' }));
    expect(selfApi.resetPreference).toHaveBeenCalledTimes(1);
  });

  it('locks scope during save and applies only the server response', async () => {
    const pending = deferred<PreferenceDetail>();
    vi.mocked(selfApi.setPreference).mockReturnValueOnce(pending.promise);
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({
      principals: [{
        canonical_service_principal_id: 'service-1',
        principal_kind: 'service_account',
        display_name: 'Nightly triage',
        tenant_label: 'Acme',
        source: 'github-actions',
        manageable: true,
      }],
    });
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    const selfScope = await screen.findByRole('radio', { name: 'My own agents' });
    const serviceScope = await screen.findByRole('radio', { name: /Nightly triage/ });
    fireEvent.click(within(row).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(selfApi.setPreference).toHaveBeenCalledTimes(1));
    expect(selfScope).toBeDisabled();
    expect(serviceScope).toBeDisabled();
    expect(within(row).getByText('claude-agent-sdk class default (candidate)')).toBeInTheDocument();

    pending.resolve(detail());
    expect(await within(row).findByText('Your choice')).toBeInTheDocument();
    await waitFor(() => expect(selfScope).toBeEnabled());
    expect(serviceScope).toBeEnabled();
  });

  it('sends the displayed reset revision, locks scope, and renders a stale conflict without mutation', async () => {
    const pending = deferred<PreferenceDetail>();
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: [{
        ...preferences.entries[0],
        effective_model_id: 'model-certified',
        effective_is_candidate: false,
        source: 'principal-mapping',
        status: 'configured',
        saved_model_id: 'model-certified',
        requested_alias: 'model-certified',
        revision: 7,
        updated_at: '2026-09-19T01:00:00Z',
      }],
    });
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({
      principals: [{
        canonical_service_principal_id: 'service-1',
        principal_kind: 'service_account',
        display_name: 'Nightly triage',
        tenant_label: 'Acme',
        source: 'github-actions',
        manageable: true,
      }],
    });
    vi.mocked(selfApi.resetPreference).mockReturnValueOnce(pending.promise);
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    const selfScope = await screen.findByRole('radio', { name: 'My own agents' });
    const serviceScope = await screen.findByRole('radio', { name: /Nightly triage/ });
    fireEvent.click(within(row).getByRole('button', { name: 'Reset' }));

    await waitFor(() => expect(selfApi.resetPreference).toHaveBeenCalledWith(
      'brand-new-persona',
      7,
    ));
    expect(selfScope).toBeDisabled();
    expect(serviceScope).toBeDisabled();
    expect(within(row).getByText('Your choice')).toBeInTheDocument();

    pending.reject({
      persona_key: 'brand-new-persona',
      current_model_id: 'model-other',
      current_revision: 8,
      effective_model_id: 'model-other',
      default_model_id: 'model-default',
    });
    expect(await within(row).findByText(/changed elsewhere/)).toBeInTheDocument();
    expect(within(row).getByText('Your choice')).toBeInTheDocument();
    expect(within(row).getAllByText('model-certified')).toHaveLength(2);
    expect(within(row).queryByText(/Nothing was changed/)).not.toBeInTheDocument();
    await waitFor(() => expect(selfScope).toBeEnabled());
    expect(serviceScope).toBeEnabled();
  });

  it('uses only a server-returned opaque ID for managed-service requests via admin module', async () => {
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({
      principals: [{
        canonical_service_principal_id: 'opaque/service:id',
        principal_kind: 'service_account',
        display_name: 'Nightly triage',
        tenant_label: 'Acme',
        source: 'github-actions',
        manageable: true,
      }],
    });
    render(<AgentModels />);

    await screen.findByText('Brand New Persona');
    vi.mocked(adminApi.getModelCatalogue).mockClear();
    const managed = await screen.findByRole('radio', { name: /Nightly triage \(Acme, github-actions\)/ });
    fireEvent.click(managed);
    await waitFor(() => expect(adminApi.getPreferences).toHaveBeenCalledWith(
      'opaque/service:id',
      expect.any(AbortSignal),
    ));
    await waitFor(() => expect(adminApi.getModelCatalogue).toHaveBeenCalledWith(
      'opaque/service:id',
      'brand-new-persona',
      expect.any(AbortSignal),
    ));
    // Self module was not used for the managed-scope load
    expect(selfApi.getPreferences).toHaveBeenCalledTimes(1); // only the initial self load
    expect(await screen.findByText(/Changes below apply to Nightly triage in Acme/)).toBeInTheDocument();

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    fireEvent.click(within(row).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(adminApi.setPreference).toHaveBeenCalledWith(
      'opaque/service:id',
      'brand-new-persona',
      'model-certified',
      undefined,
    ));
    // Self save was never called
    expect(selfApi.setPreference).not.toHaveBeenCalled();
  });

  it('does not carry an unsaved self selection into a managed principal with the same saved value', async () => {
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({ principals: [managedPrincipal] });
    vi.mocked(selfApi.getPreferences).mockResolvedValue(preferences);
    vi.mocked(adminApi.getPreferences).mockResolvedValue({
      ...preferences,
      principal_kind: 'service_account',
      principal_id: 'service-1',
    });
    render(<AgentModels />);

    const selfRow = await screen.findByTestId('persona-row-brand-new-persona');
    const selfChoice = within(selfRow).getByRole('radio', { name: /Haiku 4.5/ });
    fireEvent.click(selfChoice);
    expect(selfChoice).toBeChecked();
    expect(within(selfRow).getByRole('button', { name: 'Save' })).toBeEnabled();

    fireEvent.click(await screen.findByRole('radio', { name: /Nightly triage/ }));
    const managedRow = await screen.findByTestId('persona-row-brand-new-persona');
    const managedChoices = within(managedRow).getAllByRole('radio');
    expect(managedChoices.every((choice) => !(choice as HTMLInputElement).checked)).toBe(true);
    const managedSave = within(managedRow).getByRole('button', { name: 'Save' });
    expect(managedSave).toBeDisabled();
    fireEvent.click(managedSave);
    expect(selfApi.setPreference).not.toHaveBeenCalled();
    expect(adminApi.setPreference).not.toHaveBeenCalled();
  });

  it('does not apply a late response from an aborted principal scope', async () => {
    const staleSelfPreferences = deferred<PreferenceList>();
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({
      principals: [{
        canonical_service_principal_id: 'service-1',
        principal_kind: 'service_account',
        display_name: 'Nightly triage',
        tenant_label: 'Acme',
        source: 'github-actions',
        manageable: true,
      }],
    });
    vi.mocked(selfApi.getPreferences).mockReturnValueOnce(staleSelfPreferences.promise);
    vi.mocked(adminApi.getPreferences).mockResolvedValue({
      ...preferences,
      principal_kind: 'service_account',
      principal_id: 'service-1',
      entries: [{
        ...preferences.entries[0],
        effective_model_id: 'model-certified',
        effective_is_candidate: false,
        source: 'principal-mapping',
        status: 'configured',
        saved_model_id: 'model-certified',
        requested_alias: 'claude-haiku-4-5',
        revision: 4,
        updated_at: '2026-09-19T02:00:00Z',
      }],
    });
    render(<AgentModels />);

    const managed = await screen.findByRole('radio', { name: /Nightly triage/ });
    fireEvent.click(managed);
    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(await within(row).findByText('Your choice')).toBeInTheDocument();
    expect(within(row).getAllByText('model-certified')).toHaveLength(2);

    await act(async () => {
      staleSelfPreferences.resolve(preferences);
      await staleSelfPreferences.promise;
    });
    expect(within(row).getByText('Your choice')).toBeInTheDocument();
    expect(within(row).getAllByText('model-certified')).toHaveLength(2);
  });

  it('does not replace known state after a refusal and offers an explicit conflict reload', async () => {
    const staleReload = deferred<PreferenceList>();
    let selfReads = 0;
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({ principals: [managedPrincipal] });
    vi.mocked(selfApi.getPreferences).mockImplementation(() => {
      selfReads += 1;
      return selfReads === 2 ? staleReload.promise : Promise.resolve(preferences);
    });
    vi.mocked(adminApi.getPreferences).mockResolvedValue({
      ...preferences,
      principal_kind: 'service_account',
      principal_id: 'service-1',
      entries: [{
        ...preferences.entries[0],
        effective_model_id: 'model-certified',
        effective_is_candidate: false,
        source: 'principal-mapping',
        status: 'configured',
        saved_model_id: 'model-certified',
        requested_alias: 'claude-haiku-4-5',
        revision: 5,
        updated_at: '2026-09-19T03:00:00Z',
      }],
    });
    vi.mocked(selfApi.setPreference)
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
    expect(within(row).getByText('claude-agent-sdk class default (candidate)')).toBeInTheDocument();

    fireEvent.click(within(row).getByRole('button', { name: 'Save' }));
    expect(await within(row).findByText(/changed elsewhere/)).toBeInTheDocument();
    fireEvent.click(within(row).getByRole('button', { name: 'Reload current value' }));
    await waitFor(() => expect(selfApi.getPreferences).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('radio', { name: /Nightly triage/ }));
    const managedRow = await screen.findByTestId('persona-row-brand-new-persona');
    expect(await within(managedRow).findByText('Your choice')).toBeInTheDocument();
    await act(async () => {
      staleReload.resolve(preferences);
      await staleReload.promise;
    });
    expect(within(managedRow).getByText('Your choice')).toBeInTheDocument();
    expect(within(managedRow).getAllByText('model-certified')).toHaveLength(2);
  });

  it('fences an imperative retry so it cannot overwrite a newer principal scope', async () => {
    const staleRetry = deferred<PreferenceList>();
    let selfReads = 0;
    vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({ principals: [managedPrincipal] });
    vi.mocked(selfApi.getPreferences).mockImplementation(() => {
      selfReads += 1;
      return selfReads === 2 ? staleRetry.promise : Promise.resolve(preferences);
    });
    vi.mocked(adminApi.getPreferences).mockResolvedValue({
      ...preferences,
      principal_kind: 'service_account',
      principal_id: 'service-1',
      entries: [{
        ...preferences.entries[0],
        effective_model_id: 'model-certified',
        effective_is_candidate: false,
        source: 'principal-mapping',
        status: 'configured',
        saved_model_id: 'model-certified',
        requested_alias: 'claude-haiku-4-5',
        revision: 6,
        updated_at: '2026-09-19T04:00:00Z',
      }],
    });
    vi.mocked(selfApi.getPersonaCatalogue).mockRejectedValueOnce(new Error('catalogue offline'));
    render(<AgentModels />);

    expect(await screen.findByText(/catalogue offline/)).toBeInTheDocument();
    expect(screen.getByText(/not a statement that any effective model is known/)).toBeInTheDocument();
    expect(screen.queryByText(/platform default/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(selfApi.getPreferences).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('radio', { name: /Nightly triage/ }));
    const managedRow = await screen.findByTestId('persona-row-brand-new-persona');
    expect(await within(managedRow).findByText('Your choice')).toBeInTheDocument();

    await act(async () => {
      staleRetry.resolve(preferences);
      await staleRetry.promise;
    });
    expect(within(managedRow).getByText('Your choice')).toBeInTheDocument();
    expect(within(managedRow).getAllByText('model-certified')).toHaveLength(2);
  });

  it('keeps stale evidence visible with expiry dates and never labels it verified', async () => {
    vi.mocked(selfApi.getModelCatalogue).mockResolvedValue({
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
    const provenance = within(row).getByTestId('evidence-provenance');
    expect(provenance).toHaveTextContent(/Last verified/);
    expect(provenance).toHaveTextContent(/expired/);
    const timeElements = provenance.querySelectorAll('time');
    expect(timeElements).toHaveLength(2);
    expect(timeElements[0].getAttribute('datetime')).toBe('2026-09-01T00:00:00Z');
    expect(timeElements[1].getAttribute('datetime')).toBe('2026-09-08T00:00:00Z');
  });

  it('renders verified evidence timestamps for a proven model', async () => {
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: preferences.entries.map((entry) => ({
        ...entry,
        effective_is_candidate: false,
        class_default_status: 'proven' as const,
      })),
    });
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (personaKey) => ({
      ...modelCatalogue,
      persona_key: personaKey,
      models: modelCatalogue.models.map((model) => model.canonical_model_id === 'model-default'
        ? {
            ...model,
            selectable: true,
            reason: null,
            invocable: true,
            evidence: {
              account_id: '123456789012',
              region: 'eu-west-1',
              verified_at: '2026-09-19T00:00:00Z',
              expires_at: '2026-09-26T00:00:00Z',
              stale: false,
            },
          }
        : model),
    }));
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('Verified')).toBeInTheDocument();
    const provenance = within(row).getByTestId('evidence-provenance');
    expect(provenance).toHaveTextContent(/Verified/);
    expect(provenance).toHaveTextContent(/expires/);
    const timeElements = provenance.querySelectorAll('time');
    expect(timeElements).toHaveLength(2);
    expect(timeElements[0].getAttribute('datetime')).toBe('2026-09-19T00:00:00Z');
    expect(timeElements[1].getAttribute('datetime')).toBe('2026-09-26T00:00:00Z');
  });

  it('does not render evidence timestamps for a candidate with no evidence', async () => {
    render(<AgentModels />);

    const row = await screen.findByTestId('persona-row-brand-new-persona');
    expect(within(row).getByText('Not yet certified')).toBeInTheDocument();
    expect(within(row).queryByTestId('evidence-provenance')).not.toBeInTheDocument();
  });

  it('keeps a successful row when a different row save is refused', async () => {
    const secondPersona = {
      ...personas.personas[0],
      key: 'reviewer',
      display_name: 'Reviewer',
      purpose: 'Reviews changes.',
    };
    vi.mocked(selfApi.getPersonaCatalogue).mockResolvedValue({
      personas: [personas.personas[0], secondPersona],
    });
    vi.mocked(selfApi.getPreferences).mockResolvedValue({
      ...preferences,
      entries: [
        preferences.entries[0],
        { ...preferences.entries[0], persona_key: 'reviewer', persona_display_name: 'Reviewer' },
      ],
    });
    vi.mocked(selfApi.getModelCatalogue).mockImplementation(async (key) => ({
      ...modelCatalogue,
      persona_key: key,
    }));
    vi.mocked(selfApi.setPreference)
      .mockResolvedValueOnce(detail())
      .mockRejectedValueOnce({ detail: { reason: 'not_permitted', message: 'Reviewer model is disallowed.' } });

    render(<AgentModels />);
    const first = await screen.findByTestId('persona-row-brand-new-persona');
    const second = await screen.findByTestId('persona-row-reviewer');

    fireEvent.click(within(first).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(first).getByRole('button', { name: 'Save' }));
    expect(await within(first).findByText('Your choice')).toBeInTheDocument();

    fireEvent.click(within(second).getByRole('radio', { name: /Haiku 4.5/ }));
    fireEvent.click(within(second).getByRole('button', { name: 'Save' }));
    expect(await within(second).findByText(/Reviewer model is disallowed/)).toBeInTheDocument();
    expect(within(first).getByText('Your choice')).toBeInTheDocument();
    expect(screen.queryByText(/all rows saved/i)).not.toBeInTheDocument();
  });

  // AC-10 — keyboard operability. Real browser accessibility and narrow-width layout
  // remain PMM-09 work; these assert only what a component test can establish.
  describe('AC-10 keyboard operability', () => {
    it('reaches and operates the model choice and Save by keyboard alone', async () => {
      const user = userEvent.setup();
      render(<AgentModels />);
      const row = await screen.findByTestId('persona-row-brand-new-persona');

      const selectable = within(row).getByRole('radio', { name: /Haiku 4.5/ });
      // Tab until focus lands on the selectable model choice, proving it is reachable
      // without a pointer rather than assuming a tab order.
      let guard = 0;
      while (document.activeElement !== selectable && guard < 40) {
        await user.tab();
        guard += 1;
      }
      expect(selectable).toHaveFocus();

      // Space selects the focused radio.
      await user.keyboard(' ');
      expect(selectable).toBeChecked();

      const save = within(row).getByRole('button', { name: 'Save' });
      expect(save).toBeEnabled();
      save.focus();
      await user.keyboard('{Enter}');

      await waitFor(() => expect(selfApi.setPreference).toHaveBeenCalledWith(
        'brand-new-persona',
        'model-certified',
        undefined,
      ));
      expect(await within(row).findByText('Your choice')).toBeInTheDocument();
    });

    it('does not let keyboard focus reach a non-selectable model choice', async () => {
      render(<AgentModels />);
      const row = await screen.findByTestId('persona-row-brand-new-persona');

      // A disabled radio is not focusable, so a keyboard user cannot select a model
      // the server refused — the same guarantee the pointer path has.
      const disallowed = within(row).getByRole('radio', { name: /Opus 4.6/ });
      expect(disallowed).toBeDisabled();
      disallowed.focus();
      expect(disallowed).not.toHaveFocus();
    });

    it('gives each persona model group and scope group an accessible name', async () => {
      vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({
        principals: [managedPrincipal],
      });
      render(<AgentModels />);
      await screen.findByTestId('persona-row-brand-new-persona');

      // Each radio group must be announced with which persona it configures, otherwise
      // a screen-reader user cannot tell the identical model lists apart.
      expect(screen.getByRole('radiogroup', { name: 'Model for Brand New Persona' })).toBeInTheDocument();
      expect(screen.getByRole('group', { name: 'Configuration scope' })).toBeInTheDocument();
    });

    it('switches configuration scope by keyboard and reports the target account', async () => {
      const user = userEvent.setup();
      vi.mocked(selfApi.getManageableServicePrincipals).mockResolvedValue({
        principals: [managedPrincipal],
      });
      render(<AgentModels />);
      await screen.findByTestId('persona-row-brand-new-persona');

      const managedScope = screen.getByRole('radio', { name: /Nightly triage/ });
      managedScope.focus();
      expect(managedScope).toHaveFocus();
      await user.keyboard(' ');

      // The account and its tenant must be named before any save commits (AC-06).
      expect(await screen.findByText(/Changes below apply to Nightly triage in Acme/)).toBeInTheDocument();
      await waitFor(() => expect(adminApi.getPreferences).toHaveBeenCalledWith(
        'service-1',
        expect.any(AbortSignal),
      ));
    });

    it('announces a row-level refusal to assistive technology', async () => {
      vi.mocked(selfApi.setPreference).mockRejectedValue({
        detail: { reason: 'not_permitted', message: 'That model is disallowed.' },
      });
      render(<AgentModels />);
      const row = await screen.findByTestId('persona-row-brand-new-persona');

      fireEvent.click(within(row).getByRole('radio', { name: /Haiku 4.5/ }));
      fireEvent.click(within(row).getByRole('button', { name: 'Save' }));

      // role="alert" is what makes the failure reach a screen reader rather than being
      // visible-only; the reason must be stated, not a generic failure.
      const alert = await within(row).findByRole('alert');
      expect(alert).toHaveTextContent(/That model is disallowed/);
      expect(alert).toHaveTextContent(/Nothing was changed/);
    });
  });
});
