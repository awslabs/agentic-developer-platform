import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { runtimeChoiceFor, sessionSelectionFor, useCrewRuntime } from '@/hooks/useCrewRuntime';
import { apiClient } from '@/services/api';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn() } }));

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(apiClient.get).mockReset();
});

afterEach(() => window.localStorage.clear());

it('reveals the Crew selection only after a successful policy read', async () => {
  let finish!: (value: { allowed_targets: string[] }) => void;
  vi.mocked(apiClient.get).mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const { result } = renderHook(useCrewRuntime);
  expect(result.current.loading).toBe(true);
  expect(result.current.allowed).toBe(false);
  finish({ allowed_targets: ['eks-pod'] });
  await waitFor(() => expect(result.current.allowed).toBe(true));
  expect(result.current.hostAllowed).toBe(false);
  act(() => result.current.setChoice('kirocrew-eks'));
  await waitFor(() => expect(result.current.choice).toBe('kirocrew-eks'));
  expect(apiClient.get).toHaveBeenCalledWith('/crew/runtime');
});

it('hides a saved Crew choice when policy denies it or the request fails', async () => {
  window.localStorage.setItem('adp_crew_runtime', JSON.stringify('kirocrew-eks'));
  vi.mocked(apiClient.get).mockResolvedValueOnce({ allowed_targets: ['ec2-instance'] });
  const first = renderHook(useCrewRuntime);
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  expect(first.result.current.choice).toBe('adp-assistant');
  expect(first.result.current.hostAllowed).toBe(true);
  first.unmount();
  vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('backend unavailable'));
  const second = renderHook(useCrewRuntime);
  await waitFor(() => expect(second.result.current.loading).toBe(false));
  expect(second.result.current.allowed).toBe(false);
  expect(second.result.current.hostAllowed).toBe(false);
  expect(second.result.current.choice).toBe('adp-assistant');
});

it('retains the EC2 choice only while EC2 is permitted', async () => {
  window.localStorage.setItem('adp_crew_runtime', JSON.stringify('kirocrew-ec2'));
  vi.mocked(apiClient.get).mockResolvedValueOnce({ allowed_targets: ['ec2-instance'] });
  const first = renderHook(useCrewRuntime);
  await waitFor(() => expect(first.result.current.choice).toBe('kirocrew-ec2'));
  first.unmount();
  vi.mocked(apiClient.get).mockResolvedValueOnce({ allowed_targets: ['eks-pod'] });
  const second = renderHook(useCrewRuntime);
  await waitFor(() => expect(second.result.current.loading).toBe(false));
  expect(second.result.current.choice).toBe('adp-assistant');
});

it('always offers Claude Code in a sandbox and keeps that choice without a Crew policy grant', async () => {
  window.localStorage.setItem('adp_crew_runtime', JSON.stringify('claude-code'));
  vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('backend unavailable'));
  const { result } = renderHook(useCrewRuntime);
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.choice).toBe('claude-code');
  expect(result.current.options.map(option => option.value)).toEqual(['adp-assistant', 'claude-code']);
});

it('maps runtime choices to the session mode and executor the gateway stores (#8115)', () => {
  expect(sessionSelectionFor('adp-assistant')).toEqual({ mode: 'ephemeral' });
  expect(sessionSelectionFor('claude-code')).toEqual({ mode: 'persistent', executor: 'claude-code' });
  expect(sessionSelectionFor('kirocrew-eks')).toEqual({ mode: 'persistent', executor: 'kirocrew' });
  expect(sessionSelectionFor('kirocrew-ec2')).toEqual({ mode: 'ephemeral' });
});

it('derives the select value from the server-owned session before the local preference', () => {
  expect(runtimeChoiceFor(null, 'claude-code')).toBe('adp-assistant');
  expect(runtimeChoiceFor({ mode: 'ephemeral' }, 'kirocrew-ec2')).toBe('kirocrew-ec2');
  expect(runtimeChoiceFor({ mode: 'ephemeral' }, 'kirocrew-eks')).toBe('adp-assistant');
  expect(runtimeChoiceFor({ mode: 'persistent', executor: 'claude-code' }, 'adp-assistant')).toBe('claude-code');
  expect(runtimeChoiceFor({ mode: 'persistent', executor: 'kirocrew' }, 'adp-assistant')).toBe('kirocrew-eks');
  expect(runtimeChoiceFor({ mode: 'persistent' }, 'claude-code')).toBe('persistent-default');
  expect(runtimeChoiceFor({ mode: 'ephemeral', pending_mode: 'persistent', executor: 'claude-code' }, 'adp-assistant')).toBe('claude-code');
});
