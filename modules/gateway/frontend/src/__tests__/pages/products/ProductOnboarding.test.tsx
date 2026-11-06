import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ProductOnboarding from '@/pages/products/ProductOnboarding';
import { useFeaturesQuery } from '@/hooks/useFeatures';
import { confirmFinancialDate, getOnboarding, getProduct, guideOnboarding, listInitiatives, listProductContributions, listProductChanges, listSourceBindings, productReceipt, retryOnboardingDiscovery, unlinkOnboardingSource, saveFinancialsStep, setFinancialsApplicabilityStep, startOnboarding, type OnboardingSession } from '@/services/products';

vi.mock('@/hooks/useFeatures', () => ({ useFeaturesQuery: vi.fn() }));

vi.mock('@/services/products', () => ({
  confirmFinancialDate: vi.fn(), getOnboarding: vi.fn(), getProduct: vi.fn(), guideOnboarding: vi.fn(), listInitiatives: vi.fn(), listProductContributions: vi.fn(), listProductChanges: vi.fn(), listSourceBindings: vi.fn(), retryOnboardingDiscovery: vi.fn(), unlinkOnboardingSource: vi.fn(), saveFinancialsStep: vi.fn(), setFinancialsApplicabilityStep: vi.fn(), startOnboarding: vi.fn(), productReceipt: vi.fn(),
}));

const saved: OnboardingSession = {
  id: 'saved-session', product_id: 'product-1', project_id: 'project-1', revision: 1, state: 'open',
  steps: {
    information: { status: 'complete' }, basic: { status: 'not_started' },
    advanced: { status: 'not_started' }, financials: { status: 'not_started' },
  },
  progress: { completed: 2, applicable: 2, percentage: 100, maturity: 'saved' },
};
const props = { productId: 'product-1', projectId: 'project-1', revision: 1, userId: 'actor-1', orgId: 'org-1' };

beforeEach(() => {
  sessionStorage.clear();
  vi.resetAllMocks();
  vi.mocked(productReceipt).mockReturnValue('browser_receipt_12345');
  vi.mocked(useFeaturesQuery).mockReturnValue({ data: { devlake_connectors: false }, isError: false } as ReturnType<typeof useFeaturesQuery>);
  vi.mocked(startOnboarding).mockResolvedValue(saved);
  vi.mocked(guideOnboarding).mockResolvedValue({ revision: 1, message: 'No missing steps', proposals: [], mode: 'deterministic' });
  vi.mocked(getOnboarding).mockResolvedValue(saved);
  vi.mocked(listSourceBindings).mockResolvedValue({ items: [] });
  vi.mocked(getProduct).mockResolvedValue({ tenant_id: 'org-1', product_id: 'product-1', project_id: 'project-1', revision: 1,
    product: { name: 'Sample', purpose: 'Plan', intended_users: ['Person'], owner_ref: { kind: 'user', id: 'actor-1' } },
    project: { id: 'project-1', name: 'Sample', owner_user_id: 'actor-1' }, configuration: {} });
  vi.mocked(listProductChanges).mockResolvedValue({ items: [{ event_id: 'change-1', revision: 1 }] });
  vi.mocked(saveFinancialsStep).mockResolvedValue({ ...saved, revision: 2 });
  vi.mocked(setFinancialsApplicabilityStep).mockResolvedValue({ ...saved, revision: 2 });
  vi.mocked(confirmFinancialDate).mockResolvedValue({ ...saved, revision: 2 });
  vi.mocked(listInitiatives).mockResolvedValue({ items: [] }); vi.mocked(listProductContributions).mockResolvedValue({ items: [] });
});

