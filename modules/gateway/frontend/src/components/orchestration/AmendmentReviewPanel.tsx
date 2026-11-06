import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Permission } from '@/types';
import type { AmendmentEffectiveLimits, AmendmentImpact, AmendmentReceiptDisposition, FlowGraph, GraphNode, NodeEngineState, NodeKind } from '@/types/orchestration';
import { usePermissions } from '@/hooks/usePermissions';
import {
  acceptAmendment,
  editAmendment,
  getAmendment,
  listAmendments,
  previewAmendment,
  proposeAmendment,
  rejectAmendment,
} from '@/services/orchestration';
import { Alert, Button } from '@/components/ui';
import { groupIntoEpics } from '@/utils/flowLayout';
import { WaveCard } from './WaveCard';

const limitKeys = ['max_spend_usd', 'max_attempts_per_node', 'max_concurrent_actions', 'max_wall_clock_seconds', 'expires_at'] as const;

const placements = ['insert_before', 'insert_after', 'parallel_branch', 'existing_wave', 'new_wave'] as const;

function Topology({ label, nodes, edges, impact }: { label: string; nodes: Array<Record<string, unknown>>; edges: Array<{ from_address: string; to_address: string }>; impact: AmendmentImpact }) {
  const changes = new Map(impact.nodes.map((node) => [node.address, node.change]));
  const graphNodes: GraphNode[] = nodes.map((node) => {
    const address = String(node.address);
    const [slug = 'invalid', epicRef = 'invalid', waveRef = 'invalid', nodeRef = address] = address.split('/');
    const current = impact.nodes.find((item) => item.address === address);
    return {
      id: address, epic_ref: epicRef, wave_ref: waveRef, node_ref: nodeRef, kind: String(node.kind) as NodeKind,
      title: `${String(node.title)} · ${changes.get(address) ?? 'unchanged'}`, state: (current?.state ?? 'pending') as NodeEngineState,
      stalled: false, issue_ref: node.issue_ref == null ? null : String(node.issue_ref), attempts: current?.attempts ?? 0,
      file_groups: Array.isArray(node.file_groups) ? node.file_groups as GraphNode['file_groups'] : [],
      cost: { status: 'unknown', amount_usd: null, reason: 'preview', scope: slug },
    };
  });
  const ids = new Set(graphNodes.map((node) => node.id));
  const graph: FlowGraph = {
    flow_id: `amendment-${label}`, slug: String(nodes[0]?.address ?? 'preview').split('/')[0], title: `${label} graph`, intent_ref: null, state: 'preview', nodes: graphNodes,
    edges: edges.filter((edge) => ids.has(edge.from_address) && ids.has(edge.to_address)).map((edge) => ({ from_node_id: edge.from_address, to_node_id: edge.to_address })),
    cost: { status: 'unknown', amount_usd: null, reason: 'preview', partial: true, scope: 'amendment' },
  };
  return <section data-testid={`amendment-graph-${label.toLowerCase()}`} className="space-y-3 rounded border border-gray-200 p-3 dark:border-gray-700">
    <h4 className="font-semibold">{label} graph</h4>
    {groupIntoEpics(graph).flatMap((epic) => epic.waves.map((wave) => <WaveCard key={`${epic.epicRef}/${wave.waveRef}`} epicRef={epic.epicRef} wave={wave} graph={graph} expanded interactive={false} onToggle={() => undefined} />))}
    <ul aria-label={`${label} story targets`}>{nodes.filter((node) => node.kind === 'story').map((node) => <li key={String(node.address)}>
      {String(node.title)}: {node.repository_identity === undefined ? 'repository not provided' : repositoryLabel(node)}{storyReference(node) && ` · ${storyReference(node)}`}
    </li>)}</ul>
    <ul aria-label={`${label} graph edges`}>{edges.map((edge) => <li key={`${edge.from_address}-${edge.to_address}`}>{edge.from_address} → {edge.to_address}</li>)}</ul>
  </section>;
}

