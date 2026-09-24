/**
 * Report construction and grounding — Task API T5 (#5798).
 *
 * The design's rule: "Each finding names the evidence supporting it; absent
 * evidence produces an explicit uncertainty." This module is where a model's
 * proposed report meets that rule, and it is the difference between a useful
 * report and a confident guess.
 *
 * ## Why demotion rather than rejection
 *
 * A finding whose citation does not resolve to supplied evidence is *moved* to
 * `uncertainties`, not dropped and not rejected. Dropping it would hide that the
 * model believed something, and failing the whole run would throw away the
 * findings that are properly grounded. Demotion keeps the observation visible
 * while stripping the authority a citation confers. What the caller reads is
 * "this was suggested but nothing supplied supports it" — which is true, and
 * actionable.
 *
 * Structural failure is different: if the model's output is not a report at all,
 * there is nothing to ground, and the run fails (T5-AC05). Demotion is for
 * claims we can place; failure is for output we cannot parse.
 */

import {
  assertInvestigatorReport,
  ProtocolViolation,
  type EvidenceRef,
  type Finding,
  type InvestigatorReport,
} from './protocol.js';

/** Everything the caller supplied, indexed for citation checking. */
export interface EvidenceIndex {
  /** Citable reference tokens, e.g. `logs.txt` or `inputs.incident_window`. */
  readonly refs: ReadonlySet<string>;
  /** The full provenance records, emitted in the report's `evidence_refs`. */
  readonly records: readonly EvidenceRef[];
}

/** Outcome of grounding a candidate report. */
export interface GroundingOutcome {
  readonly report: InvestigatorReport;
  /** Statements moved from findings to uncertainties, for the progress note. */
  readonly demoted: readonly string[];
}

/**
 * Raised when candidate output cannot be treated as a report at all.
 *
 * Distinct from {@link ProtocolViolation} so the caller can map it to the
 * contract's `invalid_agent_output` failure code. T5-AC05 turns on this being a
 * failure path with no route to completion: there is deliberately no "best
 * effort" fallback that would synthesize a plausible report from unparseable
 * output, because that is precisely how a failed run gets reported as a
 * successful one.
 */
export class InvalidAgentOutputError extends Error {
  readonly isInvalidAgentOutput = true as const;

  constructor(reason: string) {
    super(reason.length > 1000 ? `${reason.slice(0, 999)}…` : reason);
    this.name = 'InvalidAgentOutputError';
  }
}

/**
 * Normalise a citation for comparison.
 *
 * Line-range suffixes are stripped because `logs.txt:L118-L204` cites the
 * artifact `logs.txt`; requiring an exact match would reject the contract's own
 * valid fixture, which cites exactly that way. Case is normalised because a
 * model reproducing `Logs.txt` is citing the same file, and treating that as a
 * dangling citation would demote a properly grounded finding.
 */
function citationRoot(ref: string): string {
  const withoutRange = ref.split(':')[0] ?? ref;
  return withoutRange.trim().toLowerCase();
}

/**
 * Build the citable index from what the caller supplied.
 *
 * Only these four provenances exist. There is no fetch, so nothing can enter the
 * index that the caller did not hand over — which is what makes a dangling
 * citation detectable rather than merely unlikely.
 */
export function buildEvidenceIndex(input: {
  instructions: string;
  inputs?: Record<string, unknown>;
  artifacts?: readonly { artifact_id: string; content: string }[];
  artifactNames?: ReadonlyMap<string, string>;
  followUpRefs?: readonly string[];
}): EvidenceIndex {
  const refs = new Set<string>();
  const records: EvidenceRef[] = [];

  refs.add(citationRoot('instructions'));
  records.push({ ref: 'instructions', source: 'instructions' });

  for (const key of Object.keys(input.inputs ?? {})) {
    refs.add(citationRoot(key));
    refs.add(citationRoot(`inputs.${key}`));
    records.push({ ref: `inputs.${key}`, source: 'inputs' });
  }

  for (const artifact of input.artifacts ?? []) {
    const name = input.artifactNames?.get(artifact.artifact_id) ?? artifact.artifact_id;
    refs.add(citationRoot(name));
    refs.add(citationRoot(artifact.artifact_id));
    records.push({ ref: name, source: 'artifact', artifact_id: artifact.artifact_id });
  }

  for (const ref of input.followUpRefs ?? []) {
    refs.add(citationRoot(ref));
    records.push({ ref, source: 'follow_up_input' });
  }

  return { refs, records };
}

/** Whether a citation resolves to something the caller actually supplied. */
export function resolvesToEvidence(ref: string, index: EvidenceIndex): boolean {
  return index.refs.has(citationRoot(ref));
}

function asStringArray(value: unknown, field: string, max: number): string[] {
  if (value === undefined || value === null) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw new InvalidAgentOutputError(`model output field ${field} is not an array`);
  }
  const out: string[] = [];
  for (const entry of value) {
    if (typeof entry !== 'string') {
      throw new InvalidAgentOutputError(`model output field ${field} contains a non-string entry`);
    }
    const trimmed = entry.trim();
    if (trimmed.length > 0) {
      out.push(trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed);
    }
  }
  return out;
}

