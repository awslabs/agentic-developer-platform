import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { PlatformPersonaDefaults } from '@/pages/settings/PlatformPersonaDefaults';
import { apiClient } from '@/services/api';
vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), put: vi.fn() } }));
const entry = {
  persona_key: 'gpt-developer', display_name: 'Codex Developer', compatibility_class: 'codex-sdk',
  canonical_model_id: null, inherited_model_id: null, recommended_model_id: 'openai.gpt-6-sol', revision: 0,
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(apiClient.get).mockResolvedValue({ entries: [entry], models: { 'codex-sdk': [{ id: 'openai.gpt-6-sol', label: 'GPT Sol' }] } });
});
async function fill() {
  fireEvent.click(screen.getByText('Platform persona defaults'));
  fireEvent.change(await screen.findByLabelText('Codex Developer'), { target: { value: 'openai.gpt-6-sol' } });
  fireEvent.change(screen.getByLabelText('Reason for Codex Developer default'), { target: { value: 'Developer baseline' } });
  fireEvent.click(screen.getByText('Save platform default'));
}
it('saves with revision and refreshes effective preferences', async () => {
  const onSaved = vi.fn();
  vi.mocked(apiClient.put).mockResolvedValue({ ...entry, canonical_model_id: 'openai.gpt-6-sol', revision: 1 });
  render(<PlatformPersonaDefaults onSaved={onSaved} />);
  await fill();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(apiClient.put).toHaveBeenCalledWith('/admin/persona-defaults/gpt-developer', {
    canonical_model_id: 'openai.gpt-6-sol', expected_revision: 0, reason: 'Developer baseline', operation_id: expect.any(String),
  });
});
it('shows a rejected save without reporting success', async () => {
  const onSaved = vi.fn();
  vi.mocked(apiClient.put).mockRejectedValue({ detail: { reason: 'default_revision_conflict' } });
  render(<PlatformPersonaDefaults onSaved={onSaved} />);
  await fill();
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(onSaved).not.toHaveBeenCalled();
});