it('confirms only the suggested date and reloads authoritative session and profile', async () => {
  const proposal = { id: 'business-case-1', base_revision: 1, product: {}, expires_at: '2027-01-01T00:00:00Z',
    remaining_paths: ['/configuration/financials/allocations', '/configuration/financials/release_target_date'],
    configuration: { financials: { release_target_date: '2026-12-01' } } };
  vi.mocked(getOnboarding).mockResolvedValueOnce({ ...saved, proposals: [proposal] })
    .mockResolvedValueOnce({ ...saved, revision: 2, proposals: [{ ...proposal, remaining_paths: ['/configuration/financials/allocations'] }] });
  sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
  render(<ProductOnboarding {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Accept suggested date from business-case-1' }));
  await waitFor(() => expect(confirmFinancialDate).toHaveBeenCalledWith(
    'product-1', 'project-1', saved.id, 1, 'business-case-1', 'browser_receipt_12345',
  ));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Accept suggested date from business-case-1' })).not.toBeInTheDocument());
  expect(getProduct).toHaveBeenCalledTimes(2);
});

it('reads connected document suggestions without writing profile fields and retries an uncertain request', async () => {
  vi.mocked(guideOnboarding).mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce({
    revision: 1, mode: 'financial', message: 'Review the suggested fields before accepting them.', proposals: [{
      id: 'document-1', base_revision: 1, product: {}, expires_at: '2027-01-01T00:00:00Z',
      remaining_paths: ['/configuration/financials/release_target_date'], configuration: { financials: { release_target_date: '2027-01-18' } },
    }],
  });
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  const read = await screen.findByRole('button', { name: 'Suggest values from connected documents' });
  fireEvent.click(read);
  expect(await screen.findByText(/Could not confirm document suggestions/)).toBeInTheDocument();
  fireEvent.click(read);
  expect(await screen.findByRole('button', { name: 'Accept suggested date from document-1' })).toBeInTheDocument();
  expect(guideOnboarding).toHaveBeenCalledTimes(2);
  expect(vi.mocked(guideOnboarding).mock.calls[0]).toEqual(vi.mocked(guideOnboarding).mock.calls[1]);
  expect(guideOnboarding).toHaveBeenCalledWith('product-1', 'project-1', saved.id, 1,
    'Read connected financial documents', 'financial', 'browser_receipt_12345');
  expect(saveFinancialsStep).not.toHaveBeenCalled();
  expect(confirmFinancialDate).not.toHaveBeenCalled();
});

describe('Product onboarding', () => {
  it('starts via the shared API, independently reads the server result, and resumes after remount', async () => {
    const view = render(<ProductOnboarding {...props} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
    await waitFor(() => expect(getOnboarding).toHaveBeenCalledWith('product-1', 'project-1', 'saved-session'));
    expect(startOnboarding).toHaveBeenCalledWith('product-1', 'project-1', 1, 'browser_receipt_12345');
    expect(await screen.findByText(/2\/2 checks · 100% · Saved/)).toBeInTheDocument();
    expect(screen.getByText('financials: not_started')).toBeInTheDocument();
    view.unmount();
    vi.clearAllMocks();
    render(<ProductOnboarding {...props} />);
    expect(await screen.findByText(/Session saved-session · Revision 1/)).toBeInTheDocument();
    expect(startOnboarding).not.toHaveBeenCalled();
    expect(getOnboarding).toHaveBeenCalledWith('product-1', 'project-1', 'saved-session');
  });

  it('keeps an uncertain receipt on retry and does not leak the old Project across a scope switch', async () => {
    vi.mocked(startOnboarding).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(saved);
    const view = render(<ProductOnboarding {...props} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/same request/);
    fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
    await screen.findByText(/Session saved-session/);
    expect(vi.mocked(startOnboarding).mock.calls[0][3]).toBe(vi.mocked(startOnboarding).mock.calls[1][3]);
    view.rerender(<ProductOnboarding {...props} projectId="project-2" />);
    await waitFor(() => expect(screen.queryByText(/Session saved-session/)).not.toBeInTheDocument());
  });
});

it('requires a deliberate metered choice and displays only reviewable suggestions', async () => {
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByText(/Session saved-session · Revision 1/);
  fireEvent.click(screen.getByText('Optional guidance'));
  fireEvent.change(screen.getByLabelText('Ask for guidance'), { target: { value: 'What can I do?' } });
  fireEvent.click(screen.getByRole('button', { name: 'Suggest next steps' }));
  await waitFor(() => expect(guideOnboarding).toHaveBeenCalledWith(
    'product-1', 'project-1', 'saved-session', 1, 'What can I do?', 'deterministic',
  ));
  vi.mocked(guideOnboarding).mockResolvedValueOnce({ revision: 1, mode: 'model', message: 'Review this change', proposals: [{
    id: 'suggestion-1', base_revision: 1, product: { purpose: 'Consider this instead' },
    remaining_paths: ['/product/purpose'], expires_at: '2099-01-01T00:00:00Z',
  }] });
  fireEvent.click(screen.getByLabelText(/Use metered model assistance/));
  fireEvent.click(screen.getByRole('button', { name: 'Suggest next steps' }));
  await waitFor(() => expect(guideOnboarding).toHaveBeenLastCalledWith(
    'product-1', 'project-1', 'saved-session', 1, 'What can I do?', 'model',
  ));
  expect(await screen.findByText(/Consider this instead/)).toBeInTheDocument();
  expect(screen.getByText(/nothing has been applied/)).toBeInTheDocument();
  expect(saveFinancialsStep).not.toHaveBeenCalled();
  vi.mocked(guideOnboarding).mockResolvedValueOnce({ revision: 1, message: 'No missing steps', proposals: [], mode: 'deterministic' });
  fireEvent.click(screen.getByRole('button', { name: 'Suggest next steps' }));
  expect(await screen.findByText(/Model unavailable. No missing steps/)).toBeInTheDocument();
});

it('restores only server-authorized pending proposals after interruption or CLI handoff', async () => {
  const proposal = { id: 'pending-1', base_revision: 1, product: { purpose: 'Review before saving' },
    remaining_paths: ['/product/purpose'], expires_at: '2099-01-01T00:00:00Z' };
  const safe = { ...proposal, id: 'safe-1', product: { goals: ['Keep unrelated suggestion'] },
    remaining_paths: ['/product/goals'] };
  const pending = { ...saved, proposals: [proposal, safe] };
  vi.mocked(getOnboarding).mockResolvedValue(pending);
  sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
  const view = render(<ProductOnboarding {...props} />);
  expect(await screen.findByText('Proposal pending-1 at revision 1')).toBeInTheDocument();
  expect(screen.getByText('Remaining paths: /product/purpose')).toBeInTheDocument();
  expect(screen.getByText(/Review before saving/)).toBeInTheDocument();
  expect(guideOnboarding).not.toHaveBeenCalled();
  view.unmount();
  vi.mocked(getOnboarding).mockResolvedValue({ ...pending, proposals: [safe] });
  render(<ProductOnboarding {...props} />);
  await screen.findByText(/Session saved-session · Revision 1/);
  await screen.findByText('Proposal safe-1 at revision 1');
  expect(screen.queryByText(/Review before saving/)).not.toBeInTheDocument();
  expect(screen.getByText(/Keep unrelated suggestion/)).toBeInTheDocument();
  expect(guideOnboarding).not.toHaveBeenCalled();
});

it('edits a proposed allocation and displays confirmed Financials without converting its decimal', async () => {
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByRole('form', { name: 'Financials' });
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Compute plan' } });
  fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '75.00' } });
  fireEvent.change(screen.getByLabelText('Period start'), { target: { value: '2026-10-01' } });
  fireEvent.change(screen.getByLabelText('Period end'), { target: { value: '2026-12-31' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save proposed allocation' }));
  await waitFor(() => expect(saveFinancialsStep).toHaveBeenCalled());
  const allocation = vi.mocked(saveFinancialsStep).mock.calls[0][4].allocations?.[0];
  expect(allocation).toMatchObject({ amount: { amount: '75.00', currency: 'USD' },
    approval_status: 'proposed', owner_ref: { kind: 'user', id: 'actor-1' },
    target_ref: { tenant_id: 'org-1', project_id: 'project-1', record_id: 'product-1', record_revision: 'change-1' } });
});

it('selects a catalog-authorized cross-product Initiative without inventing another Contribution', async () => {
  const reference = { tenant_id: 'org-1', project_id: 'project-1', source_connection_id: 'adp.catalog',
    record_type: 'initiative', record_id: 'existing-initiative', record_revision: '1' };
  vi.mocked(listInitiatives).mockResolvedValue({ items: [{ tenant_id: 'org-1', id: 'existing-initiative', revision: '1', data: { name: 'Improve checkout', status: 'planned' } }] });
  vi.mocked(listProductContributions).mockResolvedValue({ items: [{ id: 'contribution-1', revision: '2', project_id: 'project-1',
    data: { initiative_ref: reference, product_ref: { ...reference, source_connection_id: 'adp.profile', record_type: 'product', record_id: 'product-1' }, objective: 'Reduce checkout friction', work_links: [] } }] });
  vi.mocked(getProduct).mockResolvedValue({ tenant_id: 'org-1', product_id: 'product-1', project_id: 'project-1', revision: 1,
    product: { name: 'Sample', purpose: 'Plan', intended_users: ['Person'], owner_ref: { kind: 'user', id: 'actor-1' } },
    project: { id: 'project-1', name: 'Sample', owner_user_id: 'actor-1' }, configuration: {} });
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByRole('form', { name: 'Financials' });
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Future plan' } });
  fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '75.00' } });
  fireEvent.change(screen.getByLabelText('Period start'), { target: { value: '2026-10-01' } });
  fireEvent.change(screen.getByLabelText('Period end'), { target: { value: '2026-12-31' } });
  await screen.findByRole('option', { name: 'Improve checkout' });
  fireEvent.change(screen.getByRole('combobox', { name: 'Existing Initiative' }), { target: { value: JSON.stringify(reference) } });
  expect(await screen.findByText('Contribution: Reduce checkout friction · Revision 2')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Release target date'), { target: { value: '2026-12-01' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save proposed allocation' }));
  await waitFor(() => expect(saveFinancialsStep).toHaveBeenCalled());
  expect(vi.mocked(saveFinancialsStep).mock.calls[0][4]).toMatchObject({ initiatives: [reference], release_target_date: '2026-12-01' });
  expect(listProductContributions).toHaveBeenCalledWith('project-1', undefined);
});

