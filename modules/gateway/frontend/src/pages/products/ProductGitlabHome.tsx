import { useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import { validateGitlabDestination } from '@/services/gitlabSso';
import { Button } from '@/components/ui';

interface Home {
  status: 'disabled' | 'preparing' | 'ready' | 'error' | 'unavailable' | 'archived';
  message: string;
}

export default function ProductGitlabHome({ productId }: { productId: string }) {
  const [home, setHome] = useState<Home | null>(null);
  const [error, setError] = useState('');
  const [opening, setOpening] = useState(false);
  const active = useRef(true);

  useEffect(() => {
    let cancelled = false;
    active.current = true;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const value = await apiClient.get<Home>(`/v1/products/${encodeURIComponent(productId)}/gitlab`);
        if (cancelled) return;
        setHome(value);
        setError('');
        if (value.status === 'preparing' || value.status === 'error') timer = setTimeout(() => void refresh(), 10000);
      } catch {
        if (!cancelled) setError('GitLab home is temporarily unavailable.');
      }
    };
    setHome(null);
    void refresh();
    return () => { cancelled = true; active.current = false; clearTimeout(timer); };
  }, [productId]);

  const open = async () => {
    setOpening(true);
    setError('');
    try {
      const handoff = await apiClient.post<{ redirect_url?: string; message?: string }>(`/v1/products/${encodeURIComponent(productId)}/gitlab/launch`);
      if (!active.current) return;
      if (!handoff.redirect_url) throw new Error(handoff.message || 'GitLab access is being prepared. Please try again shortly.');
      window.location.assign(validateGitlabDestination(handoff.redirect_url));
    } catch (reason) {
      if (!active.current) return;
      setError(reason instanceof Error ? reason.message : 'GitLab sign-in is temporarily unavailable.');
      setOpening(false);
    }
  };

  if (home?.status === 'disabled') return null;
  return <section className="blueprint-card product-card space-y-3" aria-label="Product GitLab home">
    <h2>GitLab home</h2>
    <p className="product-muted">{home?.message ?? 'Checking your Product’s GitLab home…'}</p>
    {error && <p role="alert">{error}</p>}
    <Button disabled={opening || home?.status !== 'ready'} onClick={() => void open()}>
      {opening ? 'Opening GitLab…' : 'Open GitLab home'}
    </Button>
  </section>;
}
