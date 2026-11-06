/**
 * useDevBox — Issue #8094. The shared ensure/poll/open-dashboard flow behind both the
 * chat action and the Settings → Dev Box page.
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { useDevBox, boxState, costSummary, describeFailure, hostReadinessSentence, needsBrowserEnrollment } from '@/hooks/useDevBox';
import { apiClient } from '@/services/api';
import { resolveCrewDashboardDestination } from '@/services/crewDashboard';
import { enrollCrewHost } from '@/services/crewHostEnrollment';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), request: vi.fn() } }));
vi.mock('@/services/crewDashboard', () => ({ resolveCrewDashboardDestination: vi.fn() }));
vi.mock('@/services/crewHostEnrollment', () => ({ enrollCrewHost: vi.fn() }));

const replace = vi.fn();
const cost = { estimated_cost_usd: '0.10', reconciled_cost_usd: null, total_cost_usd: null, coverage: 'partial_unknown', retained_resources: [] };
const box = { desktop_id: 'owned', name: 'assistant', template: 'approved', profile: 'crew', status: 'running', observed_state: 'running',
  machine_generation: 1, crew_bootstrap: 'image', infrastructure_cost: cost };
const ready = { health: 'ready', lease: { target_kind: 'ec2-instance', target_ref: 'owned', generation: 1 } };
let items: unknown[] = [];
let operation: unknown = { desktop_id: 'owned', operation_id: 'op', status: 'succeeded', progress: 'running' };

beforeEach(() => {
  vi.restoreAllMocks();
  items = [];
  operation = { desktop_id: 'owned', operation_id: 'op', status: 'succeeded', progress: 'running' };
  vi.mocked(apiClient.get).mockReset().mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/policy') return { contract_version: 1, admission_allowed: true, reason: null };
    if (endpoint === '/cloud-desktops/crew-template') return { contract_version: 1, template: 'approved' };
    if (endpoint === '/cloud-desktops/owned') return box;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/cloud-desktops/owned/operations/op') return operation;
    if (endpoint === '/crew/target') return ready;
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockReset().mockImplementation(async endpoint => {
    items = endpoint.endsWith('/release') ? [] : [box];
    return { desktop_id: 'owned', operation_id: 'op', status: 'pending', progress: 'requested' };
  });
  vi.mocked(resolveCrewDashboardDestination).mockReset().mockResolvedValue('https://crew.example.test/?token=test');
  vi.mocked(enrollCrewHost).mockReset().mockResolvedValue(undefined);
  replace.mockReset();
  // `openBox` delivers by default with a same-tab location.replace (the interstitial becomes the dashboard).
  Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, replace } });
});
const HANDOFF = 'https://crew.example.test/crew/handoff';

it('does not touch the API on mount unless asked to auto-load', async () => {
  const idle = renderHook(() => useDevBox());
  expect(idle.result.current.loading).toBe(false);
  expect(apiClient.get).not.toHaveBeenCalled();
  idle.unmount();
  const loaded = renderHook(() => useDevBox({ autoLoad: true }));
  expect(loaded.result.current.loading).toBe(true);
  await waitFor(() => expect(loaded.result.current.loading).toBe(false));
  expect(loaded.result.current.box).toBeNull();
  expect(loaded.result.current.policy).toEqual({ contract_version: 1, admission_allowed: true, reason: null });
  expect(apiClient.get).toHaveBeenCalledWith('/cloud-desktops/policy', expect.any(AbortSignal));
});

it('openBox records the lifecycle in order with ISO timestamps and resolves with the opened destination', async () => {
  const { result } = renderHook(() => useDevBox());
  let opened: unknown;
  await act(async () => { opened = await result.current.openBox(HANDOFF); });
  expect(opened).toBe('https://crew.example.test/?token=test');
  const steps = result.current.timeline.map(event => event.step);
  expect(steps).toEqual(['requested', 'launching', 'running', 'installing_crew', 'enrolling_host', 'starting_crew', 'crew_ready']);
  expect(enrollCrewHost).not.toHaveBeenCalled();
  for (const event of result.current.timeline) expect(new Date(event.at).toISOString()).toBe(event.at);
  expect(result.current.timeline[0].detail).toBe('requested');
  expect(result.current.timeline[1].detail).toBe('running');
  expect(result.current.phase).toBeNull();
  expect(result.current.failure).toBeNull();
  await waitFor(() => expect(result.current.box).toEqual(box));
});

it('does not fetch the crew template when a box already exists', async () => {
  items = [{ ...box, status: 'stopped', observed_state: 'stopped' }];
  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(apiClient.get).not.toHaveBeenCalledWith('/cloud-desktops/crew-template', expect.anything());
  expect(apiClient.request).toHaveBeenCalledWith('/cloud-desktops/ensure', expect.objectContaining({ body: { template: 'approved', profile: 'crew' } }));
});

it('names a failed operation from error_class and progress', async () => {
  operation = { desktop_id: 'owned', operation_id: 'op', status: 'failed', progress: 'installing_assistant', error_class: 'crew_bootstrap_failed' };
  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(result.current.failure).toEqual({ code: 'desktop_operation_failed', errorClass: 'crew_bootstrap_failed', progress: 'installing_assistant' });
  expect(describeFailure(result.current.failure)).toBe('desktop_operation_failed — crew_bootstrap_failed at step “installing_assistant”');
  expect(result.current.timeline.map(event => event.step)).toEqual(['requested', 'launching']);
  expect(result.current.phase).toBeNull();
});

it('keeps the blocked-migration sentence and surfaces server detail for refusals', () => {
  expect(describeFailure({ code: 'crew_migration_blocked', errorClass: 'crew_migration_blocked', progress: 'installing_assistant' }))
    .toBe('Crew upgrade blocked: workspace compatibility is not verified; existing data is preserved.');
  expect(describeFailure({ code: 'desktop_request_refused', detail: 'EC2 Crew is not permitted' })).toBe('EC2 Crew is not permitted');
  // #8423: a capacity refusal after the controller's fallback is a plain sentence that ends in Retry, not a raw class.
  expect(describeFailure({ code: 'desktop_operation_failed', errorClass: 'provider_refused:InsufficientInstanceCapacity', progress: 'launch_intent' }))
    .toMatch(/^EC2 had no capacity for any approved instance size .*Retry launches again/);
  expect(describeFailure({ code: 'desktop_operation_failed', errorClass: 'workspace_zone_unavailable', progress: 'launch_intent' }))
    .toMatch(/No approved subnet lies in the availability zone/);
  expect(describeFailure({ code: 'desktop_operation_failed', errorClass: 'provider_refused:UnauthorizedOperation', progress: 'launch_intent' }))
    .toBe('desktop_operation_failed — provider_refused:UnauthorizedOperation at step “launch_intent”');
  expect(describeFailure({ code: 'crew_target_mismatch' })).toBe('crew_target_mismatch');
  expect(describeFailure(null)).toBeNull();
  expect(costSummary(null)).toBe('Infrastructure cost: not yet observed.');
});

it('refuses to release without a retention choice and never calls the API', async () => {
  items = [box];
  const { result } = renderHook(() => useDevBox({ autoLoad: true }));
  await waitFor(() => expect(result.current.box).toEqual(box));
  await act(async () => { await result.current.act('release'); });
  expect(result.current.failure).toEqual({ code: 'workspace_retention_required' });
  expect(apiClient.request).not.toHaveBeenCalled();
  await act(async () => { await result.current.act('release', 'retain'); });
  expect(apiClient.request).toHaveBeenCalledWith('/cloud-desktops/owned/release', expect.objectContaining({
    method: 'POST', body: { workspace_retention: 'retain' }, headers: { 'Idempotency-Key': expect.any(String) },
  }));
  await waitFor(() => expect(result.current.box).toBeNull());
});

it('sends a fresh idempotency key per action and polls the receipt', async () => {
  items = [box];
  const { result } = renderHook(() => useDevBox({ autoLoad: true }));
  await waitFor(() => expect(result.current.box).toEqual(box));
  await act(async () => { await result.current.act('stop'); });
  await act(async () => { await result.current.act('start'); });
  const used = vi.mocked(apiClient.request).mock.calls.map(([, options]) => (options?.headers as Record<string, string>)['Idempotency-Key']);
  expect(used).toHaveLength(2);
  expect(used[0]).not.toBe(used[1]);
  expect(apiClient.request.mock.calls.map(([endpoint]) => endpoint)).toEqual(['/cloud-desktops/owned/stop', '/cloud-desktops/owned/start']);
  expect(vi.mocked(apiClient.get).mock.calls.filter(([endpoint]) => endpoint === '/cloud-desktops/owned/operations/op')).toHaveLength(2);
});

it('openBox resolves the destination, delivers it once (same-tab replace by default, or to the given deliver), and nothing when refused', async () => {
  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(replace).toHaveBeenCalledExactlyOnceWith('https://crew.example.test/?token=test');
  expect(result.current.crewTarget).toBe('ready');

  const deliver = vi.fn();
  await act(async () => { await result.current.openBox(HANDOFF, { deliver }); });
  expect(deliver).toHaveBeenCalledExactlyOnceWith('https://crew.example.test/?token=test');
  expect(replace).toHaveBeenCalledOnce();

  deliver.mockClear();
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items: [{ ...box, profile: 'other' }] };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  await act(async () => { await result.current.openBox(HANDOFF, { deliver }); });
  expect(result.current.failure).toEqual({ code: 'desktop_profile_conflict' });
  expect(deliver).not.toHaveBeenCalled();
  expect(replace).toHaveBeenCalledOnce();
});

it('connect-only (ensure: false) delivers for a running, ready box and never provisions, starts or enrolls otherwise', async () => {
  items = [box];
  const deliver = vi.fn();
  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF, { ensure: false, deliver }); });
  expect(deliver).toHaveBeenCalledExactlyOnceWith('https://crew.example.test/?token=test');
  expect(apiClient.request).not.toHaveBeenCalled();
  expect(result.current.box).toEqual(box);

  deliver.mockClear();
  for (const current of [[], [{ ...box, status: 'stopped', observed_state: 'stopped' }]]) {
    items = current;
    await act(async () => { await result.current.openBox(HANDOFF, { ensure: false, deliver }); });
    expect(result.current.failure).toEqual({ code: 'dev_box_not_ready' });
  }
  items = [box];
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/crew/target') throw { status: 404, detail: { error: 'crew_target_missing' } };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  await act(async () => { await result.current.openBox(HANDOFF, { ensure: false, deliver }); });
  expect(result.current.failure).toEqual({ code: 'dev_box_not_ready' });
  expect(describeFailure(result.current.failure)).toBe('Your Dev Box is not running with Crew ready. Open it from the Dev Box page.');
  expect(deliver).not.toHaveBeenCalled();
  expect(apiClient.request).not.toHaveBeenCalled();
  expect(enrollCrewHost).not.toHaveBeenCalled();
});

it('one click from stopped: start → running → enroll → Crew ready → resolve → open, in that order', async () => {
  const stopped = { ...box, status: 'stopped', observed_state: 'stopped', desired_state: 'stopped' };
  items = [stopped];
  const sequence: string[] = [];
  let enrolled = false;
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint !== '/cloud-desktops/owned/resources') sequence.push(`GET ${endpoint}`);
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned/operations/start-op') return { desktop_id: 'owned', operation_id: 'start-op', status: 'succeeded', progress: 'running' };
    if (endpoint === '/cloud-desktops/owned') return { ...box, desired_state: 'running' };
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') { if (!enrolled) throw { status: 404, detail: { error: 'crew_target_missing' } }; return ready; }
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockImplementation(async endpoint => {
    sequence.push(`POST ${endpoint}`);
    items = [{ ...box, desired_state: 'running' }];
    return { desktop_id: 'owned', operation_id: 'start-op', status: 'pending', progress: 'admitted' };
  });
  vi.mocked(enrollCrewHost).mockImplementation(async desktopId => { sequence.push(`ENROLL ${desktopId}`); enrolled = true; });
  vi.mocked(resolveCrewDashboardDestination).mockImplementation(async () => { sequence.push('RESOLVE'); return 'https://crew.example.test/?token=test'; });
  replace.mockImplementation(() => { sequence.push('OPEN'); });

  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(sequence.slice(0, 11)).toEqual([
    'GET /cloud-desktops',
    'POST /cloud-desktops/ensure',
    'GET /cloud-desktops/owned/operations/start-op',
    'GET /cloud-desktops/owned',
    'GET /crew/target',
    'ENROLL owned',
    'GET /cloud-desktops/owned',
    'GET /crew/target',
    'GET /cloud-desktops/owned',
    'RESOLVE',
    'OPEN',
  ]);
  expect(result.current.timeline.map(event => event.step)).toEqual(['requested', 'launching', 'running', 'installing_crew', 'enrolling_host', 'starting_crew', 'crew_ready']);
  expect(result.current.failure).toBeNull();
  expect(result.current.crewTarget).toBe('ready');
}, 10000);

it('a fenced lease after a restart is awaited, never enrolled again: the controller re-binds the host (#8223)', async () => {
  // The stop kept the approved device (host_binding "enrolled"); the browser only waits for the
  // controller's re-bind and readiness, naming the controller's reason while it does.
  const enrolledBox = { ...box, host_binding: 'enrolled', host_readiness_refusal: null };
  items = [enrolledBox];
  let polls = 0;
  const sequence: string[] = [];
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint !== '/cloud-desktops/owned/resources') sequence.push(`GET ${endpoint}`);
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned') return polls >= 2 ? { ...enrolledBox, host_binding: 'active' } : enrolledBox;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') { polls += 1; if (polls < 3) throw { status: 409, detail: { error: 'lease_fenced' } }; return ready; }
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockResolvedValue({ desktop_id: 'owned', operation_id: null, status: 'succeeded', progress: 'last_observed_running' });
  vi.mocked(resolveCrewDashboardDestination).mockResolvedValue('https://crew.example.test/?token=test');

  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(enrollCrewHost).not.toHaveBeenCalled();
  expect(sequence.filter(step => step.startsWith('ENROLL'))).toEqual([]);
  expect(result.current.failure).toBeNull();
  expect(result.current.crewTarget).toBe('ready');
  expect(replace).toHaveBeenCalledOnce();
}, 10000);

it('a 409 crew_starting is awaited like a fenced lease and named as Crew starting (#9619)', async () => {
  // The controller activated the host; Kiro Crew on the box is not answering yet. The page keeps
  // polling (never enrolls, never fails) and hands off once the target says ready.
  const activeBox = { ...box, host_binding: 'active', host_readiness_refusal: null };
  items = [activeBox];
  let polls = 0;
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned') return activeBox;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') { polls += 1; if (polls < 3) throw { status: 409, detail: { error: 'crew_starting' } }; return ready; }
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockResolvedValue({ desktop_id: 'owned', operation_id: null, status: 'succeeded', progress: 'last_observed_running' });
  vi.mocked(resolveCrewDashboardDestination).mockResolvedValue('https://crew.example.test/?token=test');

  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(enrollCrewHost).not.toHaveBeenCalled();
  expect(result.current.failure).toBeNull();
  expect(result.current.crewTarget).toBe('ready');
  expect(replace).toHaveBeenCalledOnce();
  // Two "starting" answers were waited through before the target said ready.
  expect(polls).toBeGreaterThanOrEqual(3);
}, 10000);

it('enrolls from the browser only a generation nobody approved yet', () => {
  expect(needsBrowserEnrollment({ host_binding: 'none' }, 'crew_target_missing')).toBe(true);
  expect(needsBrowserEnrollment({ host_binding: 'none' }, 'lease_fenced')).toBe(true);
  expect(needsBrowserEnrollment({ host_binding: 'none' }, 'ready')).toBe(false);
  for (const binding of ['enrolled', 'active', 'pending_enrollment']) {
    expect(needsBrowserEnrollment({ host_binding: binding }, 'crew_target_missing')).toBe(false);
    expect(needsBrowserEnrollment({ host_binding: binding }, 'lease_fenced')).toBe(false);
  }
  // Older gateways without the field: the pre-#8223 rule, a missing target only.
  expect(needsBrowserEnrollment({}, 'crew_target_missing')).toBe(true);
  expect(needsBrowserEnrollment({}, 'lease_fenced')).toBe(false);
});

it('awaits a pending enrollment instead of re-submitting, and refuses by name when the API refuses', async () => {
  items = [box];
  let targetCalls = 0;
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned') return box;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') { targetCalls += 1; if (targetCalls < 3) throw { status: 404, detail: { error: 'crew_target_missing' } }; return ready; }
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockResolvedValue({ desktop_id: 'owned', operation_id: null, status: 'succeeded', progress: 'last_observed_running' });
  vi.mocked(enrollCrewHost).mockRejectedValueOnce({ status: 409, detail: 'A host enrollment is pending' });
  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(enrollCrewHost).toHaveBeenCalledOnce();
  expect(result.current.failure).toBeNull();
  expect(result.current.timeline.find(event => event.step === 'starting_crew')?.detail).toBe('enrollment already pending');
  expect(replace).toHaveBeenCalledOnce();

  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned') return box;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') throw { status: 404, detail: { error: 'crew_target_missing' } };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(enrollCrewHost).mockReset().mockRejectedValueOnce({ status: 409, detail: 'Current host key has changed' });
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(result.current.failure).toEqual({ code: 'enrollment_refused', detail: 'Current host key has changed' });
  expect(describeFailure(result.current.failure)).toBe('Host enrollment refused: Current host key has changed');
  expect(enrollCrewHost).toHaveBeenCalledOnce();
  expect(replace).toHaveBeenCalledOnce();
}, 10000);

it('retries enrollment on the next poll while the controller has not verified the host yet', async () => {
  items = [box];
  let attempts = 0;
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned') return box;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') { if (attempts < 2) throw { status: 404, detail: { error: 'crew_target_missing' } }; return ready; }
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockResolvedValue({ desktop_id: 'owned', operation_id: null, status: 'succeeded', progress: 'last_observed_running' });
  vi.mocked(enrollCrewHost).mockImplementation(async () => {
    attempts += 1;
    if (attempts === 1) throw { status: 409, detail: 'Current host has not been verified' };
  });
  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(enrollCrewHost).toHaveBeenCalledTimes(2);
  expect(result.current.failure).toBeNull();
  expect(replace).toHaveBeenCalledOnce();
}, 10000);

it('names a Crew scope refusal from /crew/target instead of treating it as a missing target', async () => {
  items = [box];
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned') return box;
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') throw { status: 404, detail: { error: 'crew_scope_refused', reason: 'no_member', detail: 'You are not a member of this tenant.' } };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  vi.mocked(apiClient.request).mockResolvedValue({ desktop_id: 'owned', operation_id: null, status: 'succeeded', progress: 'last_observed_running' });
  const { result } = renderHook(() => useDevBox());
  await act(async () => { await result.current.openBox(HANDOFF); });
  expect(result.current.failure).toEqual({ code: 'crew_scope_refused', detail: 'You are not a member of this tenant.' });
  expect(enrollCrewHost).not.toHaveBeenCalled();
});

it('names a fenced lease as a state and keeps it out of the enrollment step', async () => {
  items = [box];
  vi.mocked(apiClient.get).mockImplementation(async endpoint => {
    if (endpoint === '/cloud-desktops') return { contract_version: 1, items };
    if (endpoint === '/cloud-desktops/owned/resources') return { contract_version: 1, items: [] };
    if (endpoint === '/crew/target') throw { status: 409, detail: { error: 'lease_fenced' } };
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  });
  const { result } = renderHook(() => useDevBox({ autoLoad: true }));
  await waitFor(() => expect(result.current.crewTarget).toBe('lease_fenced'));
  expect(result.current.failure).toBeNull();
});

it('boxState settles only on matching status, observed and desired state', () => {
  expect(boxState(null)).toBeNull();
  expect(boxState({ status: 'running', observed_state: 'running', desired_state: 'running' })).toBe('running');
  expect(boxState({ status: 'running', observed_state: 'running' })).toBe('running');
  expect(boxState({ status: 'stopped', observed_state: 'stopped', desired_state: 'stopped' })).toBe('stopped');
  expect(boxState({ status: 'running', observed_state: 'running', desired_state: 'stopped' })).toBe('transitioning');
  expect(boxState({ status: 'stopped', observed_state: 'stopped', desired_state: 'running' })).toBe('transitioning');
  expect(boxState({ status: 'requested', observed_state: 'unknown', desired_state: 'running' })).toBe('transitioning');
  expect(boxState({ status: 'running', observed_state: 'unknown', desired_state: 'running' })).toBe('transitioning');
  expect(boxState({ status: 'failed', observed_state: 'unknown', desired_state: 'running' })).toBe('failed');
  expect(boxState({ status: 'released', observed_state: 'unknown', desired_state: 'released' })).toBe('released');
});

it('names the host readiness refusal the controller recorded, and stays silent otherwise (#8223)', () => {
  expect(hostReadinessSentence(null)).toBeNull();
  expect(hostReadinessSentence({ host_readiness_refusal: null })).toBeNull();
  expect(hostReadinessSentence({ host_readiness_refusal: 'host_refused:InvalidCheckpoint' }))
    .toBe('Crew could not start on this host (InvalidCheckpoint); the controller retries automatically. If it persists, Replace the box.');
  expect(hostReadinessSentence({ host_readiness_refusal: 'TimedOut' })).toContain('(TimedOut)');
  // #8223 cause 34: the controller pinned a release the box was never provisioned from.
  expect(hostReadinessSentence({ host_readiness_refusal: 'host_refused:CrewProfileMismatch' }))
    .toBe('This machine runs a different Crew release than its record says. Replace the box to update it; the controller keeps retrying meanwhile.');
  expect(hostReadinessSentence({ host_readiness_refusal: 'host_refused:CrewReceiptMissing' })).toContain('never installed');
});

it('shows snapshot storage GB, rate and cost without claiming incremental billing precision', () => {
  const snapshotCost = { ...cost, retained_resources: [{ kind: 'snapshot', size_gb: 40 }],
    by_kind: { snapshot: { storage_gb: 40, rate_usd_per_gb_hour: '0.000070', known_cost_usd: '0.067200' } } };
  expect(costSummary(snapshotCost)).toContain('Snapshot storage estimate: 40 GB; $0.000070/GB-hour; reported snapshot cost $0.067200.');
  const unknownCost = { ...snapshotCost, by_kind: { snapshot: { ...snapshotCost.by_kind.snapshot, storage_gb: null, rate_usd_per_gb_hour: null } } };
  expect(costSummary(unknownCost)).toContain('Snapshot storage estimate: unknown GB; rate unavailable;');
  expect(costSummary(unknownCost)).not.toContain('$null');
});