it.each(['1', '2'])('reloads reordered Initiative references while requiring catalog revision %s', async (catalogRevision) => {
  const reference = { record_revision: '1', record_id: 'existing-initiative', record_type: 'initiative',
    source_connection_id: 'adp.catalog', project_id: 'project-1', tenant_id: 'org-1' };
  vi.mocked(listInitiatives).mockResolvedValue({ items: [{ tenant_id: 'org-1', id: 'existing-initiative', revision: catalogRevision,
    data: { name: 'Improve checkout', status: 'planned' } }] });
  vi.mocked(listProductContributions).mockResolvedValue({ items: [{ id: 'contribution-1', revision: '2', project_id: 'project-1',
    data: { initiative_ref: reference, product_ref: { ...reference, source_connection_id: 'adp.profile', record_type: 'product', record_id: 'product-1' },
      objective: 'Reduce checkout friction', work_links: [] } }] });
  vi.mocked(getProduct).mockResolvedValue({ tenant_id: 'org-1', product_id: 'product-1', project_id: 'project-1', revision: 1,
    product: { name: 'Sample', purpose: 'Plan', intended_users: ['Person'], owner_ref: { kind: 'user', id: 'actor-1' } },
    configuration: { financials: { initiatives: [reference] } } });
  sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
  render(<ProductOnboarding {...props} />);
  await screen.findByRole('option', { name: 'Improve checkout' });
  const selection = screen.getByRole('combobox', { name: 'Existing Initiative' });
  const submit = screen.getByRole('button', { name: 'Save proposed allocation' });
  if (catalogRevision === '1') {
    expect(selection).toHaveDisplayValue('Improve checkout');
    expect(screen.getByText('Contribution: Reduce checkout friction · Revision 2')).toBeInTheDocument();
    expect(screen.getByText('Initiative: existing-initiative')).toBeInTheDocument();
    expect(submit).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Future plan' } });
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '75.00' } });
    fireEvent.change(screen.getByLabelText('Period start'), { target: { value: '2026-10-01' } });
    fireEvent.change(screen.getByLabelText('Period end'), { target: { value: '2026-12-31' } });
    fireEvent.click(submit);
    await waitFor(() => expect(saveFinancialsStep).toHaveBeenCalled());
    expect(vi.mocked(saveFinancialsStep).mock.calls[0][4].initiatives).toEqual([reference]);
  } else {
    expect(selection).toHaveDisplayValue('None');
    expect(screen.queryByText(/Contribution: Reduce checkout friction/)).not.toBeInTheDocument();
    expect(screen.queryByText('Initiative: existing-initiative')).not.toBeInTheDocument();
    expect(submit).toBeDisabled();
    expect(saveFinancialsStep).not.toHaveBeenCalled();
  }
});

