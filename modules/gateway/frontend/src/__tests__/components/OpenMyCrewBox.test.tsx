/**
 * Chat action for the owner's EC2 Crew box (#8140, #8223 UX): the click opens the
 * interstitial tab synchronously and does nothing else — the flow runs in that tab.
 */
import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import OpenMyCrewBox from '@/components/chat/OpenMyCrewBox';
import { apiClient } from '@/services/api';
import { DEV_BOX_OPEN_PATH, consumeDevBoxOpenIntent } from '@/services/devBoxOpen';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), request: vi.fn(), post: vi.fn() } }));

function renderAction() {
  return render(<MemoryRouter><OpenMyCrewBox /></MemoryRouter>);
}

beforeEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

it('opens the interstitial tab inside the click, with a one-time intent, and calls no API itself', () => {
  const tab = { closed: false, focus: vi.fn() } as unknown as Window;
  const open = vi.spyOn(window, 'open').mockReturnValue(tab);
  renderAction();
  fireEvent.click(screen.getByRole('button', { name: 'Open my EC2 Crew' }));
  expect(open).toHaveBeenCalledOnce();
  const [href, target] = open.mock.calls[0];
  expect(String(href).startsWith(`${DEV_BOX_OPEN_PATH}?intent=`)).toBe(true);
  expect(target).toBe('_blank');
  expect(consumeDevBoxOpenIntent(new URL(String(href), 'https://adp.example.test').searchParams.get('intent'))).toBe(true);
  expect(apiClient.get).not.toHaveBeenCalled();
  expect(apiClient.request).not.toHaveBeenCalled();
  expect(apiClient.post).not.toHaveBeenCalled();
  expect(screen.queryByRole('link', { name: 'Open in new tab' })).not.toBeInTheDocument();
});

it('a second click while the tab is open focuses it instead of opening another', () => {
  const tab = { closed: false, focus: vi.fn() } as unknown as Window;
  const open = vi.spyOn(window, 'open').mockReturnValue(tab);
  renderAction();
  fireEvent.click(screen.getByRole('button', { name: 'Open my EC2 Crew' }));
  fireEvent.click(screen.getByRole('button', { name: 'Open my EC2 Crew' }));
  expect(open).toHaveBeenCalledOnce();
  expect(tab.focus).toHaveBeenCalledOnce();
});

it('offers the interstitial as a link when the browser refuses even a click-time open', () => {
  vi.spyOn(window, 'open').mockReturnValue(null);
  renderAction();
  fireEvent.click(screen.getByRole('button', { name: 'Open my EC2 Crew' }));
  const link = screen.getByRole('link', { name: 'Open in new tab' });
  expect(link.getAttribute('href')).toMatch(new RegExp(`^${DEV_BOX_OPEN_PATH}\\?intent=[0-9a-f-]{36}$`));
  expect(link).toHaveAttribute('target', '_blank');
  expect(link).not.toHaveAttribute('rel');
  expect(screen.getByRole('alert')).toHaveTextContent('The browser blocked the new tab');
});