/**
 * Turn candidate model output into a grounded, contract-valid report.
 *
 * Throws {@link InvalidAgentOutputError} when the output is not a report. Never
 * returns a report that would fail {@link assertInvestigatorReport}: the final
 * assertion at the end of this function is the guarantee, so a caller cannot
 * receive something it would then be unable to emit.
 */
export function groundReport(candidate: unknown, index: EvidenceIndex): GroundingOutcome {
  if (typeof candidate !== 'object' || candidate === null || Array.isArray(candidate)) {
    throw new InvalidAgentOutputError('model output is not a JSON object');
  }
  const raw = candidate as Record<string, unknown>;

  const summaryValue = raw['summary'];
  if (typeof summaryValue !== 'string' || summaryValue.trim().length === 0) {
    throw new InvalidAgentOutputError('model output has no summary');
  }
  const summary =
    summaryValue.trim().length > 4000 ? `${summaryValue.trim().slice(0, 3999)}…` : summaryValue.trim();

  const rawFindings = raw['findings'];
  if (rawFindings !== undefined && rawFindings !== null && !Array.isArray(rawFindings)) {
    throw new InvalidAgentOutputError('model output field findings is not an array');
  }

  const findings: Finding[] = [];
  const demoted: string[] = [];
  const uncertainties = asStringArray(raw['uncertainties'], 'uncertainties', 1000);

  for (const entry of (rawFindings ?? []) as unknown[]) {
    if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) {
      throw new InvalidAgentOutputError('a finding in model output is not an object');
    }
    const record = entry as Record<string, unknown>;
    const statementValue = record['statement'];
    if (typeof statementValue !== 'string' || statementValue.trim().length === 0) {
      throw new InvalidAgentOutputError('a finding in model output has no statement');
    }
    const statement =
      statementValue.trim().length > 2000
        ? `${statementValue.trim().slice(0, 1999)}…`
        : statementValue.trim();

    const cited = asStringArray(record['evidence_refs'], 'finding.evidence_refs', 256);
    const grounded = cited.filter((ref) => resolvesToEvidence(ref, index));

    if (grounded.length === 0) {
      // Either no citation at all, or every citation names something that was
      // never supplied. Both mean the claim has no provenance, so it loses its
      // standing as a finding and is recorded as an uncertainty instead.
      demoted.push(statement);
      uncertainties.push(
        cited.length === 0
          ? `Unsupported by supplied evidence: ${statement}`
          : `Cited evidence not found in the supplied material (${cited.join(', ')}): ${statement}`,
      );
      continue;
    }

    const finding: Finding = { statement, evidence_refs: grounded };
    const confidence = record['confidence'];
    if (confidence === 'low' || confidence === 'medium' || confidence === 'high') {
      finding.confidence = confidence;
    }
    findings.push(finding);
  }

  // Only the provenance records actually cited are emitted, so the report's
  // evidence list describes what the investigation used rather than everything
  // it was handed.
  const citedRoots = new Set(findings.flatMap((f) => f.evidence_refs.map(citationRoot)));
  const evidenceRefs = index.records.filter((record) => citedRoots.has(citationRoot(record.ref)));

  const report: InvestigatorReport = {
    summary,
    findings,
    uncertainties: uncertainties.slice(0, 50),
    recommendations: asStringArray(raw['recommendations'], 'recommendations', 1000).slice(0, 50),
    evidence_refs: evidenceRefs,
  };

  // The guarantee this function owes its caller. A ProtocolViolation escaping
  // here would mean grounding produced something unemittable, which is a defect
  // in this module rather than bad model output — so it is re-raised as invalid
  // output only after being given the chance to surface in tests.
  try {
    assertInvestigatorReport(report);
  } catch (error) {
    const reason = error instanceof ProtocolViolation ? error.message : 'report failed validation';
    throw new InvalidAgentOutputError(`grounded report is not contract-valid: ${reason}`);
  }

  return { report, demoted };
}

/**
 * Extract the first JSON object from model text.
 *
 * Models wrap JSON in prose or fences even when asked not to, and failing a run
 * for a markdown fence would be a brittle agent rather than a careful one. What
 * this deliberately does *not* do is repair malformed JSON or infer fields from
 * prose: unparseable output must fail (T5-AC05), and a lenient reconstruction is
 * indistinguishable from inventing a result.
 */
export function extractJsonObject(text: string): unknown {
  const fenced = /```(?:json)?\s*([\s\S]*?)```/.exec(text);
  const candidates = [fenced?.[1], text];

  for (const candidate of candidates) {
    if (candidate === undefined) {
      continue;
    }
    const start = candidate.indexOf('{');
    const end = candidate.lastIndexOf('}');
    if (start === -1 || end <= start) {
      continue;
    }
    try {
      return JSON.parse(candidate.slice(start, end + 1));
    } catch {
      continue;
    }
  }
  throw new InvalidAgentOutputError('model output contains no parseable JSON object');
}
