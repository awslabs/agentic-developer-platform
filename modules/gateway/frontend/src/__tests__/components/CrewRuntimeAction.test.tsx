import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import CrewRuntimeAction from '@/components/chat/CrewRuntimeAction';

import { RUNTIME_OPTIONS } from '@/hooks/useCrewRuntime';

type Runtime = { choice: string; allowed: boolean; hostAllowed: boolean; loading: boolean; setChoice: ReturnType<typeof vi.fn> };
const withOptions = (runtime: Runtime) => ({ ...runtime,
  options: RUNTIME_OPTIONS.filter(option => !option.requires || (option.requires === 'eks-pod' ? runtime.allowed : runtime.hostAllowed)) });
let runtime: Runtime = { choice: 'adp-assistant', allowed: false, hostAllowed: false, loading: false, setChoice: vi.fn() };
vi.mock('@/hooks/useCrewRuntime', async () => {
  const actual = await vi.importActual<typeof import('@/hooks/useCrewRuntime')>('@/hooks/useCrewRuntime');
  return { ...actual, useCrewRuntime: () => withOptions(runtime) };
});
vi.mock('@/components/chat/OpenMyCrew', () => ({ default: () => <button>Open my Crew</button> }));
vi.mock('@/components/chat/OpenMyCrewBox', () => ({ default: () => <button>Open my EC2 Crew</button> }));

beforeEach(() => {
  runtime = { choice: 'adp-assistant', allowed: false, hostAllowed: false, loading: false, setChoice: vi.fn() };
});

it('does not show the Crew action without a policy-permitted runtime selection', () => {
  const view = render(<CrewRuntimeAction handoffUrl="https://crew.example.test/crew/handoff" />);
  expect(screen.queryByRole('button', { name: 'Open my Crew' })).not.toBeInTheDocument();
  runtime = { ...runtime, choice: 'kirocrew-eks', allowed: true };
  view.rerender(<CrewRuntimeAction handoffUrl="https://crew.example.test/crew/handoff" />);
  expect(screen.getByRole('button', { name: 'Open my Crew' })).toBeInTheDocument();
  runtime = { ...runtime, allowed: false };
  view.rerender(<CrewRuntimeAction handoffUrl="https://crew.example.test/crew/handoff" />);
  expect(screen.queryByRole('button', { name: 'Open my Crew' })).not.toBeInTheDocument();
});

// The runtime chooser itself lives on the chat page (AgentChat); the Settings → Runtime page was folded
// into Dev Box (#8864), so only the action's gating is asserted here.
it('shows the EC2 action only for a permitted host runtime', () => {
  runtime = { ...runtime, hostAllowed: true, choice: 'kirocrew-ec2' };
  const action = render(<CrewRuntimeAction handoffUrl="https://crew.example.test/crew/handoff" />);
  expect(screen.getByRole('button', { name: 'Open my EC2 Crew' })).toBeInTheDocument();
  runtime = { ...runtime, hostAllowed: false };
  action.rerender(<CrewRuntimeAction handoffUrl="https://crew.example.test/crew/handoff" />);
  expect(screen.queryByRole('button', { name: 'Open my EC2 Crew' })).not.toBeInTheDocument();
});