it('excludes unauthorized Initiatives and Contributions belonging to another Product', async () => {
  const reference = { tenant_id: 'org-1', project_id: 'project-1', source_connection_id: 'adp.catalog',
    record_type: 'initiative', record_id: 'visible', record_revision: '3' };
  vi.mocked(listInitiatives).mockResolvedValue({ items: [
    { tenant_id: 'other-tenant', id: 'secret', revision: '1', data: { name: 'Private initiative', status: 'planned' } },
    { tenant_id: 'org-1', id: 'visible', revision: '3', data: { name: 'Visible initiative', status: 'planned' } },
  ] });
  vi.mocked(listProductContributions).mockResolvedValue({ items: [{ id: 'other-contribution', revision: '1', project_id: 'project-1',
    data: { initiative_ref: reference, product_ref: { ...reference, record_type: 'product', record_id: 'other-product' },
      objective: 'Private contribution', work_links: [] } }] });
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByRole('option', { name: 'Visible initiative' });
  expect(screen.queryByRole('option', { name: 'Private initiative' })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole('combobox', { name: 'Existing Initiative' }), { target: { value: JSON.stringify(reference) } });
  expect(await screen.findByText('No Contribution recorded for this Product and Project. Selecting an Initiative does not create one.')).toBeInTheDocument();
  expect(screen.queryByText('Private contribution')).not.toBeInTheDocument();
});

