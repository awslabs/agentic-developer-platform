import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ALL_FEATURES_ENABLED } from '@/services/features';
import AssistantActivityChat from '@/pages/AssistantActivityChat';
import { getMyInvocationDetail } from '@/services/activity';

vi.mock('@/services/activity', () => ({ getMyInvocationDetail: vi.fn() }));
vi.mock('@/pages/AgentChat', () => ({ default: ({ activityId }: { activityId: string }) => <p>Chat for {activityId}</p> }));
let assistantEnabled = false;
let featuresPending = false;
let featuresError = false;
vi.mock('@/hooks/useFeatures', () => ({
  useRevalidatingFeaturesQuery: () => ({ data: { ...ALL_FEATURES_ENABLED, assistant_ui: assistantEnabled }, isPending: featuresPending, isError: featuresError }),
}));

function renderEntry(invocationId: string) {
  return render(entryRoute(invocationId));
}

function entryRoute(invocationId: string) {
  return <MemoryRouter initialEntries={[`/assistant/activity/${invocationId}`]}><Routes>
    <Route path="/" element={<p>Unavailable</p>} />
    <Route path="/assistant/activity/:invocationId" element={
      <AssistantActivityChat />
    } />
  </Routes></MemoryRouter>;
}

describe('assistant contextual entry', () => {
  beforeEach(() => {
    assistantEnabled = false;
    featuresPending = false;
    featuresError = false;
    vi.mocked(getMyInvocationDetail).mockReset();
  });

  it('keeps the new route closed when the backend flag is off', () => {
    renderEntry('run-1');
    expect(screen.getByText('Unavailable')).toBeInTheDocument();
    expect(getMyInvocationDetail).not.toHaveBeenCalled();
  });

  it('resolves the reference against the caller-scoped endpoint before opening chat', async () => {
    assistantEnabled = true;
    vi.mocked(getMyInvocationDetail).mockResolvedValue({ invocation_id: 'run-1' } as Awaited<ReturnType<typeof getMyInvocationDetail>>);
    renderEntry('run-1');
    expect(screen.getByRole('status')).toHaveTextContent('Checking activity access');
    expect(await screen.findByText('Chat for run-1')).toBeInTheDocument();
    expect(getMyInvocationDetail).toHaveBeenCalledWith('run-1');
  });

  it('does not render chat for a denied or mismatched direct URL', async () => {
    assistantEnabled = true;
    vi.mocked(getMyInvocationDetail).mockRejectedValueOnce(new Error('404'));
    const page = renderEntry('someone-elses-run');
    expect(await screen.findByRole('alert')).toHaveTextContent('not available');
    expect(screen.queryByText(/Chat for/)).not.toBeInTheDocument();
    page.unmount();
    vi.mocked(getMyInvocationDetail).mockResolvedValueOnce({ invocation_id: 'another-run' } as Awaited<ReturnType<typeof getMyInvocationDetail>>);
    renderEntry('someone-elses-run');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('not available'));
    expect(screen.queryByText(/Chat for/)).not.toBeInTheDocument();
  });

  it('withholds direct navigation while availability is still loading', () => {
    assistantEnabled = true;
    featuresPending = true;
    renderEntry('run-1');
    expect(screen.getByRole('status')).toHaveTextContent('Checking assistant availability');
    expect(getMyInvocationDetail).not.toHaveBeenCalled();
  });

  it.each(['flag withdrawn', 'backend unavailable'])('withdraws an open chat when %s', async (failure) => {
    assistantEnabled = true;
    vi.mocked(getMyInvocationDetail).mockResolvedValue({ invocation_id: 'run-1' } as Awaited<ReturnType<typeof getMyInvocationDetail>>);
    const page = renderEntry('run-1');
    expect(await screen.findByText('Chat for run-1')).toBeInTheDocument();
    if (failure === 'flag withdrawn') assistantEnabled = false;
    else featuresError = true;
    page.rerender(entryRoute('run-1'));
    await waitFor(() => expect(screen.getByText('Unavailable')).toBeInTheDocument());
    expect(screen.queryByText('Chat for run-1')).not.toBeInTheDocument();
  });
});
