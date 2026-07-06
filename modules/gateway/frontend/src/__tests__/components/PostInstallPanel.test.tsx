/**
 * PostInstallPanel component tests.
 *
 * Issue #2984: Post-install what's-next panel.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { PostInstallPanel } from '@/components/PostInstallPanel';

describe('PostInstallPanel', () => {
  it('renders the success heading without repo count when not provided', () => {
    render(<PostInstallPanel />);

    expect(screen.getByText('Org connected')).toBeInTheDocument();
  });

  it('renders with repo count when provided', () => {
    render(<PostInstallPanel repoCount={5} />);

    expect(screen.getByText(/Org connected — 5 repos/)).toBeInTheDocument();
  });

  it('uses singular "repo" for count of 1', () => {
    render(<PostInstallPanel repoCount={1} />);

    expect(screen.getByText(/Org connected — 1 repo$/)).toBeInTheDocument();
  });

  it('shows step 1: share sign-in URL', () => {
    render(<PostInstallPanel />);

    expect(screen.getByText('Share the sign-in URL with your team')).toBeInTheDocument();
  });

  it('shows step 2: mention agent', () => {
    render(<PostInstallPanel />);

    expect(screen.getByText(/Mention/)).toBeInTheDocument();
    expect(screen.getByText('@agent-developer')).toBeInTheDocument();
  });

  it('displays the provided sign-in URL', () => {
    render(<PostInstallPanel signInUrl="https://my-app.example.com" />);

    expect(screen.getByText('https://my-app.example.com')).toBeInTheDocument();
  });

  it('renders the Copy button for the sign-in URL', () => {
    render(<PostInstallPanel signInUrl="https://test.example.com" />);

    expect(screen.getByTitle('Copy URL')).toBeInTheDocument();
  });
});