function receiptValue(kind: AmendmentReceiptDisposition['kind'], value: unknown): string {
  if (value == null) return 'none';
  if (typeof value !== 'object') return String(value);
  const fields = value as Record<string, unknown>;
  if (kind === 'budget') {
    return `flow $${fields.max_spend_usd}, run $${fields.max_run_spend_usd}, chain $${fields.max_chain_spend_usd}`;
  }
  if (kind === 'window') {
    return `expiry ${fields.expires_at}, wall-clock ${fields.max_wall_clock_seconds} seconds`;
  }
  return JSON.stringify(value);
}

function DroppedReceipts({ receipts, preview = false }: { receipts: AmendmentReceiptDisposition[]; preview?: boolean }) {
  if (receipts.length === 0) return null;
  return <Alert variant="warning" title={preview ? "Accepted increases would be dropped on accept" : "Accepted increases dropped — re-apply if needed"}>
    <ul>{receipts.map((item) => <li key={`${item.kind}-${item.decision_id}`}>
      {item.kind} (decision {item.decision_id}): {receiptValue(item.kind, item.before)} → {receiptValue(item.kind, item.after)} — {item.reason}
    </li>)}</ul>
  </Alert>;
}

function EffectiveLimits({ before, after, committed = false }: {
  before?: AmendmentEffectiveLimits | null; after?: AmendmentEffectiveLimits | null; committed?: boolean;
}) {
  if (!before && !after) return null;
  return <section data-testid={committed ? 'amendment-committed-limits' : 'amendment-effective-limits'}>
    <h4 className="font-semibold">{committed ? 'Committed effective limits (before → after)' : 'Effective limits (before → after accept)'}</h4>
    <ul>{limitKeys.map((key) => {
      const previous = before?.[key] ?? 'none';
      const next = after?.[key] ?? 'none';
      return <li key={key}>{key}: {String(previous)} → {String(next)}{previous !== next ? ' (changed)' : ''}</li>;
    })}</ul>
  </section>;
}

function repositoryLabel(value: Record<string, unknown> | null | undefined): string {
  if (!value || typeof value.repository_identity !== 'string' || !value.repository_identity) return 'unbound';
  const repositoryId = typeof value.provider_repository_id === 'number' ? value.provider_repository_id : 'unverified';
  const connection = value.repository_connection;
  const installation = connection && typeof connection === 'object' && 'installation_id' in connection
    ? connection.installation_id : 'unverified';
  return `${value.repository_identity} (provider ID ${repositoryId}, installation ${installation})`;
}

function storyReference(value: Record<string, unknown> | null | undefined): string {
  if (!value) return '';
  const issue = typeof value.issue_ref === 'string' && value.issue_ref.trim() ? `issue #${value.issue_ref.replace(/^#/, '')}` : '';
  const proposal = typeof value.proposal_identity === 'string' && value.proposal_identity.trim() ? `proposal ${value.proposal_identity}` : '';
  return [issue, proposal].filter(Boolean).join(' · ');
}

