import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { getGitlabHandoff } from '@/services/gitlabSso';

export default function GitlabLaunch() {
  const handoff = useRef<Promise<string> | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    handoff.current ??= getGitlabHandoff();
    void handoff.current.then(url => {
      if (active) window.location.assign(url);
    }).catch(reason => {
      if (active) setError(reason instanceof Error ? reason.message : 'GitLab sign-in is temporarily unavailable.');
    });
    return () => { active = false; };
  }, []);
  return <main className="mx-auto max-w-xl p-8 space-y-4">
    <h1 className="text-2xl font-semibold">Open GitLab</h1>
    {error ? <p role="alert">{error}</p> : <p role="status">Signing you into GitLab…</p>}
    <Link className="underline" to="/products">Go to your Products</Link>
  </main>;
}
