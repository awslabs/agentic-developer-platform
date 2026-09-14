/**
 * Tests for NextHome — Issue #5079.
 *
 * The preview landing page has one job in this story: tell the truth about what
 * the preview is and route people to the current UI for everything else. The
 * assertions below are about that honesty, not about styling.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import NextHome from '@/pages/next/NextHome';

vi.mock('@/components/next/CurrentUiLinks', () => ({
  CurrentUiLinks: () => <div data-testid="next-current-ui-links" />,
}));

function renderHome() {
  return render(
    <MemoryRouter>
      <NextHome />
    </MemoryRouter>,
  );
}

describe('NextHome — Issue #5079', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the current-UI links for unmigrated capabilities', () => {
    renderHome();
    expect(screen.getByTestId('next-current-ui-links')).toBeInTheDocument();
  });

  it('states that the current UI remains the default and still works', () => {
    renderHome();
    expect(screen.getByText(/current UI remains the default/i)).toBeInTheDocument();
  });

  it('points at the return control by name', () => {
    // The control itself lives in NextLayout; the page tells the user it exists.
    renderHome();
    expect(screen.getByText('Back to current UI')).toBeInTheDocument();
  });

  it('renders no journey navigation — that is NUI-02, not this story', () => {
    // Guards against someone adding placeholder Use ADP / Administration nav here,
    // which the coexistence contract forbids until the pages behind it exist.
    renderHome();
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });
});
