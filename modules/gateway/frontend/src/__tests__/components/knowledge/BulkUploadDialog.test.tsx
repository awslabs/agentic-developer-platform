/**
 * Component tests for BulkUploadDialog.
 *
 * Issue #1795 (Story F of E10 #1736).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BulkUploadDialog } from '@/components/knowledge/BulkUploadDialog';

// Mock knowledge service
vi.mock('@/services/knowledge', () => ({
  bulkPreview: vi.fn(),
  bulkCommit: vi.fn(),
}));

import { bulkPreview, bulkCommit } from '@/services/knowledge';

const mockBulkPreview = bulkPreview as ReturnType<typeof vi.fn>;
const mockBulkCommit = bulkCommit as ReturnType<typeof vi.fn>;

const mockPreviewResponse = {
  total_lines: 5,
  parsed: 4,
  skipped_comments: 1,
  valid: [
    {
      line: 1,
      source_ref: 'https://github.com/acme/repo-a',
      asset_type: 'repo',
      display_name: 'acme/repo-a',
      tags: {},
    },
    {
      line: 2,
      source_ref: 'https://docs.example.com/api',
      asset_type: 'url',
      display_name: 'API Docs',
      tags: {},
    },
  ],
  rejected: [
    {
      line: 3,
      source_ref: 'ftp://invalid.host/file',
      reason: 'Unsupported protocol',
    },
  ],
  duplicates: [
    {
      line: 4,
      source_ref: 'https://github.com/acme/existing',
      existing_id: 'asset-existing-001',
    },
  ],
  quota_ok: true,
  quota_after: {
    repo: { used: 3, limit: 200 },
    url: { used: 2, limit: 500 },
  },
};

const mockCommitResponse = {
  created: 2,
  skipped_duplicates: 0,
  assets: [],
};

function createTestFile(content = 'https://github.com/acme/repo-a\nhttps://docs.example.com/api') {
  return new File([content], 'assets.txt', { type: 'text/plain' });
}

const defaultProps = {
  isOpen: true,
  onClose: vi.fn(),
  onAssetsAdded: vi.fn(),
  scope: 'tenant' as const,
};

describe('BulkUploadDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockBulkPreview.mockResolvedValue(mockPreviewResponse);
    mockBulkCommit.mockResolvedValue(mockCommitResponse);
  });

  it('renders the dialog with dropzone when open', () => {
    render(<BulkUploadDialog {...defaultProps} />);

    expect(screen.getByText('Bulk Upload')).toBeInTheDocument();
    expect(screen.getByText('Drop a file here or click to select')).toBeInTheDocument();
  });

  it('does not render content when closed', () => {
    render(<BulkUploadDialog {...defaultProps} isOpen={false} />);

    expect(screen.queryByText('Bulk Upload')).not.toBeInTheDocument();
  });

  it('shows file name after selecting a file', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);

    expect(screen.getByText('assets.txt')).toBeInTheDocument();
  });

  it('calls bulkPreview when Preview button is clicked', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);

    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(mockBulkPreview).toHaveBeenCalledWith(file, 'tenant');
    });
  });

  it('renders the preview table with valid items', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('2 valid')).toBeInTheDocument();
    });

    expect(screen.getByText('https://github.com/acme/repo-a')).toBeInTheDocument();
    expect(screen.getByText('https://docs.example.com/api')).toBeInTheDocument();
  });

  it('renders rejected items in the preview', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('1 rejected')).toBeInTheDocument();
    });

    expect(screen.getByText('Unsupported protocol')).toBeInTheDocument();
  });

  it('renders duplicate items in the preview', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('1 duplicates')).toBeInTheDocument();
    });

    expect(screen.getByText('https://github.com/acme/existing')).toBeInTheDocument();
  });

  it('shows quota info in the preview', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('repo: 3/200')).toBeInTheDocument();
    });
    expect(screen.getByText('url: 2/500')).toBeInTheDocument();
  });

  it('shows quota warning when quota_ok is false', async () => {
    mockBulkPreview.mockResolvedValue({ ...mockPreviewResponse, quota_ok: false });
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(
        screen.getByText('Quota exceeded — some assets may not be created.'),
      ).toBeInTheDocument();
    });
  });

  it('calls bulkCommit when Commit button is clicked', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('Commit 2 Assets')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Commit 2 Assets'));

    await waitFor(() => {
      expect(mockBulkCommit).toHaveBeenCalledWith({
        items: [
          {
            source_ref: 'https://github.com/acme/repo-a',
            asset_type: 'repo',
            display_name: 'acme/repo-a',
            tags: {},
          },
          {
            source_ref: 'https://docs.example.com/api',
            asset_type: 'url',
            display_name: 'API Docs',
            tags: {},
          },
        ],
        scope: 'tenant',
      });
    });
  });

  it('shows success message after commit', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('Commit 2 Assets')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Commit 2 Assets'));

    await waitFor(() => {
      expect(screen.getByText('2 assets queued for indexing.')).toBeInTheDocument();
    });
  });

  it('calls onAssetsAdded after successful commit', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('Commit 2 Assets')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Commit 2 Assets'));

    await waitFor(() => {
      expect(defaultProps.onAssetsAdded).toHaveBeenCalled();
    });
  });

  it('shows error when preview fails', async () => {
    mockBulkPreview.mockRejectedValue({ detail: 'File too large' });
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('File too large')).toBeInTheDocument();
    });
  });

  it('shows error when commit fails', async () => {
    mockBulkCommit.mockRejectedValue({ detail: 'Quota exceeded' });
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('Commit 2 Assets')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Commit 2 Assets'));

    await waitFor(() => {
      expect(screen.getByText('Quota exceeded')).toBeInTheDocument();
    });
  });

  it('disables Preview button when no file is selected', () => {
    render(<BulkUploadDialog {...defaultProps} />);

    const previewBtn = screen.getByText('Preview');
    expect(previewBtn).toBeDisabled();
  });

  it('allows going back from preview to upload phase', async () => {
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('Back')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Back'));

    expect(screen.getByText('Drop a file here or click to select')).toBeInTheDocument();
  });

  it('handles file drop', async () => {
    render(<BulkUploadDialog {...defaultProps} />);

    const dropzone = screen.getByText('Drop a file here or click to select').closest('[role="button"]')!;
    const file = createTestFile();

    const dataTransfer = {
      files: [file],
      types: ['Files'],
    };

    fireEvent.dragEnter(dropzone, { dataTransfer });
    fireEvent.dragOver(dropzone, { dataTransfer });
    fireEvent.drop(dropzone, { dataTransfer });

    expect(screen.getByText('assets.txt')).toBeInTheDocument();
  });

  it('shows skipped duplicates in success message', async () => {
    mockBulkCommit.mockResolvedValue({ created: 2, skipped_duplicates: 1, assets: [] });
    const user = userEvent.setup();
    render(<BulkUploadDialog {...defaultProps} />);

    const file = createTestFile();
    const input = screen.getByTestId('bulk-file-input');
    await user.upload(input, file);
    await user.click(screen.getByText('Preview'));

    await waitFor(() => {
      expect(screen.getByText('Commit 2 Assets')).toBeInTheDocument();
    });

    await user.click(screen.getByText('Commit 2 Assets'));

    await waitFor(() => {
      expect(screen.getByText('1 duplicate skipped.')).toBeInTheDocument();
    });
  });
});
