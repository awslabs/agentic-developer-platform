import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { TranscriptMarkdown, TranscriptContent } from '@/components/TranscriptViewer';
import { getMyTranscript } from '@/services/activity';
vi.mock('@/services/activity', () => ({ getMyTranscript: vi.fn(), getAdminTranscript: vi.fn() }));

describe('saved transcript presentation', () => {
  it('preserves headings, nested lists, tables and exact command whitespace', () => {
    const command = 'printf "hello"\n  echo done';
    const { container } = render(<TranscriptMarkdown markdown={`# Run\n\n## Implementation walkthrough\n\n### Update 2\n\n- First\n  - Nested\n\n\`\`\`bash\n${command}\n\`\`\`\n\n| Check | Result |\n| --- | --- |\n| Tests | Passed |`} />);
    expect(screen.getByRole('heading', { name: 'Implementation walkthrough' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Update 2' })).toBeInTheDocument();
    expect(container.querySelector('ul ul')).toHaveTextContent('Nested');
    expect(screen.getByLabelText('Code or command output').textContent).toBe(command + '\n');
    expect(screen.getByRole('region', { name: 'Transcript table' })).toContainElement(screen.getByRole('table'));
  });

  it('keeps agent HTML inert and rejects executable links', () => {
    const { container } = render(<TranscriptMarkdown markdown={'<script>alert(1)</script>\n\n[unsafe](javascript:alert%281%29)\n\n[PR](https://github.com/aws-e/adp/pull/6625)'} />);
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText('unsafe').getAttribute('href')).not.toMatch(/^javascript:/);
    expect(screen.getByRole('link', { name: 'PR' })).toHaveAttribute('href', 'https://github.com/aws-e/adp/pull/6625');
  });
});


it('hides the machine record envelope from the rendered transcript', () => {
  render(<TranscriptMarkdown markdown={'<!-- adp-run-record:v1 eyJ2ZXJzaW9uIjoxfQ== -->\n\n# Recorded work'} />);
  expect(screen.getByRole('heading', { name: 'Recorded work' })).toBeInTheDocument();
  expect(screen.queryByText(/adp-run-record/)).not.toBeInTheDocument();
});

it('places the closure report above the saved checklist and transcript', async () => {
  const at = '2026-10-05T20:00:00Z';
  const record = { version: 1, invocation_id: 'closure-run', persona: 'reviewer', model: 'test', repository: 'org/repo', issue: 1,
    started_at: at, captured_at: at, session_ids: [], task_transitions: [], history_truncated: false, evidence: [],
    closure_report: { summary: 'Workspace UI validation is complete.', delivery: 'Pull request merged.',
      completed: ['Browser checks passed.'], remaining: ['Live acceptance remains.'], reporting_notes: [] } };
  vi.mocked(getMyTranscript).mockResolvedValueOnce('<!-- adp-run-record:v1 ' + btoa(JSON.stringify(record)) + ' -->\n\n# Original activity');
  render(<QueryClientProvider client={new QueryClient()}><TranscriptContent invocationId="closure-run" /></QueryClientProvider>);
  const closure = await screen.findByRole('region', { name: 'Closure report' });
  const saved = screen.getByRole('complementary', { name: 'Retained run record' });
  const transcript = screen.getByRole('article', { name: 'Run transcript' });
  expect(closure.compareDocumentPosition(saved) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(closure.compareDocumentPosition(transcript) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(closure).toHaveTextContent('Live acceptance remains.');
});
