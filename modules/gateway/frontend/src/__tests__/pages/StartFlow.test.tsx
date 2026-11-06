import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

const authFixture = vi.hoisted(() => ({ userId: 'author' }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: authFixture.userId, orgId: 'tenant-a' } }) }));
import StartFlow from '@/pages/StartFlow';
import { listProducts } from '@/services/products';
import { deriveIntakePlan, getIntakeSession, sendIntakeTurn, startFlowIntake } from '@/services/orchestration';

vi.mock('@/services/products', () => ({ listProducts: vi.fn() }));
vi.mock('@/services/orchestration', () => ({ startFlowIntake: vi.fn(), getIntakeSession: vi.fn(), sendIntakeTurn: vi.fn(), deriveIntakePlan: vi.fn() }));

beforeEach(() => {
  vi.clearAllMocks();
  authFixture.userId = 'author';
  sessionStorage.clear();
  vi.mocked(getIntakeSession).mockResolvedValue({ session_id: 'sess-a', working: true, awaiting_answer: false, open_questions: [], draft: {}, draft_available: true, issue_ref: '', last_response: '', repository: 'planning/epics', requested_issue: '21', product_id: 'product-a' });
  vi.mocked(listProducts).mockResolvedValue({ items: [{ id: 'product-a', name: 'Checkout', revision: 1, project_ids: [] }], next_cursor: null });
});

function show(path = '/flows/new') { return render(<MemoryRouter initialEntries={[path]}><StartFlow /></MemoryRouter>); }

