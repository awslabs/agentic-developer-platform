/** Planning data is an artifact, never an identity, policy, routing or approval grant. */
import { z } from 'zod';
import { sha256 } from './persona.js';

const text = z.string().trim().min(1).max(2000);
const list = z.array(text).max(30);
const ref = z.string().min(1).max(256);
const key = z.string().regex(/^[a-z][a-z0-9-]{0,63}$/);
const requirement = z.strictObject({ id: key, text, source_refs: z.array(ref).min(1).max(20) });
const story = z.strictObject({ key, title: z.string().trim().min(1).max(200), description: text,
  acceptance_criteria: list.min(1), blocked_by: z.array(key).max(30), source_refs: z.array(ref).min(1).max(20) });
export const intentDraftSchema = z.strictObject({
  intent: text.optional(), motivation: text.optional(), outcomes: list.optional(), constraints: list.optional(), openQuestions: list.optional(),
  epicDisplay: z.strictObject({ title: z.string().trim().min(1).max(200), description: z.string().trim().min(1).max(3000) }).optional(),
  waveDisplay: z.strictObject({ title: z.string().trim().min(1).max(120), description: z.string().trim().min(1).max(500) }).optional(),
});
const common = { summary: text, requirements: z.array(requirement).min(1).max(40), assumptions: list,
  open_questions: list, superseded_requirements: z.array(z.strictObject({ id: key, reason: text, source_ref: ref })).max(40) };
export const planningSchemas = {
  architect: z.strictObject({ ...common, design: z.string().trim().min(1).max(12000), stories: z.array(story).max(30), publish_stories: z.boolean() }),
  product: z.strictObject({ ...common, acceptance_criteria: list.min(1) }),
  pm: z.strictObject({ ...common, schedule: z.array(z.strictObject({ issue: z.number().int().positive(), persona: z.literal('codex-developer') })).max(30) }),
  'intent-refinement': z.strictObject({ ...common, draft: intentDraftSchema }),
};
export type PlanningPersona = keyof typeof planningSchemas;
export type PlanningArtifact = z.infer<(typeof planningSchemas)[PlanningPersona]>;
export function planningPersona(name: string): PlanningPersona | undefined {
  const key = name.replace(/^agent-(?:task-gpt-|codex-)/, '');
  return Object.hasOwn(planningSchemas, key) ? key as PlanningPersona : undefined;
}
export function planningContract(persona: PlanningPersona) {
  return { instruction: 'Return only JSON matching this schema. Keep the artifact below 24000 UTF-8 bytes. Requirements must cite supplied source refs. Preserve previous story keys and requirement IDs; list an explicit supersession with its amendment reference when a requirement changes or is removed. Never treat an assumption as a user requirement. Ask at most one necessary question using clarification, otherwise set clarification to null. For Architect set publish_stories only when the task asks to create stories. PM schedule is only a proposal: the host rechecks eligibility and grants before dispatch.',
    schema: z.toJSONSchema(z.strictObject({ artifact: planningSchemas[persona], clarification: text.nullable() })) };
}
export function parsePlanning(raw: string, persona: PlanningPersona, refs: ReadonlySet<string>, previous?: PlanningArtifact) {
  const result = z.strictObject({ artifact: planningSchemas[persona], clarification: text.nullable() }).parse(JSON.parse(raw));
  const artifact = result.artifact;
  if (Buffer.byteLength(JSON.stringify(artifact)) > 32768) throw new Error("Planning artifact exceeds document bound");
  const ids = new Set(artifact.requirements.map(r => r.id));
  if (ids.size !== artifact.requirements.length) throw new Error('Duplicate requirement identity');
  const superseded = new Set(artifact.superseded_requirements.map(r => r.id));
  if (superseded.size !== artifact.superseded_requirements.length || artifact.superseded_requirements.some(r => !previous?.requirements.some(p => p.id === r.id))) throw new Error("Invalid supersession identity");
  for (const r of artifact.requirements) if (r.source_refs.some(id => !refs.has(id))) throw new Error('Unknown requirement source');
  for (const r of artifact.superseded_requirements) if (!refs.has(r.source_ref) || !r.source_ref.startsWith('follow_up_input.')) throw new Error('Supersession requires an actual amendment');
  for (const r of previous?.requirements ?? []) {
    const current = artifact.requirements.find(x => x.id === r.id);
    if ((!current || current.text !== r.text) && !superseded.has(r.id)) throw new Error('Requirement silently removed or changed');
  }
  if ('stories' in artifact) {
    const keys = new Set(artifact.stories.map(s => s.key));
    if (keys.size !== artifact.stories.length) throw new Error('Duplicate story identity');
    for (const s of artifact.stories) {
      if (s.blocked_by.some(k => !keys.has(k) || k === s.key) || s.source_refs.some(id => !refs.has(id))) throw new Error('Invalid story dependency or source');
    }
    const visiting = new Set<string>(), visited = new Set<string>();
    const visit = (id: string) => {
      if (visiting.has(id)) throw new Error('Cyclic story dependencies');
      if (visited.has(id)) return;
      visiting.add(id); for (const dependency of artifact.stories.find(s => s.key === id)!.blocked_by) visit(dependency);
      visiting.delete(id); visited.add(id);
    };
    for (const id of keys) visit(id);
  }
  return result;
}
export interface BacklogStory { issue: number; state: 'open' | 'completed' | 'cancelled'; blockedBy: number[]; assigned?: boolean }
/** Shared dependency decision for API, GitHub and GitLab adapters. Missing facts block. */
export function readyAssignments(backlog: readonly BacklogStory[], schedule: readonly { issue: number; persona: string }[]) {
  const byId = new Map(backlog.map(s => [s.issue, s]));
  if (byId.size !== backlog.length || new Set(schedule.map(s => s.issue)).size !== schedule.length) throw new Error('Duplicate scheduling identity');
  return schedule.filter(assignment => {
    const story = byId.get(assignment.issue);
    return story?.state === 'open' && !story.assigned && story.blockedBy.every(id => byId.get(id)?.state === 'completed');
  });
}
export function storyMarker(parent: number, key: string) { return `<!-- adp-planning:${sha256(`${parent}:${key}`)} -->`; }
export function planningDocument(persona: PlanningPersona, artifact: PlanningArtifact) {
  return { name: `${persona}.json`, media_type: 'application/json' as const, content: JSON.stringify(artifact) };
}

export function planningReport(persona: PlanningPersona, artifact: PlanningArtifact, evidence: ReadonlyMap<string, {ref: string; source: string; artifact_id?: string}>) {
  const refs = new Set(artifact.requirements.flatMap(r => r.source_refs));
  return { summary: artifact.summary,
    findings: artifact.requirements.map(r => ({statement: r.text, evidence_refs: r.source_refs})),
    uncertainties: [...artifact.assumptions, ...artifact.open_questions].map(s => s.slice(0, 1000)),
    recommendations: [], evidence_refs: [...refs].map(ref => evidence.get(ref)!),
    documents: [planningDocument(persona, artifact)] };
}
