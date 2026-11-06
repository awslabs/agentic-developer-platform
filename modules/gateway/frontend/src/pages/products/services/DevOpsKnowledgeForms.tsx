import { useState } from 'react';
import type { AssetReadback, KnowledgeObservation, KnowledgePreparation } from '@/services/devopsKnowledgeEvidence';

const inputClass = 'block w-full rounded border p-2';
export function KnowledgePreparationForm({ disabled, onSubmit }: {
  disabled: boolean; onSubmit: (value: Omit<KnowledgePreparation, 'source_key' | 'flow_id' | 'challenge_id'>) => void;
}) {
  const [value, setValue] = useState({ prompt: '', expected_behavior: '', source_excerpt: '', reason: '' });
  const fields = [
    ['prompt', 'Prompt you will send to DevOps', 8000], ['expected_behavior', 'Expected source-dependent behavior', 1000],
    ['source_excerpt', 'Exact source excerpt supporting that expectation', 4000], ['reason', 'Why this scenario tests the selected knowledge', 1000],
  ] as const;
  return <form onSubmit={event => { event.preventDefault(); if (!disabled) onSubmit(value); }} className="space-y-3">
    <p>Declare the scenario before starting a new conversation. Keep the expected answer and source excerpt out of your prompt.</p>
    <fieldset disabled={disabled} className="space-y-3">
      {fields.map(([key, label, limit]) => <label className="block" key={key}>{label}<textarea className={inputClass} required minLength={8} maxLength={limit} value={value[key]} onChange={event => setValue({ ...value, [key]: event.target.value })} /></label>)}
      <button type="submit">Prepare my observation</button>
    </fieldset>
  </form>;
}