it('does not carry an unsaved allocation into another Project', async () => {
  vi.mocked(listProductChanges).mockResolvedValueOnce({ items: [{ event_id: 'creation-1', revision: 1 }] })
    .mockResolvedValueOnce({ items: [{ event_id: 'binding-2', revision: 2 }] });
  const view = render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByRole('form', { name: 'Financials' });
  fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '75.00' } });
  expect(screen.getByLabelText('Amount')).toHaveValue('75.00');
  view.rerender(<ProductOnboarding {...props} projectId="project-2" />);
  await waitFor(() => expect(screen.queryByRole('form', { name: 'Financials' })).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByRole('form', { name: 'Financials' });
  expect(getProduct).toHaveBeenLastCalledWith('product-1', 'project-2');
  expect(screen.getByLabelText('Amount')).toHaveValue('');
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Second plan' } });
  fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '120.00' } });
  fireEvent.change(screen.getByLabelText('Period start'), { target: { value: '2026-10-01' } });
  fireEvent.change(screen.getByLabelText('Period end'), { target: { value: '2026-12-31' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save proposed allocation' }));
  await waitFor(() => expect(saveFinancialsStep).toHaveBeenCalled());
  expect(vi.mocked(saveFinancialsStep).mock.calls[0][4].allocations?.[0]).toMatchObject({
    amount: { amount: '120.00', currency: 'USD' },
    target_ref: { project_id: 'project-2', record_revision: 'binding-2' },
  });
});

it('chooses Later and Not Applicable without clearing a saved manual amount', async () => {
  const reference = { tenant_id: 'org-1', project_id: 'project-1', source_connection_id: 'adp.profile',
    record_type: 'product', record_id: 'product-1', record_revision: 'change-1' };
  vi.mocked(getProduct).mockResolvedValue({ tenant_id: 'org-1', product_id: 'product-1', project_id: 'project-1', revision: 1,
    product: { name: 'Sample', purpose: 'Plan', intended_users: ['Person'], owner_ref: { kind: 'user', id: 'actor-1' } },
    project: { id: 'project-1', name: 'Sample', owner_user_id: 'actor-1' }, configuration: {
      financials: { allocations: [{ id: 'allocation-1', name: 'Current plan', target_ref: reference,
        amount: { amount: '75.00', currency: 'USD' }, category: 'ai', period_start: '2026-10-01',
        period_end: '2026-12-31', approval_status: 'proposed', owner_ref: { kind: 'user', id: 'actor-1' } }] },
    } });
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByRole('form', { name: 'Financials' });
  expect(screen.getByLabelText('Amount')).toHaveValue('75.00');
  fireEvent.click(screen.getByRole('button', { name: 'Financials Later' }));
  await waitFor(() => expect(setFinancialsApplicabilityStep).toHaveBeenCalledWith(
    'product-1', 'project-1', 'saved-session', 1, 'later', 'Deferred for later.'));
  expect(screen.getByLabelText('Amount')).toHaveValue('75.00');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Financials Later' })).not.toBeDisabled());
  fireEvent.change(screen.getByLabelText('Applicability reason'), { target: { value: 'Planning not needed' } });
  fireEvent.click(screen.getByRole('button', { name: 'Financials Not Applicable' }));
  await waitFor(() => expect(setFinancialsApplicabilityStep).toHaveBeenCalledWith(
    'product-1', 'project-1', 'saved-session', 2, 'not_applicable', 'Planning not needed'));
  expect(screen.getByLabelText('Amount')).toHaveValue('75.00');
});

it('hides partially loaded Initiative and Contribution names when catalog access is revoked', async () => {
  vi.mocked(listInitiatives).mockResolvedValue({ items: [{ tenant_id: 'org-1', id: 'private-initiative', revision: '1',
    data: { name: 'Previously visible initiative', status: 'planned' } }] });
  vi.mocked(listProductContributions).mockRejectedValue({ status: 403, code: 'permission_denied' });
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  expect(await screen.findByText(/Initiative access changed/)).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'Previously visible initiative' })).not.toBeInTheDocument();
  expect(screen.queryByText(/Contribution: /)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Save proposed allocation' })).not.toBeDisabled();
});

