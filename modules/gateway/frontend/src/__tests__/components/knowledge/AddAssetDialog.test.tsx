/**
 * Component tests for AddAssetDialog — public-repo-by-URL path.
 *
 * Follow-up to #2213 UI gap: public/OSS repos can't be enumerated by the
 * installed-App picker, so the Repo tab accepts a pasted GitHub URL and
 * registers it directly (backend treats public repos as shared scope).
 *
 * Tests:
 * - Pasting a public repo URL → createAsset called with that source_ref
 * - Invalid (non-GitHub) URL → validation error, createAsset NOT called
 * - Empty repo tab (no URL, no selection) → error, createAsset NOT called
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AddAssetDialog } from '@/components/knowledge/AddAssetDialog';

vi.mock('@/services/knowledge', () => ({
  getAccessibleRepos: vi.fn(),
  createAsset: vi.fn(),
}));

import { getAccessibleRepos, createAsset } from '@/services/knowledge';

const mockGetAccessibleRepos = getAccessibleRepos as ReturnType<typeof vi.fn>;
const mockCreateAsset = createAsset as ReturnType<typeof vi.fn>;

function renderDialog() {
  return render(
    <AddAssetDialog
      isOpen
      onClose={vi.fn()}
      onAssetAdded={vi.fn()}
      scope="tenant"
    />,
  );
}

describe('AddAssetDialog — public repo by URL', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // No installed repos — exactly the state that previously dead-ended OSS adds.
    mockGetAccessibleRepos.mockResolvedValue({ repos: [], total: 0, page: 1, hasMore: false });
    mockCreateAsset.mockResolvedValue({ id: 'asset-1', status: 'queued' });
  });

  it('registers a pasted public GitHub repo URL', async () => {
    const user = userEvent.setup();
    renderDialog();

    const urlBox = await screen.findByPlaceholderText('https://github.com/owner/repo');
    await user.type(urlBox, 'https://github.com/HKUDS/DeepTutor');
    await user.click(screen.getByRole('button', { name: /add/i }));

    await waitFor(() => {
      expect(mockCreateAsset).toHaveBeenCalledWith(
        expect.objectContaining({
          asset_type: 'repo',
          source_ref: 'https://github.com/HKUDS/DeepTutor',
          display_name: 'HKUDS/DeepTutor',
          scope: 'tenant',
        }),
      );
    });
  });

  it('rejects a non-GitHub URL without calling the API', async () => {
    const user = userEvent.setup();
    renderDialog();

    const urlBox = await screen.findByPlaceholderText('https://github.com/owner/repo');
    await user.type(urlBox, 'https://gitlab.com/foo/bar');
    await user.click(screen.getByRole('button', { name: /add/i }));

    await waitFor(() => {
      expect(screen.getByText(/GitHub repo URL/i)).toBeInTheDocument();
    });
    expect(mockCreateAsset).not.toHaveBeenCalled();
  });

  it('errors when neither a URL nor a picker selection is provided', async () => {
    const user = userEvent.setup();
    renderDialog();

    await screen.findByPlaceholderText('https://github.com/owner/repo');
    await user.click(screen.getByRole('button', { name: /add/i }));

    await waitFor(() => {
      expect(screen.getByText(/paste a public GitHub repo URL/i)).toBeInTheDocument();
    });
    expect(mockCreateAsset).not.toHaveBeenCalled();
  });
});
