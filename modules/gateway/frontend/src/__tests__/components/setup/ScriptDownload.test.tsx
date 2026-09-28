/**
 * ScriptDownload tests — Issues #4146, #4156, #4852.
 *
 * Both original entries were dead links (nothing served /api/cli/*, and bg-auth.ps1
 * had no source file at all). These tests pin the list to exactly the files the
 * backend actually serves — four since #4852 added `adp` and its installer, which
 * are the primary flow's payload even though the cards themselves are a fallback.
 *
 * The list here and ALLOWED_SCRIPTS in src/cli_download/routes.py must agree; a
 * card whose URL is not allowlisted is a Download button that 404s, which is the
 * exact bug #4146 was filed for.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ScriptDownload, ScriptDownloadList } from '@/components/setup/ScriptDownload';

afterEach(() => vi.restoreAllMocks());

describe('ScriptDownloadList', () => {
  it('lists exactly the four files the backend serves', () => {
    render(<ScriptDownloadList />);

    expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(4);
  });

  it('offers bg-cognito-auth.sh', () => {
    render(<ScriptDownloadList />);

    expect(screen.getByText('bg-cognito-auth.sh')).toBeInTheDocument();
  });

  it('offers the adp CLI and its installer (Issue #4852)', () => {
    render(<ScriptDownloadList />);

    expect(screen.getByText('adp')).toBeInTheDocument();
    expect(screen.getByText('install.sh')).toBeInTheDocument();
  });

  it('explains that install.sh is the normal way to get adp', () => {
    // Downloading `adp` alone gets you a CLI that cannot find its siblings, so
    // the card has to point at the installer rather than imply it is standalone.
    render(<ScriptDownloadList />);
    const text = document.body.textContent ?? '';

    expect(text).toContain('install.sh');
    expect(text).toContain('~/.adp/bin');
  });

  it('filters to the requested files', () => {
    // Each setup tab is self-contained: the Claude Code tab must not advertise
    // the Codex-only proxy.
    render(<ScriptDownloadList files={['bg-cognito-auth.sh']} />);

    expect(screen.getByText('bg-cognito-auth.sh')).toBeInTheDocument();
    expect(screen.queryByText('bg-gateway-proxy.py')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(1);
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
    [2, '/api/cli/adp'],
    [3, '/api/cli/install.sh'],
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