function ImpactPreview({ impact, flow }: { impact: AmendmentImpact; flow: FlowGraph }) {
  return (
    <div className="space-y-3 text-sm" data-testid="amendment-impact">
      {(impact.product_before !== undefined || impact.product_after !== undefined) && (
        <section aria-label="Product scope in amendment">
          <p>Current Product: {impact.product_before
            ? flow.product_id === impact.product_before && flow.product_status === 'accessible' && flow.product_name
              ? `${flow.product_name} (${impact.product_before})` : `${impact.product_before} (access not verified)`
            : 'not selected'}</p>
          <p>Proposed Product: {impact.product_after
            ? flow.product_id === impact.product_after && flow.product_status === 'accessible' && flow.product_name
              ? `${flow.product_name} (${impact.product_after})` : `${impact.product_after} (access not verified)`
            : 'not selected'}</p>
        </section>
      )}
      {flow.planning_epics?.map((epic) => <p key={epic.address}>Current planning epic: {epic.issue.repository}#{epic.issue.issue_number}</p>)}
      {impact.blocking.length > 0 && (
        <Alert variant="error" title="This revision cannot be accepted">
          <ul>{impact.blocking.map((item) => <li key={`${item.rule}-${item.where}`}>{item.rule}: {item.message}</li>)}</ul>
        </Alert>
      )}
      {impact.nodes.filter((node) => node.kind === 'story' && node.change === 'modified'
        && repositoryLabel(node.before) !== repositoryLabel(node.after)).map((node) => (
        <section key={node.address} aria-label={`Repository scope change for ${node.title}`} className="rounded border border-amber-400 p-3">
          <h4 className="font-semibold">Story repository scope change: {node.title}</h4>
          <p>Accepted target: {repositoryLabel(node.before)}{storyReference(node.before) && ` · ${storyReference(node.before)}`}</p>
          <p>Proposed target: {repositoryLabel(node.after)}{storyReference(node.after) && ` · ${storyReference(node.after)}`}</p>
          <p>Acceptance requires the server's current review; a started story cannot be retargeted.</p>
        </section>
      ))}
      <DroppedReceipts receipts={impact.receipts_dropped ?? []} preview />
      {impact.policy_changed && (
        <section>
          <h4 className="font-semibold">Execution authority changes</h4>
          <pre className="overflow-auto rounded bg-gray-100 p-2 text-xs dark:bg-gray-900">{JSON.stringify({ before: impact.policy_before, after: impact.policy_after }, null, 2)}</pre>
        </section>
      )}
      <EffectiveLimits before={impact.effective_limits_before} after={impact.effective_limits_after} />
      {(impact.issue_publications?.length ?? 0) > 0 && (
        <section data-testid="amendment-issue-publications">
          <h4 className="font-semibold">Issues published on accept</h4>
          <ul>{impact.issue_publications?.map((item) => <li key={item.address}>
            {item.address} → {item.repository}: {item.creates_issue
              ? (item.action === 'create' ? 'creates a new issue' : 'reconciles an interrupted publication (no second issue)')
              : `uses issue #${item.issue_number ?? '?'}${item.action === 'update' ? ' (content refreshed)' : ''}`}
          </li>)}</ul>
        </section>
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        <Topology label="Before" nodes={impact.before_nodes ?? []} edges={impact.before_edges ?? []} impact={impact} />
        <Topology label="After" nodes={impact.after_nodes ?? []} edges={impact.after_edges ?? []} impact={impact} />
      </div>
      <section>
        <h4 className="font-semibold">Nodes and current state</h4>
        <ul>{impact.nodes.filter((node) => node.change !== 'unchanged').map((node) => <li key={node.address}>
          {node.change}: {node.address} ({node.kind}, {node.state ?? 'new'}) · issue {node.issue_ref ?? 'unbound'} · repository {node.repository_identity ?? 'unresolved'} ({node.provider_repository_id ?? 'unresolved id'}) · owner {node.execution_owner ?? 'default'} · adapter {node.execution_adapter ?? 'default'}
          {(node.criteria?.length ?? 0) > 0 && <> · criteria {node.criteria?.join(', ')}</>}
          {(node.required_inputs?.length ?? 0) > 0 && <> · inputs {node.required_inputs?.map((input) => `${input.name} (${input.source})`).join(', ')}</>}
          {(node.evidence_expectations?.length ?? 0) > 0 && <> · evidence {node.evidence_expectations?.join(', ')}</>}
          {node.change === 'modified' && <pre className="overflow-auto text-xs">{JSON.stringify({ before: node.before, after: node.after }, null, 2)}</pre>}
        </li>)}</ul>
      </section>
      <section>
        <h4 className="font-semibold">Dependency changes</h4>
        <ul>{impact.edges.map((edge) => <li key={`${edge.change}-${edge.from_address}-${edge.to_address}`}>{edge.change}: {edge.from_address} → {edge.to_address}</li>)}</ul>
      </section>
      <section>
        <h4 className="font-semibold">Waves, gates, and downstream impact</h4>
        <ul>
          {impact.wave_changes.map((wave) => <li key={wave.wave}>{wave.wave}: +{wave.added_addresses.join(', ')} -{wave.removed_addresses.join(', ')}</li>)}
          {impact.downstream.map((node) => <li key={node.address}>{node.address}: waits on +{node.added_prerequisites.join(', ')} -{node.removed_prerequisites.join(', ')}</li>)}
          {impact.gates_added.map((gate) => <li key={`gate+${gate}`}>gate added: {gate}</li>)}
          {impact.gates_removed.map((gate) => <li key={`gate-${gate}`}>gate removed: {gate}</li>)}
        </ul>
      </section>
      <section>
        <h4 className="font-semibold">Proposed execution order</h4>
        <ol>{impact.execution_order.map((stage) => <li key={`${stage.stage}-${stage.waves.join()}`}>Stage {stage.stage ?? 'unresolved'}: {stage.waves.join(' ∥ ')}</li>)}</ol>
      </section>
    </div>
  );
}

export function AmendmentReviewPanel({ flow }: { flow: FlowGraph }) {
  const { hasPermission } = usePermissions();
  const allowed = hasPermission(Permission.PLAN_APPROVE);
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState('');
  const [requestText, setRequestText] = useState('');
  const [ref, setRef] = useState('');
  const [title, setTitle] = useState('');
  const [kind, setKind] = useState('story');
  const [issueRef, setIssueRef] = useState('');
  const [repositoryIdentity, setRepositoryIdentity] = useState('');
  const [executionOwner, setExecutionOwner] = useState('');
  const [executionAdapter, setExecutionAdapter] = useState('');
  const [criteria, setCriteria] = useState('');
  const [requiredInputs, setRequiredInputs] = useState('');
  const [evidenceExpectations, setEvidenceExpectations] = useState('');
  const [placement, setPlacement] = useState<(typeof placements)[number]>('insert_after');
  const [anchor, setAnchor] = useState(flow.nodes[0] ? `${flow.slug}/${flow.nodes[0].epic_ref}/${flow.nodes[0].wave_ref}/${flow.nodes[0].node_ref}` : '');
  const [rejoinAt, setRejoinAt] = useState('');
  const [requires, setRequires] = useState('');
  const [precedes, setPrecedes] = useState('');
  const [epicRef, setEpicRef] = useState(flow.nodes[0]?.epic_ref ?? 'epic-1');
  const [waveRef, setWaveRef] = useState(flow.nodes[0]?.wave_ref ?? 'wave-1');
  const [documentText, setDocumentText] = useState<string | null>(null);
  const [reason, setReason] = useState('');

  const list = useQuery({ queryKey: ['orchestration', 'amendments', flow.flow_id], queryFn: () => listAmendments(flow.flow_id), enabled: allowed });
  const selected = useQuery({ queryKey: ['orchestration', 'amendment', flow.flow_id, selectedId], queryFn: () => getAmendment(flow.flow_id, selectedId), enabled: allowed && Boolean(selectedId) });
  const preview = useQuery({ queryKey: ['orchestration', 'amendment-preview', flow.flow_id, selectedId], queryFn: () => previewAmendment(flow.flow_id, selectedId), enabled: allowed && Boolean(selectedId) });
  const current = selected.data;
  const savedDocumentText = current?.proposal_document ? JSON.stringify(current.proposal_document, null, 2) : '';
  const hasUnsavedEdits = documentText !== null && documentText !== savedDocumentText;
  const readError = selected.error ?? preview.error;
  const canAccept = current?.state === 'pending' && current.base_is_current && !hasUnsavedEdits
    && selected.isSuccess && !selected.isFetching && preview.isSuccess && !preview.isFetching
    && preview.data.acceptable && preview.data.base_plan_version === current.base_plan_version
    && preview.data.base_plan_hash === current.base_plan_hash;

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['orchestration', 'amendments', flow.flow_id] });
    await queryClient.invalidateQueries({ queryKey: ['orchestration', 'amendment', flow.flow_id] });
    await queryClient.invalidateQueries({ queryKey: ['orchestration', 'amendment-preview', flow.flow_id] });
    await queryClient.invalidateQueries({ queryKey: ['orchestration', 'flow-graph', flow.flow_id] });
  };
  const mutation = useMutation({ mutationFn: async (action: 'propose' | 'edit' | 'reject' | 'accept') => {
    if (action === 'propose') {
      const work = [{
        ref,
        kind,
        title,
        ...(issueRef ? { issue_ref: issueRef } : {}),
        ...(repositoryIdentity ? { repository_identity: repositoryIdentity } : {}),
        ...(executionOwner ? { execution_owner: executionOwner } : {}),
        ...(executionAdapter ? { execution_adapter: executionAdapter } : {}),
        criteria: criteria.split(',').map((item) => item.trim()).filter(Boolean),
        required_inputs: requiredInputs.split(',').map((item) => item.trim()).filter(Boolean).map((item) => {
          const [name, ...source] = item.split('=');
          return { name: name.trim(), source: source.join('=').trim() };
        }),
        evidence_expectations: evidenceExpectations.split(',').map((item) => item.trim()).filter(Boolean),
        requires: requires.split(',').map((item) => item.trim()).filter(Boolean),
        precedes: precedes.split(',').map((item) => item.trim()).filter(Boolean),
      }];
      const position: Record<string, unknown> = { kind: placement };
      if (['insert_before', 'insert_after', 'parallel_branch'].includes(placement)) position.anchor = anchor;
      if (placement === 'parallel_branch') position.rejoin_at = rejoinAt.split(',').map((item) => item.trim()).filter(Boolean);
      if (['existing_wave', 'new_wave'].includes(placement)) Object.assign(position, { epic_ref: epicRef, wave_ref: waveRef });
      return proposeAmendment(flow.flow_id, { request_text: requestText, placement: { work, placement: position, purpose: requestText } });
    }
    if (!current) throw new Error('Select a revision first.');
    if (action === 'edit') return editAmendment(flow.flow_id, current.draft_id, JSON.parse(documentText ?? savedDocumentText), reason);
    if (action === 'reject') return rejectAmendment(flow.flow_id, current.draft_id, reason);
    if (!canAccept) throw new Error('Save your edits and load the current preview before accepting.');
    return acceptAmendment(flow.flow_id, current, reason);
  }, onSuccess: async (result) => {
    if ('draft_id' in result) setSelectedId(result.draft_id);
    setDocumentText(null);
    await refresh();
  }});

  useEffect(() => {
    setDocumentText(null);
  }, [flow.flow_id, selectedId]);

  if (!allowed) return <p className="text-sm text-gray-600 dark:text-gray-400">Plan changes require plan approval permission in this workspace.</p>;
  return (
    <section className="min-w-0 max-w-full space-y-4 rounded-lg border border-gray-200 p-4 [overflow-wrap:anywhere] [&_input]:min-w-0 [&_input]:max-w-full [&_select]:min-w-0 [&_select]:max-w-full dark:border-gray-700" aria-label="Plan amendments">
      <h2 className="text-lg font-semibold">Plan amendments</h2>
      <p className="text-sm text-gray-600 dark:text-gray-400">Propose placement, inspect the complete graph and authority diff, then approve only the exact revision shown.</p>
      {mutation.error && <Alert variant="error" title="Amendment action failed">{mutation.error.message}</Alert>}
      {readError && <Alert variant="error" title="Amendment readback failed">{readError.message}</Alert>}
      <div className="grid gap-2 md:grid-cols-2">
        <input aria-label="Request" value={requestText} onChange={(event) => setRequestText(event.target.value)} placeholder="Why this work is needed" />
        <input aria-label="Work title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Work title" />
        <input aria-label="Work reference" value={ref} onChange={(event) => setRef(event.target.value)} placeholder="stable-work-ref" />
        <select aria-label="Work kind" value={kind} onChange={(event) => setKind(event.target.value)}><option value="story">Development</option><option value="eval">Validation</option><option value="gate">Human gate</option></select>
        <input aria-label="Existing issue" value={issueRef} onChange={(event) => setIssueRef(event.target.value)} placeholder="Optional; leave blank to publish idempotently" />
        <input aria-label="Story repository (owner/name)" value={repositoryIdentity} onChange={(event) => setRepositoryIdentity(event.target.value)} placeholder="owner/repo" />
        <input aria-label="Execution owner" value={executionOwner} onChange={(event) => setExecutionOwner(event.target.value)} placeholder="owner or assignment" />
        <input aria-label="Execution adapter" value={executionAdapter} onChange={(event) => setExecutionAdapter(event.target.value)} placeholder="adapter" />
        <input aria-label="Criteria" value={criteria} onChange={(event) => setCriteria(event.target.value)} placeholder="comma-separated criterion ids" />
        <input aria-label="Required inputs" value={requiredInputs} onChange={(event) => setRequiredInputs(event.target.value)} placeholder="name=source, never secret values" />
        <input aria-label="Evidence expectations" value={evidenceExpectations} onChange={(event) => setEvidenceExpectations(event.target.value)} placeholder="comma-separated evidence expectations" />
        <select aria-label="Placement" value={placement} onChange={(event) => setPlacement(event.target.value as typeof placement)}>{placements.map((item) => <option key={item}>{item}</option>)}</select>
        {['insert_before', 'insert_after', 'parallel_branch'].includes(placement) && <input aria-label="Anchor address" value={anchor} onChange={(event) => setAnchor(event.target.value)} />}
        {placement === 'parallel_branch' && <input aria-label="Rejoin addresses" value={rejoinAt} onChange={(event) => setRejoinAt(event.target.value)} placeholder="comma-separated addresses" />}
        {['existing_wave', 'new_wave'].includes(placement) && <><input aria-label="Epic reference" value={epicRef} onChange={(event) => setEpicRef(event.target.value)} /><input aria-label="Wave reference" value={waveRef} onChange={(event) => setWaveRef(event.target.value)} /></>}
        <label>Prerequisite addresses<input aria-label="Prerequisite addresses" value={requires} onChange={(event) => setRequires(event.target.value)} placeholder="Comma-separated addresses this work must wait for" /></label>
        <label>Successor addresses<input aria-label="Successor addresses" value={precedes} onChange={(event) => setPrecedes(event.target.value)} placeholder="Comma-separated addresses that must wait for this work" /></label>
      </div>
      <p className="text-sm text-gray-600 dark:text-gray-400">These dependencies add to the placement's connections; they never replace existing edges. New waves inherit no connections, so specify where the work starts and where it rejoins the graph.</p>
      <Button onClick={() => mutation.mutate('propose')} disabled={mutation.isPending || !requestText || !ref || !title || (kind !== 'gate' && !issueRef && !repositoryIdentity)}>Propose without applying</Button>
      <select className="block w-full min-w-0 max-w-full rounded border p-2" aria-label="Pending amendment" value={selectedId} disabled={mutation.isPending} onChange={(event) => setSelectedId(event.target.value)}>
        <option value="">Select an amendment</option>
        {(list.data ?? []).map((draft) => <option key={draft.draft_id} value={draft.draft_id}>{draft.state}: {(draft.request_text || draft.draft_id).slice(0, 72)}{(draft.request_text?.length ?? 0) > 72 ? '…' : ''} · {draft.draft_id.slice(0, 8)}</option>)}
      </select>
      {current?.request_text && <p className="whitespace-pre-wrap text-sm">{current.request_text}</p>}
      {current && <p className="text-xs">Revision {current.proposal_hash} · base v{current.base_plan_version} ({current.base_plan_hash}) · actor {current.decision_actor ?? 'undecided'} · decision {current.decision_reason ?? 'pending'} · accepted plan {current.accepted_plan_version ?? 'none'}</p>}
      {mutation.data && 'plan_version' in mutation.data && mutation.data.draft_id === selectedId && <section aria-label="Committed receipt outcome">
        <h3>Accepted plan v{mutation.data.plan_version}{mutation.data.already_accepted ? ' (replayed)' : ''}</h3>
        <DroppedReceipts receipts={mutation.data.receipts_dropped ?? []} />
        <EffectiveLimits before={mutation.data.effective_limits_before} after={mutation.data.effective_limits_after} committed />
      </section>}
      {preview.data && <ImpactPreview impact={preview.data} flow={flow} />}
      {current && <>
        {hasUnsavedEdits && <Alert variant="warning" title="Unsaved proposal edits">Save your edits and review the new preview before accepting. The graph shown is the saved revision.</Alert>}
        <textarea aria-label="Proposal document" className="h-48 w-full font-mono text-xs" value={documentText ?? savedDocumentText} disabled={mutation.isPending} onChange={(event) => setDocumentText(event.target.value)} />
        <input aria-label="Decision reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Reason" />
        <div className="flex flex-wrap gap-2"><Button variant="secondary" onClick={() => mutation.mutate('edit')} disabled={mutation.isPending || current.state !== 'pending'}>Save new revision</Button><Button variant="danger" onClick={() => mutation.mutate('reject')} disabled={mutation.isPending || current.state !== 'pending'}>Reject</Button><Button onClick={() => mutation.mutate('accept')} disabled={mutation.isPending || !canAccept}>Accept exact revision</Button></div>
      </>}
    </section>
  );
}