describe('Product-bound flow intake', () => {
  it('selects an existing Product and a separate epic repository without approving execution', async () => {
    const user = userEvent.setup();
    vi.mocked(startFlowIntake).mockResolvedValue({ session_id: 'sess-a', task_id: 'task-a', retry_token: 'retry-a', enqueued_at: 1, thread_id: 'thread-a' });
    show();
    expect(await screen.findByRole('option', { name: 'Checkout' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Project')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start planning' })).toBeDisabled();
    await user.selectOptions(screen.getByRole('combobox', { name: /^Product/ }), 'product-a');
    await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
    await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
    await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build API and web stories');
    await user.click(screen.getByRole('button', { name: 'Start planning' }));
    expect(startFlowIntake).toHaveBeenCalledWith({ product_id: 'product-a', repository: 'planning/epics', issue: '21', message: 'Build API and web stories', retry_token: expect.any(String) });
    expect(screen.getByRole('link', { name: 'Resume this planning session' })).toHaveAttribute('href', '/flows/new?session=sess-a');
    expect(await screen.findByRole('status')).toHaveTextContent('no plan has been approved or dispatched');
  });

  it.each(['base', 'launch'])('carries the selected %s Project from the Product workspace into planning', async projectId => {
    const user = userEvent.setup();
    vi.mocked(listProducts).mockResolvedValue({ items: [{ id: 'product-a', name: 'Checkout', revision: 1,
      project_ids: ['base', 'launch'], base_project_id: 'base', projects: [
        { id: 'base', name: 'Checkout', is_base: true }, { id: 'launch', name: 'Launch', is_base: false },
      ] }], next_cursor: null });
    vi.mocked(startFlowIntake).mockResolvedValue({ session_id: 'sess-a', task_id: 'task-a', retry_token: 'retry-a', enqueued_at: 1, thread_id: 'thread-a' });
    show(`/flows/new?product=product-a&project=${projectId}`);
    expect(await screen.findByRole('combobox', { name: /^Project/ })).toHaveValue(projectId);
    await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
    await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
    await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build API');
    await user.click(screen.getByRole('button', { name: 'Start planning' }));
    expect(startFlowIntake).toHaveBeenCalledWith(expect.objectContaining({ product_id: 'product-a', project_id: projectId }));
  });

  it('surfaces missing Products and does not offer planning without selection', async () => {
    vi.mocked(listProducts).mockResolvedValue({ items: [], next_cursor: null });
    show();
    expect(await screen.findByText(/No accessible Products are available/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Start planning' })).not.toBeInTheDocument();
  });

  it('keeps a failed intake available for retry without claiming a created flow', async () => {
    const user = userEvent.setup();
    vi.mocked(startFlowIntake).mockRejectedValueOnce(new Error('inaccessible'));
    show();
    await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
    await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
    await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
    await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build story');
    await user.click(screen.getByRole('button', { name: 'Start planning' }));
    expect(await screen.findByText(/Planning could not be confirmed/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Retry same request' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: /^What should this flow achieve/ })).toBeDisabled();
  });
});


it('reuses the exact stored request and token after a lost acknowledgement and a reload', async () => {
  const user = userEvent.setup();
  const sessions = new Map<string, string>();
  let first = true;
  vi.mocked(startFlowIntake).mockImplementation(async (input) => {
    if (!sessions.has(input.retry_token)) sessions.set(input.retry_token, 'sess-a');
    if (first) { first = false; throw new Error('acknowledgement lost'); }
    return { session_id: sessions.get(input.retry_token)!, task_id: 'task-a', retry_token: input.retry_token, enqueued_at: 1, thread_id: 'thread-a' };
  });
  const firstView = show();
  await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
  await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build story');
  await user.click(screen.getByRole('button', { name: 'Start planning' }));
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeEnabled();
  const initial = vi.mocked(startFlowIntake).mock.calls[0][0];
  expect(initial.retry_token).toEqual(expect.any(String));
  expect(JSON.parse(sessionStorage.getItem('flow-intake:tenant-a:author')!)).toEqual({ input: initial });
  firstView.unmount();
  show();
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeEnabled();
  expect(screen.getByRole('textbox', { name: /^What should this flow achieve/ })).toHaveValue('Build story');
  await user.click(screen.getByRole('button', { name: 'Retry same request' }));
  await waitFor(() => expect(startFlowIntake).toHaveBeenCalledTimes(2));
  expect(vi.mocked(startFlowIntake).mock.calls[1][0]).toEqual(initial);
  expect(sessions.size).toBe(1);
  expect(await screen.findByRole('link', { name: 'Resume this planning session' })).toHaveAttribute('href', '/flows/new?session=sess-a');
  expect(screen.getByRole('status', { name: '' })).toHaveTextContent('no plan has been approved or dispatched');
});

it('restores an acknowledged session after reload without opening another', async () => {
  const user = userEvent.setup();
  vi.mocked(startFlowIntake).mockResolvedValue({ session_id: 'sess-a', task_id: 'task-a', retry_token: 'retry-a', enqueued_at: 1, thread_id: 'thread-a' });
  const first = show();
  await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
  await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build story');
  await user.click(screen.getByRole('button', { name: 'Start planning' }));
  expect(await screen.findByRole('link', { name: 'Resume this planning session' })).toHaveAttribute('href', '/flows/new?session=sess-a');
  first.unmount();
  show();
  expect(await screen.findByRole('link', { name: 'Resume this planning session' })).toHaveAttribute('href', '/flows/new?session=sess-a');
  expect(await screen.findByText(/The planning agent is responding/)).toBeVisible();
  expect(startFlowIntake).toHaveBeenCalledTimes(1);
});

it('reads the owned session, answers an open question and derives an inert proposal', async () => {
  const user = userEvent.setup();
  vi.mocked(startFlowIntake).mockResolvedValue({ session_id: 'sess-a', task_id: 'task-a', retry_token: 'retry-a', enqueued_at: 1, thread_id: 'thread-a' });
  vi.mocked(getIntakeSession).mockResolvedValueOnce({ session_id: 'sess-a', working: false, awaiting_answer: true,
    open_questions: ['Which repository owns the worker?'], draft_available: true, draft: {}, issue_ref: '',
    last_response: 'Please clarify the worker.', repository: 'planning/epics', requested_issue: '21', product_id: 'product-a' })
    .mockResolvedValueOnce({ session_id: 'sess-a', working: false, awaiting_answer: false, open_questions: [],
      draft_available: true, draft: { intent: 'Build worker' }, issue_ref: '21', last_response: 'The worker is scoped.',
      repository: 'planning/epics', requested_issue: '21', product_id: 'product-a' });
  vi.mocked(sendIntakeTurn).mockResolvedValue({ session_id: 'sess-a', task_id: 'task-b', retry_token: 'retry-b', enqueued_at: 2, thread_id: 'thread-a' });
  vi.mocked(deriveIntakePlan).mockResolvedValue({ session_id: 'sess-a', proposal: { title: 'Build worker' }, prerequisites: ['Bind worker repository'], wrote_nothing: true });
  show();
  await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
  await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build story');
  await user.click(screen.getByRole('button', { name: 'Start planning' }));
  expect(await screen.findByText('Which repository owns the worker?')).toBeVisible();
  await user.type(screen.getByRole('textbox', { name: 'Answer the planning questions' }), 'delivery/worker');
  await user.click(screen.getByRole('button', { name: 'Send answer' }));
  await waitFor(() => expect(sendIntakeTurn).toHaveBeenCalledWith('sess-a', { message: 'delivery/worker', retry_token: expect.any(String) }));
  expect(await screen.findByText('Planning issue: planning/epics#21')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Review derived plan' }));
  const proposal = await screen.findByRole('region', { name: 'Derived plan' });
  expect(proposal).toHaveTextContent('Bind worker repository');
  expect(proposal).toHaveTextContent('Build worker');
  expect(proposal).toHaveTextContent('no plan is approved or dispatched');
  expect(proposal).toHaveTextContent('adp flow create --file plan.json');
  expect(deriveIntakePlan).toHaveBeenCalledWith('sess-a');
});


it.each(['complete', 'fail'] as const)('keeps a new intake form usable after a pending plan review %ss', async (outcome) => {
  const user = userEvent.setup();
  vi.mocked(getIntakeSession).mockResolvedValue({ session_id: 'sess-a', working: false, awaiting_answer: false,
    open_questions: [], draft: { intent: 'Build worker' }, draft_available: true, issue_ref: '21', last_response: '',
    repository: 'planning/epics', requested_issue: '21', product_id: 'product-a' });
  let resolvePlan!: (value: Awaited<ReturnType<typeof deriveIntakePlan>>) => void;
  let rejectPlan!: (reason: Error) => void;
  vi.mocked(deriveIntakePlan).mockReturnValue(new Promise((resolve, reject) => {
    resolvePlan = resolve;
    rejectPlan = reject;
  }));
  vi.mocked(startFlowIntake).mockResolvedValue({ session_id: 'sess-b', task_id: 'task-b', retry_token: 'retry-b', enqueued_at: 2, thread_id: 'thread-b' });
  show('/flows/new?session=sess-a');
  await user.click(await screen.findByRole('button', { name: 'Review derived plan' }));
  const startAnother = screen.getByRole('button', { name: 'Start another planning request' });
  expect(startAnother).toBeDisabled();
  await user.click(startAnother);
  expect(screen.queryByRole('combobox', { name: /^Product/ })).not.toBeInTheDocument();
  if (outcome === 'complete') {
    await act(async () => resolvePlan({ session_id: 'sess-a', proposal: { title: 'Build worker' }, prerequisites: [], wrote_nothing: true }));
  } else {
    await act(async () => rejectPlan(new Error('review unavailable')));
  }
  expect(startAnother).toBeEnabled();
  await user.click(startAnother);
  await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '22');
  await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build delivery worker');
  expect(screen.getByRole('button', { name: 'Start planning' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: 'Start planning' }));
  await waitFor(() => expect(startFlowIntake).toHaveBeenCalledTimes(1));
  expect(startFlowIntake).toHaveBeenCalledWith({ product_id: 'product-a', repository: 'planning/epics',
    issue: '22', message: 'Build delivery worker', retry_token: expect.any(String) });
  expect(await screen.findByText(/Planning request accepted for Product Checkout\. Session sess-b/)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Starting…' })).not.toBeInTheDocument();
});

it('does not expose a retained request to another authenticated author', async () => {
  const user = userEvent.setup();
  vi.mocked(startFlowIntake).mockRejectedValue(new Error('acknowledgement lost'));
  const first = show();
  await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
  await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Private planning intent');
  await user.click(screen.getByRole('button', { name: 'Start planning' }));
  expect(await screen.findByRole('button', { name: 'Retry same request' })).toBeEnabled();
  first.unmount();
  authFixture.userId = 'different-author';
  show();
  expect(await screen.findByRole('button', { name: 'Start planning' })).toBeDisabled();
  expect(screen.getByRole('textbox', { name: /^What should this flow achieve/ })).toHaveValue('');
  expect(sessionStorage.getItem('flow-intake:tenant-a:different-author')).toBeNull();
  expect(sessionStorage.getItem('flow-intake:tenant-a:author')).toContain('Private planning intent');
});

it('does not show a late acknowledgement to an author who switched accounts', async () => {
  const user = userEvent.setup();
  let acknowledge!: (value: Awaited<ReturnType<typeof startFlowIntake>>) => void;
  vi.mocked(startFlowIntake).mockReturnValue(new Promise((resolve) => { acknowledge = resolve; }));
  const view = show();
  await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
  await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Private intent');
  await user.click(screen.getByRole('button', { name: 'Start planning' }));
  authFixture.userId = 'different-author';
  view.rerender(<MemoryRouter><StartFlow /></MemoryRouter>);
  expect(await screen.findByRole('button', { name: 'Start planning' })).toBeDisabled();
  await act(async () => acknowledge({ session_id: 'sess-a', task_id: 'task-a', retry_token: 'retry-a', enqueued_at: 1, thread_id: 'thread-a' }));
  expect(screen.queryByText(/Planning request accepted/)).not.toBeInTheDocument();
  expect(screen.queryByRole('link', { name: 'Resume this planning session' })).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: /^What should this flow achieve/ })).toHaveValue('');
  expect(sessionStorage.getItem('flow-intake:tenant-a:author')).toContain('sess-a');
});

it.each(['sess-a', 'sess-b'])('shows the viewed session Product rather than saved Product from %s', async (savedSessionId) => {
  sessionStorage.setItem('flow-intake:tenant-a:author', JSON.stringify({
    input: { product_id: 'product-a', repository: 'planning/epics', issue: '21', message: 'Checkout', retry_token: 'retry-a' },
    sessionId: savedSessionId,
  }));
  vi.mocked(listProducts).mockResolvedValue({ items: [
    { id: 'product-a', name: 'Checkout', revision: 1, project_ids: [] },
    { id: 'product-b', name: 'Shipping', revision: 1, project_ids: [] },
  ], next_cursor: null });
  vi.mocked(getIntakeSession).mockResolvedValue({ session_id: 'sess-b', working: true, awaiting_answer: false,
    open_questions: [], draft: {}, draft_available: false, issue_ref: '', last_response: '',
    repository: 'planning/epics', requested_issue: '22', product_id: 'product-b' });
  show('/flows/new?session=sess-b');
  expect(await screen.findByText(/Planning request accepted for Product Shipping\. Session sess-b/)).toBeVisible();
  expect(screen.getByText(/Planning request accepted for Product/)).not.toHaveTextContent('Checkout');
  expect(sessionStorage.getItem('flow-intake:tenant-a:author')).toContain(savedSessionId);
});

it('uses the matching saved request while its session is still loading', async () => {
  sessionStorage.setItem('flow-intake:tenant-a:author', JSON.stringify({
    input: { product_id: 'product-a', repository: 'planning/epics', issue: '21', message: 'Checkout', retry_token: 'retry-a' },
    sessionId: 'sess-a',
  }));
  vi.mocked(getIntakeSession).mockReturnValue(new Promise(() => {}));
  show('/flows/new?session=sess-a');
  expect(await screen.findByText(/Planning request accepted for Product Checkout\. Session sess-a/)).toBeVisible();
});

it('reads a linked session through the owned API and never claims a failed lookup was accepted', async () => {
  vi.mocked(getIntakeSession).mockRejectedValueOnce(new Error('not found'))
    .mockResolvedValueOnce({ session_id: 'foreign', working: true, awaiting_answer: false, open_questions: [], draft: {},
      draft_available: true, issue_ref: '', last_response: '', repository: 'planning/epics', requested_issue: '21', product_id: 'product-a' });
  show('/flows/new?session=foreign');
  expect(await screen.findByText(/Planning session could not be read/)).toBeVisible();
  expect(screen.queryByText(/Planning request accepted/)).not.toBeInTheDocument();
  expect(sendIntakeTurn).not.toHaveBeenCalled();
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Refresh session status' }));
  expect(await screen.findByText(/The planning agent is responding/)).toBeVisible();
  expect(getIntakeSession).toHaveBeenCalledWith('foreign');
});

it('retains one retry token for an answer whose acknowledgement was lost', async () => {
  const user = userEvent.setup();
  vi.mocked(getIntakeSession).mockResolvedValue({ session_id: 'sess-a', working: false, awaiting_answer: true,
    open_questions: ['Which repository?'], draft: {}, draft_available: true, issue_ref: '',
    last_response: '', repository: 'planning/epics', requested_issue: '21', product_id: 'product-a' });
  vi.mocked(sendIntakeTurn).mockRejectedValueOnce(new Error('acknowledgement lost'))
    .mockResolvedValue({ session_id: 'sess-a', task_id: 'task-b', retry_token: 'reply-token', enqueued_at: 2, thread_id: 'thread-a' });
  const first = show('/flows/new?session=sess-a');
  await user.type(await screen.findByRole('textbox', { name: 'Answer the planning questions' }), 'delivery/worker');
  await user.click(screen.getByRole('button', { name: 'Send answer' }));
  expect(await screen.findByText(/Reply could not be confirmed/)).toBeVisible();
  const sent = vi.mocked(sendIntakeTurn).mock.calls[0][1];
  expect(sent).toEqual({ message: 'delivery/worker', retry_token: expect.any(String) });
  first.unmount();
  show('/flows/new?session=sess-a');
  await screen.findByRole('button', { name: 'Retry same answer' });
  await user.click(screen.getByRole('button', { name: 'Retry same answer' }));
  await waitFor(() => expect(sendIntakeTurn).toHaveBeenCalledTimes(2));
  expect(vi.mocked(sendIntakeTurn).mock.calls[1][1]).toEqual(sent);
});

it('releases a definitively refused request so the author can correct the Product', async () => {
  const user = userEvent.setup();
  vi.mocked(startFlowIntake).mockRejectedValue({ status: 403 });
  show();
  await user.selectOptions(await screen.findByRole('combobox', { name: /^Product/ }), 'product-a');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic repository/ }), 'planning/epics');
  await user.type(screen.getByRole('textbox', { name: /^Planning epic issue number/ }), '21');
  await user.type(screen.getByRole('textbox', { name: /^What should this flow achieve/ }), 'Build story');
  await user.click(screen.getByRole('button', { name: 'Start planning' }));
  expect(await screen.findByText(/Planning was refused/)).toBeVisible();
  expect(screen.getByRole('textbox', { name: /^What should this flow achieve/ })).toBeEnabled();
  expect(sessionStorage.getItem('flow-intake:tenant-a:author')).toBeNull();
});


it('provides accessible keyboard access to Product selection and epic input', async () => {
  const user = userEvent.setup();
  show();
  await screen.findByRole('combobox', { name: /^Product/ });
  await user.tab();
  expect(screen.getByRole('combobox', { name: /^Product/ })).toHaveFocus();
  await user.tab();
  expect(screen.getByRole('textbox', { name: /^Planning epic repository/ })).toHaveFocus();
  expect(screen.getByRole('link', { name: 'Back to delivery flows' })).toHaveAttribute('href', '/flows');
});