function ReadbackFields({ name, value, onChange }: { name: string; value: ReadbackInput; onChange: (value: ReadbackInput) => void }) {
  return <fieldset className="space-y-2"><legend>{name} native Asset readback</legend>
    <label className="block">{name} Asset ID<input className={inputClass} required value={value.asset_id} onChange={event => onChange({ ...value, asset_id: event.target.value })} /></label>
    <label className="block">{name} Asset version<input className={inputClass} required type="number" min={1} step={1} value={value.version} onChange={event => onChange({ ...value, version: event.target.value })} /></label>
    <label className="block">{name} readback time (include time zone)<input className={inputClass} required placeholder="YYYY-MM-DDTHH:mm:ssZ" value={value.observed_at} onChange={event => onChange({ ...value, observed_at: event.target.value })} /></label>
    <label className="block">{name} full Asset content<textarea className={inputClass} required maxLength={1048576} value={value.content} onChange={event => onChange({ ...value, content: event.target.value })} /></label>
    <label className="block">{name} Asset metadata (JSON)<textarea className={inputClass} required value={value.metadata} onChange={event => onChange({ ...value, metadata: event.target.value })} /></label>
  </fieldset>;
}
interface ReadbackInput { asset_id: string; version: string; observed_at: string; content: string; metadata: string }
const emptyReadback = (): ReadbackInput => ({ asset_id: '', version: '', observed_at: '', content: '', metadata: '' });
function readback(value: ReadbackInput): AssetReadback {
  const metadata: unknown = JSON.parse(value.metadata);
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error('Asset metadata must be a JSON object.');
  if (!Number.isInteger(Number(value.version)) || Number(value.version) < 1) throw new Error('Enter the actual positive Asset version.');
  if (!/(Z|[+-]\d\d:\d\d)$/.test(value.observed_at) || !Number.isFinite(Date.parse(value.observed_at))) throw new Error('Enter the actual readback time with its time zone.');
  return { ...value, version: Number(value.version), metadata: metadata as Record<string, unknown> };
}
export function KnowledgeObservationForm({ disabled, skill, onSubmit }: {
  disabled: boolean; skill: boolean; onSubmit: (value: Omit<KnowledgeObservation, 'preparation_hash'>) => void;
}) {
  const [before, setBefore] = useState(emptyReadback);
  const [after, setAfter] = useState(emptyReadback);
  const [session, setSession] = useState('');
  const [agent, setAgent] = useState('');
  const [started, setStarted] = useState('');
  const [turns, setTurns] = useState<KnowledgeObservation['transcript']>([{ role: 'user', text: '' }, { role: 'assistant', text: '' }]);
  const [quotes, setQuotes] = useState({ response_quote: '', source_citation_quote: '', skill_invocation_quote: '', explanation: '' });
  const [error, setError] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  return <form className="space-y-3" onSubmit={event => {
    event.preventDefault(); if (disabled || !confirmed) return;
    try {
      if (!/(Z|[+-]\d\d:\d\d)$/.test(started) || !Number.isFinite(Date.parse(started))) throw new Error('Enter the actual conversation start time with its time zone.');
      const value = { native_session_id: session, native_agent_type: agent, session_started_at: started,
        before: readback(before), after: readback(after), transcript: turns,
        response_quote: quotes.response_quote, source_citation_quote: quotes.source_citation_quote,
        explanation: quotes.explanation, ...(skill ? { skill_invocation_quote: quotes.skill_invocation_quote } : {}) };
      setError(''); onSubmit(value);
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Check the entered readbacks.'); }
  }}>
    <p>Enter what you actually observed in your native DevOps session. Include the complete Asset readback before and after the conversation, and its actual turns.</p>
    {error && <p role="alert">{error}</p>}
    <fieldset disabled={disabled} className="space-y-3">
      <label className="block">Native conversation ID<input className={inputClass} required value={session} onChange={event => setSession(event.target.value)} /></label>
      <label className="block">Agent used<select className={inputClass} required value={agent} onChange={event => setAgent(event.target.value)}><option value="">Choose the agent you used</option>{['CHAT', 'INCIDENT_TRIAGE', 'INCIDENT_RCA', 'INCIDENT_MITIGATION', 'INCIDENT_UI', 'PREVENTION', 'RELEASE_READINESS_REVIEW', 'RELEASE_TESTING'].map(name => <option key={name} value={name}>{name.toLowerCase().replace(/_/g, ' ')}</option>)}</select></label>
      <label className="block">Conversation start time (include time zone)<input className={inputClass} required placeholder="YYYY-MM-DDTHH:mm:ssZ" value={started} onChange={event => setStarted(event.target.value)} /></label>
      <ReadbackFields name="Before" value={before} onChange={setBefore} />
      <ReadbackFields name="After" value={after} onChange={setAfter} />
      <fieldset className="space-y-2"><legend>Actual conversation transcript</legend>
        {turns.map((turn, index) => <div key={index}>
          <label>Turn {index + 1} speaker<select value={turn.role} onChange={event => setTurns(turns.map((item, offset) => offset === index ? { ...item, role: event.target.value as 'user' | 'assistant' } : item))}><option value="user">Me</option><option value="assistant">DevOps agent</option></select></label>
          <label className="block">Turn {index + 1} text<textarea className={inputClass} required maxLength={16000} value={turn.text} onChange={event => setTurns(turns.map((item, offset) => offset === index ? { ...item, text: event.target.value } : item))} /></label>
        </div>)}
        <button type="button" disabled={turns.length >= 32} onClick={() => setTurns([...turns, { role: 'user', text: '' }, { role: 'assistant', text: '' }])}>Add conversation exchange</button>
      </fieldset>
      {([['response_quote', 'Exact response showing the expected behavior', 8], ['source_citation_quote', 'Exact source citation from the response', 1], ...(skill ? [['skill_invocation_quote', 'Exact skill invocation from the response', 1] as const] : []), ['explanation', 'How the source affected this response', 30]] as const).map(([key, label, min]) => <label key={key} className="block">{label}<textarea className={inputClass} required minLength={min} maxLength={4000} value={quotes[key]} onChange={event => setQuotes({ ...quotes, [key]: event.target.value })} /></label>)}
      <label className="block"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> I personally observed this session and these readbacks.</label>
      <button type="submit" disabled={!confirmed}>Record my knowledge observation</button>
    </fieldset>
  </form>;
}
