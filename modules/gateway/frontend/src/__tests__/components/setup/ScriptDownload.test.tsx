/**
 * ScriptDownload tests — Issue #4146.
 *
 * Both prior entries were dead links (nothing served /api/cli/*, and bg-auth.ps1
 * had no source file at all). These tests pin the list to the one script the
 * backend actually serves.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ScriptDownload, ScriptDownloadList } from '@/components/setup/ScriptDownload';

afterEach(() => vi.restoreAllMocks());

describe('ScriptDownloadList', () => {
  it('lists exactly one script', () => {
    render(<ScriptDownloadList />);

    expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(1);
  });

  it('offers bg-cognito-auth.sh', () => {
    render(<ScriptDownloadList />);

    expect(screen.getByText('bg-cognito-auth.sh')).toBeInTheDocument();
  });

  it('does not offer the legacy or non-existent scripts', () => {
    render(<ScriptDownloadList />);
    const text = document.body.textContent ?? '';

    expect(screen.queryByText('bg-auth.sh')).not.toBeInTheDocument();
    expect(screen.queryByText('bg-auth.ps1')).not.toBeInTheDocument();
    expect(text).not.toContain('.ps1');
    expect(text).not.toContain('SSO');
  });

  it('downloads from the route the backend actually serves', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<ScriptDownloadList />);

    fireEvent.click(screen.getByRole('button', { name: 'Download' }));

    expect(open).toHaveBeenCalledWith('/api/cli/bg-cognito-auth.sh', '_blank');
  });
});

describe('ScriptDownload', () => {
  it('opens its download URL in a new tab', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(
      <ScriptDownload
        scriptName="bg-cognito-auth.sh"
        description="Cognito auth helper"
        platform="unix"
        downloadUrl="/api/cli/bg-cognito-auth.sh"
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Download' }));

    expect(open).toHaveBeenCalledWith('/api/cli/bg-cognito-auth.sh', '_blank');
  });
});
