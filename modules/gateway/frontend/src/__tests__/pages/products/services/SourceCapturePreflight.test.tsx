import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SourceCapturePreflight from '@/pages/products/services/SourceCapturePreflight';
import { getSecuritySourcePreflight, type SourceCapturePreflightResult } from '@/services/l5Services';

vi.mock('@/services/l5Services', () => ({ getSecuritySourcePreflight: vi.fn() }));

const props = { productId: 'product', projectId: 'project', configurationId: 'config', configurationRevision: 3 };
const report: SourceCapturePreflightResult = {
  configuration_id: 'config', configuration_revision: 3, observed_at: '2026-11-01T00:00:00Z',
  status: 'exceeds_limits', limits: { max_files: 1000, max_bytes: 6291456 },
  advisory_only: true, content_captured: false, run_authority_checked: false, limitations: [],
  combined_repositories: { metadata_coverage: 'complete', known_selected_files: 1001, known_selected_bytes: 8000000,
    snapshot_archive_bytes: 8200000, native_combined_archive_bytes: null, reasons: [] },
  sources: [{ source_ref: { tenant_id: 'tenant', project_id: 'project', source_connection_id: 'github', record_type: 'repository', record_id: 'repo', record_revision: 'a'.repeat(40) },
    status: 'exceeds_limits', metadata_coverage: 'complete', coverage_scope: 'selected_paths',
    selected_files: 1001, selected_bytes: 8000000, archive_bytes: 8200000, excluded_entries: 3, exclusions: ['docs/'],
    reasons: ['source_file_count_exceeded', 'source_archive_bytes_exceeded'],
    oversized_files: [{ path: 'large.bin', bytes: 7000000 }], oversized_file_count: 1, oversized_files_truncated: false }],
};

describe('saved source capture preflight', () => {
  beforeEach(() => { vi.resetAllMocks(); vi.mocked(getSecuritySourcePreflight).mockResolvedValue(report); });

  it('checks metadata only after an explicit action and shows actionable partial coverage', async () => {
    render(<SourceCapturePreflight {...props} />);
    expect(getSecuritySourcePreflight).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Check source size' }));
    expect(getSecuritySourcePreflight).toHaveBeenCalledWith('product', 'project', 3);
    expect(await screen.findByText(/1,001 selected files · 8,000,000 content bytes · 8,200,000 archive bytes/)).toBeInTheDocument();
    expect(screen.getByText(/Partial repository selection: 3 entries are excluded/)).toBeInTheDocument();
    expect(screen.getByText(/Changing exclusions reduces assessment coverage; no changes have been applied/)).toBeInTheDocument();
    expect(screen.getByText('large.bin')).toBeInTheDocument();
    expect(screen.getByText(/not a security assessment or permission to run/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start|apply|save/i })).not.toBeInTheDocument();
  });

  it('does not display partial counts for an incomplete tree', async () => {
    vi.mocked(getSecuritySourcePreflight).mockResolvedValue({ ...report, status: 'unknown', sources: [{
      source_ref: report.sources[0].source_ref, status: 'unknown', metadata_coverage: 'unknown', reasons: ['source_metadata_incomplete'],
    }] });
    render(<SourceCapturePreflight {...props} />);
    await userEvent.click(screen.getByRole('button', { name: 'Check source size' }));
    expect(await screen.findByText(/No complete size estimate is available/)).toBeInTheDocument();
    expect(screen.queryByText(/selected files/)).not.toBeInTheDocument();
    expect(screen.queryByText('large.bin')).not.toBeInTheDocument();
  });

  it('discards an in-flight result when the saved configuration changes', async () => {
    let finish!: (value: SourceCapturePreflightResult) => void;
    vi.mocked(getSecuritySourcePreflight).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const rendered = render(<SourceCapturePreflight {...props} />);
    await userEvent.click(screen.getByRole('button', { name: 'Check source size' }));
    rendered.rerender(<SourceCapturePreflight {...props} configurationRevision={4} />);
    await act(async () => { finish(report); });
    expect(screen.queryByText('large.bin')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('shows cumulative limits even when each repository fits individually', async () => {
    const source = { ...report.sources[0], status: 'within_limits' as const, selected_files: 900, reasons: [], oversized_files: [] };
    vi.mocked(getSecuritySourcePreflight).mockResolvedValue({ ...report,
      sources: [source, { ...source, source_ref: { ...source.source_ref, record_id: 'second-repo' } }],
      combined_repositories: { ...report.combined_repositories, known_selected_files: 1800, known_selected_bytes: 1800,
        reasons: ['combined_source_file_count_exceeded'] },
    });
    render(<SourceCapturePreflight {...props} />);
    await userEvent.click(screen.getByRole('button', { name: 'Check source size' }));
    expect(await screen.findByText('The saved repositories together exceed the file limit for one run.')).toBeInTheDocument();
    expect(screen.getByText(/1,800 files and 1,800 content bytes against the shared per-run limits/)).toBeInTheDocument();
    expect(screen.getByText(/compressed native combined archive size is unknown without capturing content/)).toBeInTheDocument();
  });

  it('withholds stale or inaccessible results and supports retry', async () => {
    vi.mocked(getSecuritySourcePreflight).mockResolvedValue({ ...report, configuration_id: 'another-config' });
    render(<SourceCapturePreflight {...props} />);
    await userEvent.click(screen.getByRole('button', { name: 'Check source size' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh the saved setup, verify source access, and try again');
    expect(screen.queryByText('large.bin')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check source size' })).toBeEnabled();
  });

  it('disables the check while the enclosing setup has unsaved changes', () => {
    render(<SourceCapturePreflight {...props} disabled />);
    expect(screen.getByRole('button', { name: 'Check source size' })).toBeDisabled();
    expect(getSecuritySourcePreflight).not.toHaveBeenCalled();
  });
});
