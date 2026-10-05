import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { TranscriptMarkdown } from '@/components/TranscriptViewer';

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
