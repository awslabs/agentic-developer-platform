/**
 * ScriptDownload tests — Issues #4146, #4156.
 *
 * Both original entries were dead links (nothing served /api/cli/*, and bg-auth.ps1
 * had no source file at all). These tests pin the list to exactly the scripts the
 * backend actually serves — now two, since the Codex `serve` flow needs
 * bg-gateway-proxy.py alongside the auth helper.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ScriptDownload, ScriptDownloadList } from '@/components/setup/ScriptDownload';

afterEach(() => vi.restoreAllMocks());

describe('ScriptDownloadList', () => {
  it('lists exactly the two scripts the backend serves', () => {
    render(<ScriptDownloadList />);

    expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(2);
  });

  it('offers bg-cognito-auth.sh', () => {
    render(<ScriptDownloadList />);

    expect(screen.getByText('bg-cognito-auth.sh')).toBeInTheDocument();
  });

  it('offers bg-gateway-proxy.py, the file `serve` needs (Issue #4156)', () => {
    render(<ScriptDownloadList />);

    expect(screen.getByText('bg-gateway-proxy.py')).toBeInTheDocument();
  });

  it('labels the proxy as Codex-only and tells the user serve starts it', () => {
    render(<ScriptDownloadList />);
    const text = document.body.textContent ?? '';

    expect(text).toContain('Codex only');
    expect(text).toContain('serve');
  });

  it('does not offer the legacy or non-existent scripts', () => {
    render(<ScriptDownloadList />);
    const text = document.body.textContent ?? '';

    expect(screen.queryByText('bg-auth.sh')).not.toBeInTheDocument();
    expect(screen.queryByText('bg-auth.ps1')).not.toBeInTheDocument();
    expect(text).not.toContain('.ps1');
    expect(text).not.toContain('SSO');
  });

  it.each([
    [0, '/api/cli/bg-cognito-auth.sh'],
    [1, '/api/cli/bg-gateway-proxy.py'],
  ])('button %i downloads from the route the backend actually serves', (index, url) => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<ScriptDownloadList />);

    fireEvent.click(screen.getAllByRole('button', { name: 'Download' })[index]);

    expect(open).toHaveBeenCalledWith(url, '_blank');
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
