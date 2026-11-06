import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import RunAccess from '@/pages/products/services/RunAccess';
import { advanceRunPreparation, beginRunPreparation, readRunPreparation, retainRunPreparation, type RunPreparation, type PreparationProgress } from '@/services/l5RunPreparation';
import { configuration, binding } from '../../../fixtures/devOpsActivation';
vi.mock('@/services/l5RunPreparation', () => ({ advanceRunPreparation: vi.fn(), beginRunPreparation: vi.fn(), readRunPreparation: vi.fn(), retainRunPreparation: vi.fn() }));
const saved = { value: configuration, etag: '"config-hash"' };
const journal = { version: 1, service: 'devops', original: saved, binding: { value: { ...binding, readiness: [] }, etag: '"4"' }, plan: 'devops-resources', effects: {} } as RunPreparation;
beforeEach(() => { vi.resetAllMocks(); vi.mocked(readRunPreparation).mockReturnValue(null); vi.mocked(beginRunPreparation).mockResolvedValue(journal); });
describe('run access recovery panel', () => {
  it('does not renew access on mount or before the owner reviews it', async () => {
    render(<RunAccess service="devops" configuration={saved} bindingId="binding" onReady={vi.fn()} />);
    expect(beginRunPreparation).not.toHaveBeenCalled(); expect(advanceRunPreparation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh run access' }));
    await screen.findByRole('button', { name: 'Review access' });
    expect(advanceRunPreparation).not.toHaveBeenCalled(); expect(retainRunPreparation).toHaveBeenCalledWith(journal);
  });
  it('retains in-flight workflow receipts after leaving the view without delivering stale completion', async () => {
    let persist!: (value: RunPreparation) => void;
    let finish!: (value: PreparationProgress) => void;
    vi.mocked(advanceRunPreparation).mockImplementation((_journal, save) => { persist = save; return new Promise(resolve => { finish = resolve; }); });
    const ready = vi.fn();
    const view = render(<RunAccess service="devops" configuration={saved} bindingId="binding" onReady={ready} />);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh run access' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Review access' }));
    await waitFor(() => expect(advanceRunPreparation).toHaveBeenCalled());
    view.unmount();
    const pending = { ...journal, effects: { draft: { key: 'original-draft-receipt', etag: saved.etag, body: { enabled: false } } } };
    await act(async () => { persist(pending); finish({ kind: 'done', configuration: saved }); });
    expect(retainRunPreparation).toHaveBeenLastCalledWith(pending);
    expect(ready).not.toHaveBeenCalled();
  });
});

it('resumes a receipt retained by another mounted section before creating new access', async () => {
  render(<><RunAccess service="devops" configuration={saved} bindingId="binding" onReady={vi.fn()} />
    <RunAccess service="devops" configuration={saved} bindingId="binding" onReady={vi.fn()} /></>);
  const pending = { ...journal, effects: { check: { key: 'retained-verification-receipt', etag: '"4"', body: { action: 'check' } } } };
  vi.mocked(readRunPreparation).mockReturnValue(pending);
  vi.mocked(advanceRunPreparation).mockResolvedValue({ kind: 'checking' });
  fireEvent.click(screen.getAllByRole('button', { name: 'Refresh run access' })[1]);
  fireEvent.click(await screen.findByRole('button', { name: 'Review access' }));
  await waitFor(() => expect(advanceRunPreparation).toHaveBeenCalledWith(pending, expect.any(Function), undefined, undefined));
  expect(beginRunPreparation).not.toHaveBeenCalled();
});
