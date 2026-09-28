import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LiveExplanations } from '@/components/LiveExplanations';
import { readExplanations, type StreamUpdate } from '@/services/agentExplanations';
const flags = vi.hoisted(() => ({ data: { agent_explanations: true, agent_control: false }, isPending: false, isError: false }));
vi.mock('@/hooks/useFeatures', () => ({ useRevalidatingFeaturesQuery: () => flags }));
vi.mock('@/services/agentExplanations', async () => ({
  ...await vi.importActual<typeof import('@/services/agentExplanations')>('@/services/agentExplanations'), readExplanations: vi.fn(),
}));
let send: (event: StreamUpdate) => void, signal: AbortSignal;
function explanation(sequence: number, text: string): StreamUpdate {
  return { kind: 'explanation', cursor: `run:1:${sequence}`, event: { version: 1, invocation_id: 'run', generation: 1,
    sequence, timestamp: '2026-09-24T10:00:00Z', kind: 'explanation', payload: { text } } };
}
beforeEach(() => {
  vi.clearAllMocks(); flags.data.agent_explanations = true; flags.isError = false; flags.isPending = false;
  vi.mocked(readExplanations).mockImplementation((_id, _cursor, abort, callback) => {
    send = callback; signal = abort;
    return new Promise(resolve => abort.addEventListener('abort', () => resolve()));
  });
});
describe('live explanations', () => {
  it('shows explanations with controls off, deduplicates and never autoscrolls', () => {
    const scroll = vi.fn(); Element.prototype.scrollIntoView = scroll;
    render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    act(() => { send(explanation(1, 'Mechanism: bounded replay.')); send(explanation(1, 'duplicate')); send(explanation(2, 'Evidence: source tests.')); });
    expect(screen.getByText('Mechanism: bounded replay.')).toBeInTheDocument();
    expect(screen.queryByText('duplicate')).not.toBeInTheDocument();
    expect(scroll).not.toHaveBeenCalled();
    screen.getByRole('button', { name: /Jump to latest/ }).click();
    expect(scroll).toHaveBeenCalledOnce();
  });
  it('keeps heartbeat freshness separate and safely renders hostile text', () => {
    const { container } = render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    act(() => send({ kind: 'heartbeat' }));
    expect(screen.queryByText(/Last explanation:/)).not.toBeInTheDocument();
    act(() => send(explanation(1, '<script>alert(1)</script>\n[bad](javascript:alert(1))\n![track](https://evil.test/pixel)')));
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('a')?.getAttribute('href')).not.toContain('javascript:');
  });
  it('aborts on close and run switch and renders an explicit gap', () => {
    const view = render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    const first = signal;
    act(() => { send(explanation(1, 'First')); send({ kind: 'reset' }); send(explanation(9, 'Retained')); });
    expect(screen.getByText(/Some live history/)).toBeInTheDocument();
    expect(screen.queryByText('First')).not.toBeInTheDocument();
    view.rerender(<LiveExplanations invocationId="other" isOpen terminal={false} />);
    expect(first.aborted).toBe(true);
    view.unmount(); expect(signal.aborted).toBe(true);
  });
  it.each(['isPending', 'isError'] as const)('fails closed when %s', field => {
    flags[field] = true;
    render(<LiveExplanations invocationId="run" isOpen terminal={false} />);
    expect(readExplanations).not.toHaveBeenCalled();
  });
  it('retains transcript guidance on terminal runs without opening a stream', () => {
    render(<LiveExplanations invocationId="run" isOpen terminal />);
    expect(readExplanations).not.toHaveBeenCalled();
    expect(screen.getByText(/final transcript available/)).toBeInTheDocument();
  });
});