it('shows an explicit conflict and clears optional choices after a stale Initiative save', async () => {
  const reference = { tenant_id: 'org-1', project_id: 'project-1', source_connection_id: 'adp.catalog',
    record_type: 'initiative', record_id: 'initiative-1', record_revision: '1' };
  vi.mocked(listInitiatives).mockResolvedValue({ items: [{ tenant_id: 'org-1', id: 'initiative-1', revision: '1',
    data: { name: 'Previously approved', status: 'planned' } }] });
  vi.mocked(saveFinancialsStep).mockRejectedValue({ status: 412, detail: { code: 'stale_revision' } });
  render(<ProductOnboarding {...props} />);
  fireEvent.click(screen.getByRole('button', { name: 'Start or resume onboarding' }));
  await screen.findByRole('option', { name: 'Previously approved' });
  fireEvent.change(screen.getByLabelText('Existing Initiative'), { target: { value: JSON.stringify(reference) } });
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Proposed plan' } });
  fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '75.00' } });
  fireEvent.change(screen.getByLabelText('Period start'), { target: { value: '2026-10-01' } });
  fireEvent.change(screen.getByLabelText('Period end'), { target: { value: '2026-12-31' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save proposed allocation' }));
  expect(await screen.findByText(/Your edits were not saved/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Save proposed allocation' })).toBeDisabled();
  expect(screen.queryByRole('option', { name: 'Previously approved' })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Refresh Initiative choices' })).toBeInTheDocument();
});


describe('Connector recovery', () => {
  it('keeps the saved session and Financials available if source binding readback is offline', async () => {
    vi.mocked(listSourceBindings).mockRejectedValueOnce(new Error('source unavailable'));
    sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
    render(<ProductOnboarding {...props} />);
    expect(await screen.findByText(/Session saved-session · Revision 1/)).toBeInTheDocument();
    expect(await screen.findByText(/Source bindings unavailable/)).toBeInTheDocument();
    expect(await screen.findByRole('form', { name: 'Financials' })).toBeInTheDocument();
  });

  it('shows partial coverage on resume and retries only the server-authorized page with a stable receipt', async () => {
    const partial = { ...saved, discoveries: [{ id: 'page-1', state: 'failed', coverage: 'partial', complete: false,
      resource_count: 1, cursor: 'page-1', binding_ids: ['binding-1'], adapter: 'native' as const }] };
    vi.mocked(getOnboarding).mockResolvedValue(partial);
    vi.mocked(retryOnboardingDiscovery).mockRejectedValueOnce(new Error('lost response')).mockResolvedValueOnce({ id: 'page-2', state: 'succeeded', coverage: 'complete' });
    sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
    render(<ProductOnboarding {...props} />);
    expect(await screen.findByText(/Discovery page-1: partial · failed · Incomplete/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry discovery page-1' }));
    expect(await screen.findByText(/Discovery outcome is unknown/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry discovery page-1' }));
    await waitFor(() => expect(retryOnboardingDiscovery).toHaveBeenCalledTimes(2));
    expect(vi.mocked(retryOnboardingDiscovery).mock.calls[0]).toEqual(vi.mocked(retryOnboardingDiscovery).mock.calls[1]);
    expect(retryOnboardingDiscovery).toHaveBeenCalledWith('product-1', 'project-1', 'saved-session', 1,
      ['binding-1'], 'native', 'page-1', 'browser_receipt_12345');
  });

  it('hides denied retry details and rereads bindings and session after unlink', async () => {
    vi.mocked(getOnboarding).mockResolvedValue({ ...saved, discoveries: [
      { id: 'denied', state: 'cancelled', coverage: 'denied', complete: false, resource_count: 0 }] });
    vi.mocked(listSourceBindings).mockResolvedValueOnce({ items: [{ id: 'binding-1', project_id: 'project-1', status: 'healthy' }] })
      .mockResolvedValueOnce({ items: [{ id: 'binding-1', project_id: 'project-1', status: 'unlinked' }] });
    vi.mocked(unlinkOnboardingSource).mockResolvedValue({ id: 'binding-1', project_id: 'project-1', status: 'unlinked' });
    sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
    render(<ProductOnboarding {...props} />);
    expect(await screen.findByText(/Discovery denied: denied · cancelled · Incomplete/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Retry discovery/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink binding binding-1' }));
    await waitFor(() => expect(unlinkOnboardingSource).toHaveBeenCalledWith(
      'product-1', 'project-1', 'binding-1', 1, 'browser_receipt_12345'));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Unlink binding binding-1' })).not.toBeInTheDocument());
    expect(listSourceBindings).toHaveBeenCalledTimes(2);
  });

  it.each([false, true])('releases a stale unlink request before retrying with a new receipt (refresh fails: %s)', async (refreshFails) => {
    const storageKey = 'product-onboarding:org-1:actor-1:product-1:project-1';
    vi.mocked(getOnboarding).mockResolvedValueOnce({ ...saved, revision: 2 })
      .mockResolvedValueOnce({ ...saved, revision: 2 }).mockResolvedValue({ ...saved, revision: 3 });
    if (refreshFails) vi.mocked(getOnboarding).mockRejectedValueOnce(new Error('read unavailable'));
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [{ id: 'binding-1', project_id: 'project-1', status: 'healthy' }] });
    vi.mocked(productReceipt).mockReturnValueOnce('rejected-receipt').mockReturnValueOnce('fresh-receipt');
    vi.mocked(unlinkOnboardingSource).mockRejectedValueOnce({ status: 412, detail: { code: 'stale_revision' } })
      .mockResolvedValueOnce({ id: 'binding-1', project_id: 'project-1', status: 'unlinked' });
    sessionStorage.setItem(storageKey, saved.id);
    render(<ProductOnboarding {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Unlink binding binding-1' }));
    expect(await screen.findByText(/Profile changed. Unlink was not applied/)).toBeInTheDocument();
    if (refreshFails) expect(await screen.findByText(/Saved bindings are unavailable/)).toBeInTheDocument();
    else expect(await screen.findByText(/Session saved-session · Revision 3/)).toBeInTheDocument();
    expect(sessionStorage.getItem(`${storageKey}:unlink-receipt`)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink binding binding-1' }));
    await waitFor(() => expect(unlinkOnboardingSource).toHaveBeenCalledTimes(2));
    expect(vi.mocked(unlinkOnboardingSource).mock.calls.map((args) => args.slice(3))).toEqual([
      [2, 'rejected-receipt'], [3, 'fresh-receipt'],
    ]);
    await waitFor(() => expect(sessionStorage.getItem(`${storageKey}:unlink-receipt`)).toBeNull());
  });

  it('retains an uncertain unlink request across reload even when the saved revision advances', async () => {
    const storageKey = 'product-onboarding:org-1:actor-1:product-1:project-1';
    vi.mocked(getOnboarding).mockResolvedValueOnce({ ...saved, revision: 2 })
      .mockResolvedValueOnce({ ...saved, revision: 2 }).mockResolvedValue({ ...saved, revision: 3 });
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [{ id: 'binding-1', project_id: 'project-1', status: 'healthy' }] });
    vi.mocked(unlinkOnboardingSource).mockRejectedValueOnce(new Error('lost response'))
      .mockResolvedValueOnce({ id: 'binding-1', project_id: 'project-1', status: 'unlinked' });
    sessionStorage.setItem(storageKey, saved.id);
    const first = render(<ProductOnboarding {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Unlink binding binding-1' }));
    expect(await screen.findByText(/Unlink outcome is unknown/)).toBeInTheDocument();
    expect(JSON.parse(sessionStorage.getItem(`${storageKey}:unlink-receipt`)!)).toEqual({
      bindingId: 'binding-1', revision: 2, receipt: 'browser_receipt_12345',
    });
    first.unmount();
    render(<ProductOnboarding {...props} />);
    expect(await screen.findByText(/Session saved-session · Revision 3/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Unlink binding binding-1' }));
    await waitFor(() => expect(unlinkOnboardingSource).toHaveBeenCalledTimes(2));
    expect(vi.mocked(unlinkOnboardingSource).mock.calls[0]).toEqual(vi.mocked(unlinkOnboardingSource).mock.calls[1]);
    expect(productReceipt).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(sessionStorage.getItem(`${storageKey}:unlink-receipt`)).toBeNull());
  });
});


describe('Optional connector discovery', () => {
  const connector = { ...saved, discoveries: [{ id: 'connector-page', state: 'failed', coverage: 'partial',
    complete: false, resource_count: 1, cursor: 'connector-page', binding_ids: ['binding-1'], adapter: 'devlake' as const }] };

  it.each([
    { data: undefined, isError: false },
    { data: {}, isError: false },
    { data: { devlake_connectors: false }, isError: false },
    { data: { devlake_connectors: true }, isError: true },
  ])('withholds retry on off, missing or failed feature discovery while retaining Profile editing: %j', async (flags) => {
    vi.mocked(useFeaturesQuery).mockReturnValue(flags as ReturnType<typeof useFeaturesQuery>);
    vi.mocked(getOnboarding).mockResolvedValue(connector);
    let resolveChanges!: (value: Awaited<ReturnType<typeof listProductChanges>>) => void;
    const changesRead = new Promise<Awaited<ReturnType<typeof listProductChanges>>>((resolve) => {
      resolveChanges = resolve;
    });
    vi.mocked(listProductChanges).mockReturnValue(changesRead);
    sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
    render(<ProductOnboarding {...props} />);
    expect(await screen.findByText(/Discovery connector-page: partial · failed · Incomplete/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Retry discovery connector-page/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Financials' })).not.toBeInTheDocument();
    resolveChanges({ items: [{ event_id: 'change-1', revision: 1 }] });
    expect(await screen.findByRole('form', { name: 'Financials' })).toBeInTheDocument();
    expect(retryOnboardingDiscovery).not.toHaveBeenCalled();
  });

  it('offers a server-authorized retry when deployed connector discovery is enabled', async () => {
    vi.mocked(useFeaturesQuery).mockReturnValue({ data: { devlake_connectors: true }, isError: false } as ReturnType<typeof useFeaturesQuery>);
    vi.mocked(getOnboarding).mockResolvedValue(connector);
    vi.mocked(retryOnboardingDiscovery).mockResolvedValue({ id: 'retry', state: 'failed', coverage: 'unavailable' });
    sessionStorage.setItem('product-onboarding:org-1:actor-1:product-1:project-1', saved.id);
    render(<ProductOnboarding {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry discovery connector-page' }));
    await waitFor(() => expect(retryOnboardingDiscovery).toHaveBeenCalledWith(
      'product-1', 'project-1', saved.id, 1, ['binding-1'], 'devlake', 'connector-page', 'browser_receipt_12345'));
    expect(await screen.findByRole('form', { name: 'Financials' })).toBeInTheDocument();
  });
});
