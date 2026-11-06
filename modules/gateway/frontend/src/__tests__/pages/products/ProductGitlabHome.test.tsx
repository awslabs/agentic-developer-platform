import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import ProductGitlabHome from '@/pages/products/ProductGitlabHome';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
vi.mock('@/services/auth', () => ({ getAccessToken: vi.fn() }));

afterEach(() => { vi.clearAllMocks(); vi.unstubAllGlobals(); });

describe('Product GitLab home', () => {
  it('opens the selected Product through the authenticated launch endpoint', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ status: 'ready', message: 'Product repositories' });
    vi.mocked(apiClient.post).mockResolvedValue({ redirect_url: 'https://gitlab.example.test/users/auth/jwt/callback?jwt=fixture' });
    const assign = vi.fn();
    vi.stubGlobal('location', { assign });
    render(<ProductGitlabHome productId="product-a" />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open GitLab home' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Open GitLab home' }));
    await waitFor(() => expect(assign).toHaveBeenCalledOnce());
    expect(apiClient.post).toHaveBeenCalledWith('/v1/products/product-a/gitlab/launch');
    expect(assign).toHaveBeenCalledWith('https://gitlab.example.test/users/auth/jwt/callback?jwt=fixture');
  });

  it('shows setup status and does not launch before access is ready', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ status: 'preparing', message: 'Preparing your home' });
    render(<ProductGitlabHome productId="product-a" />);
    expect(await screen.findByText('Preparing your home')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open GitLab home' })).toBeDisabled();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('reports a failed launch without falling back to a second login', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ status: 'ready', message: 'Ready' });
    vi.mocked(apiClient.post).mockRejectedValue(new Error('Access changed. Please try again.'));
    render(<ProductGitlabHome productId="product-a" />);
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled());
    fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByRole('alert')).toHaveTextContent('Access changed');
  });

  it('does not navigate after the Product view has been left', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ status: 'ready', message: 'Ready' });
    let finish: (value: unknown) => void = () => {};
    vi.mocked(apiClient.post).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const assign = vi.fn();
    vi.stubGlobal('location', { assign });
    const { unmount } = render(<ProductGitlabHome productId="product-a" />);
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled());
    fireEvent.click(screen.getByRole('button'));
    unmount();
    await act(async () => finish({ redirect_url: 'https://gitlab.example.test/users/auth/jwt/callback?jwt=fixture' }));
    expect(assign).not.toHaveBeenCalled();
  });
});
