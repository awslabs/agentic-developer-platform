import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { listProducts, type ProductSummary } from '@/services/products';
import { deriveIntakePlan, getIntakeSession, sendIntakeTurn, startFlowIntake, type DerivedIntakePlan, type IntakeSession, type IntakeStartRequest } from '@/services/orchestration';
import { Alert, Button, Input, Select, Textarea } from '@/components/ui';

interface SavedIntake {
  input: IntakeStartRequest;
  sessionId?: string;
}

function readSavedIntake(key: string | null): SavedIntake | null {
  if (!key) return null;
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) ?? 'null') as SavedIntake | null;
    if (!saved || !saved.input || !['product_id', 'repository', 'issue', 'message', 'retry_token']
      .every((field) => typeof saved.input[field as keyof IntakeStartRequest] === 'string') ||
      (saved.sessionId !== undefined && typeof saved.sessionId !== 'string')) return null;
    return saved;
  } catch {
    return null;
  }
}

export default function StartFlow() {
  const { user } = useAuth();
  const storageKey = user?.id && user.orgId ? `flow-intake:${user.orgId}:${user.id}` : null;
  const [searchParams, setSearchParams] = useSearchParams();
  const urlProduct = searchParams.get('product') ?? '';
  const urlProject = searchParams.get('project') ?? '';
  const [products, setProducts] = useState<ProductSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [productId, setProductId] = useState('');
  const [projectId, setProjectId] = useState('');
  const [repository, setRepository] = useState('');
  const [issue, setIssue] = useState('');
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [scopeKey, setScopeKey] = useState<string | null>(null);
  const [pending, setPending] = useState<SavedIntake | null>(null);
  const sessionId = searchParams.get('session') ?? (scopeKey === storageKey ? pending?.sessionId ?? null : null);
  const activeSessionId = useRef(sessionId);
  activeSessionId.current = sessionId;
  const activeScope = useRef(storageKey);
  activeScope.current = storageKey;
  const [session, setSession] = useState<IntakeSession | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [reply, setReply] = useState('');
  const [pendingReply, setPendingReply] = useState<{ message: string; retry_token: string } | null>(null);
  const [plan, setPlan] = useState<DerivedIntakePlan | null>(null);
  const currentSession = scopeKey === storageKey && session?.session_id === sessionId ? session : null;
  const viewedProductId = currentSession?.product_id ?? (pending?.sessionId === sessionId ? pending.input.product_id : null);

  useEffect(() => {
    setSubmitting(false);
    setError(null);
    setSessionError(null);
    setReply('');
    const saved = readSavedIntake(storageKey);
    setPending(saved);
    setProductId(saved?.input.product_id ?? urlProduct);
    setProjectId(saved?.input.project_id ?? urlProject);
    setRepository(saved?.input.repository ?? '');
    setIssue(saved?.input.issue ?? '');
    setMessage(saved?.input.message ?? '');
    setScopeKey(storageKey);
  }, [storageKey, urlProduct, urlProject]);

  useEffect(() => {
    setPendingReply(null);
    if (!storageKey || !sessionId) return;
    try {
      const saved = JSON.parse(sessionStorage.getItem(`${storageKey}:${sessionId}:reply`) ?? 'null');
      if (saved && typeof saved.message === 'string' && typeof saved.retry_token === 'string') setPendingReply(saved);
    } catch { setPendingReply(null); }
  }, [storageKey, sessionId]);

  const refreshSession = useCallback(async (id: string) => {
    setSessionLoading(true);
    setSessionError(null);
    setPlan(null);
    try {
      const result = await getIntakeSession(id);
      if (activeSessionId.current === id && activeScope.current === storageKey && result.session_id === id) setSession(result);
    } catch {
      if (activeSessionId.current === id && activeScope.current === storageKey) {
        setSession(null);
        setSessionError('Planning session could not be read. Check access and retry.');
      }
    } finally {
      if (activeSessionId.current === id && activeScope.current === storageKey) setSessionLoading(false);
    }
  }, [storageKey]);

  useEffect(() => {
    setSession(null);
    setPlan(null);
    if (sessionId && storageKey) void refreshSession(sessionId);
  }, [sessionId, storageKey, refreshSession]);

  useEffect(() => {
    let active = true;
    setProducts([]);
    setLoading(true);
    if (!storageKey) return () => { active = false; };
    async function load() {
      try {
        const items: ProductSummary[] = [];
        let cursor: string | null = null;
        do {
          const page = await listProducts(cursor ?? undefined);
          items.push(...page.items);
          cursor = page.next_cursor;
        } while (cursor);
        if (active) setProducts(items);
      } catch {
        if (active) setError('Products could not be loaded. Try again later.');
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; };
  }, [storageKey]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!storageKey || submitting || (!pending && (!productId || !repository || !issue || !message))) return;
    const saved = pending ?? { input: { product_id: productId, ...(projectId ? { project_id: projectId } : {}), repository, issue, message, retry_token: crypto.randomUUID() } };
    sessionStorage.setItem(storageKey, JSON.stringify(saved));
    setPending(saved);
    setSubmitting(true);
    setError(null);
    try {
      const accepted = await startFlowIntake(saved.input);
      const confirmed = { ...saved, sessionId: accepted.session_id };
      sessionStorage.setItem(storageKey, JSON.stringify(confirmed));
      if (activeScope.current !== storageKey) return;
      setPending(confirmed);
      setSearchParams({ session: accepted.session_id }, { replace: true });
    } catch (failure) {
      if (activeScope.current !== storageKey) return;
      if ([400, 403, 404, 422].includes((failure as { status?: number })?.status ?? 0)) {
        sessionStorage.removeItem(storageKey);
        setPending(null);
      }
      setError([400, 403, 404, 422].includes((failure as { status?: number })?.status ?? 0)
        ? 'Planning was refused. Check your Product and epic access before submitting a new request.'
        : 'Planning could not be confirmed. Retry the same request if its response was lost; a new request may create a second session.');
    } finally {
      if (activeScope.current === storageKey) setSubmitting(false);
    }
  }

  async function sendReply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!sessionId || !storageKey || !currentSession || sessionLoading || currentSession.working || !currentSession.awaiting_answer || (!pendingReply && !reply.trim()) || submitting) return;
    const turn = pendingReply ?? { message: reply.trim(), retry_token: crypto.randomUUID() };
    sessionStorage.setItem(`${storageKey}:${sessionId}:reply`, JSON.stringify(turn));
    setPendingReply(turn);
    setSubmitting(true);
    setSessionError(null);
    setPlan(null);
    try {
      await sendIntakeTurn(sessionId, turn);
      sessionStorage.removeItem(`${storageKey}:${sessionId}:reply`);
      if (activeScope.current !== storageKey || activeSessionId.current !== sessionId) return;
      setPendingReply(null);
      setReply('');
      await refreshSession(sessionId);
    } catch {
      if (activeScope.current === storageKey && activeSessionId.current === sessionId) setSessionError('Reply could not be confirmed. Refresh the session before retrying the same reply.');
    } finally {
      if (activeScope.current === storageKey && activeSessionId.current === sessionId) setSubmitting(false);
    }
  }

  async function reviewPlan() {
    if (!sessionId || !currentSession || sessionLoading || currentSession.working || currentSession.open_questions.length || !currentSession.draft_available) return;
    setSubmitting(true);
    setSessionError(null);
    try {
      const result = await deriveIntakePlan(sessionId);
      if (activeScope.current === storageKey && activeSessionId.current === sessionId) setPlan(result);
    } catch {
      if (activeScope.current === storageKey && activeSessionId.current === sessionId) setSessionError('Plan could not be derived. Refresh the session and resolve its prerequisites.');
    } finally {
      if (activeScope.current === storageKey && activeSessionId.current === sessionId) setSubmitting(false);
    }
  }

  if (!storageKey || scopeKey !== storageKey) return <main className="p-6" role="status">Loading your planning context…</main>;

  return (
    <main className="max-w-xl space-y-4 p-6">
      <h1 className="text-xl font-semibold">Start a delivery flow</h1>
      <p>Select the Product this flow serves and the planning epic. Each story gets its own repository during planning; the epic repository is not the story target. Starting planning does not approve execution.</p>
      {error && <Alert variant="error" title="Planning unavailable">{error}</Alert>}
      {sessionId ? (
        <section className="space-y-3" aria-label="Planning session">
          {(currentSession || pending?.sessionId === sessionId) && <p role="status">Planning request accepted for Product {viewedProductId ? products.find((item) => item.id === viewedProductId)?.name ?? viewedProductId : 'unavailable'}. Session {sessionId} is processing; no plan has been approved or dispatched.</p>}
          <Link to={`/flows/new?session=${encodeURIComponent(sessionId)}`} className="underline">Resume this planning session</Link>
          <Button type="button" variant="secondary" disabled={submitting} onClick={() => {
            if (storageKey) {
              sessionStorage.removeItem(storageKey);
              sessionStorage.removeItem(`${storageKey}:${sessionId}:reply`);
            }
            setPendingReply(null);
            setPending(null);
            setProductId('');
            setRepository('');
            setIssue('');
            setMessage('');
            setSearchParams({}, { replace: true });
          }}>Start another planning request</Button>
          {sessionError && <Alert variant="error" title="Session unavailable">{sessionError}</Alert>}
          {sessionLoading && <p role="status">Loading session status…</p>}
          <Button type="button" variant="secondary" disabled={sessionLoading || submitting} onClick={() => void refreshSession(sessionId)}>Refresh session status</Button>
          {currentSession && <>
            <p>{currentSession.working ? 'The planning agent is responding.' : currentSession.awaiting_answer ? 'Your answer is needed.' : 'No turn is in progress.'}</p>
            {currentSession.last_response && <p className="whitespace-pre-wrap">Latest response: {currentSession.last_response}</p>}
            {currentSession.issue_ref && <p>Planning issue: {currentSession.repository}#{currentSession.issue_ref.replace(/^#/, '')}</p>}
            {currentSession.open_questions.length > 0 && <ul aria-label="Planning questions">{currentSession.open_questions.map((question) => <li key={question}>{question}</li>)}</ul>}
            {currentSession.awaiting_answer && !currentSession.working && <form onSubmit={(event) => void sendReply(event)} className="space-y-2">
              <Textarea name="planning-answer" label="Answer the planning questions" value={pendingReply?.message ?? reply} disabled={Boolean(pendingReply)} onChange={(event) => setReply(event.target.value)} />
              <Button type="submit" disabled={submitting || (!pendingReply && !reply.trim())}>{pendingReply ? 'Retry same answer' : 'Send answer'}</Button>
            </form>}
            {!currentSession.working && currentSession.open_questions.length === 0 && currentSession.draft_available && <Button type="button" variant="secondary" disabled={submitting} onClick={() => void reviewPlan()}>Review derived plan</Button>}
            {plan && <section aria-label="Derived plan" className="space-y-2">
              <p>This is a read-only proposal; no plan is approved or dispatched. Resolve the listed prerequisites, save the proposal as plan.json, then use `adp flow create --file plan.json` to register an inert draft for human review.</p>
              {plan.prerequisites.length > 0 && <ul aria-label="Plan prerequisites">{plan.prerequisites.map((item) => <li key={item}>{item}</li>)}</ul>}
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(plan.proposal, null, 2)}</pre>
            </section>}
          </>}
        </section>
      ) : loading ? <p role="status">Loading Products…</p> : products.length === 0 ? (
        <p>No accessible Products are available. <Link to="/products" className="underline">View Products</Link></p>
      ) : (
        <form className="space-y-3" onSubmit={(event) => void submit(event)}>
          <Select id="flow-product" label="Product" value={productId} required disabled={Boolean(pending)} onChange={(event) => { setProductId(event.target.value); setProjectId(''); }}
            options={[{ value: '', label: 'Select a Product' }, ...products.map((item) => ({ value: item.id, label: item.name }))]} />
          {productId && <Select id="flow-project" label="Project" value={projectId} disabled={Boolean(pending)} onChange={event => setProjectId(event.target.value)}
            options={[{ value: products.find(item => item.id === productId)?.base_project_id === projectId ? projectId : '', label: 'Base Project (default)' }, ...(products.find(item => item.id === productId)?.projects ?? []).filter(item => !item.is_base).map(item => ({ value: item.id, label: item.name }))]} />}
          {productId && <p className="text-sm text-gray-500">Planning and agent runs share this Project’s budget.</p>}
          <Input name="epic-repository" label="Planning epic repository (owner/name)" value={repository} required disabled={Boolean(pending)} onChange={(event) => setRepository(event.target.value)} />
          <Input name="epic-issue" label="Planning epic issue number" value={issue} required disabled={Boolean(pending)} onChange={(event) => setIssue(event.target.value)} />
          <Textarea name="flow-goal" label="What should this flow achieve?" value={message} required disabled={Boolean(pending)} onChange={(event) => setMessage(event.target.value)} />
          <Button type="submit" disabled={submitting || !productId || !repository || !issue || !message}>{submitting ? 'Starting…' : pending ? 'Retry same request' : 'Start planning'}</Button>
        </form>
      )}
      <Link to="/flows" className="underline">Back to delivery flows</Link>
    </main>
  );
}
