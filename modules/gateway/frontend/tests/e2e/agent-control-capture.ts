/**
 * Measured-evidence producer for the run-control dashboard — Issue #5878 (S7).
 *
 * This module turns the Playwright scenario in `agent-control.spec.ts` into a
 * producer of `browser_control_run.json`, the artifact
 * `platform/scripts/agent-control-eval.py` reads for its wave-4 checks
 * (W4-02, W4-04, W4-08 today; W4-06's keys are populated here too — see
 * "Keys without a predicate" below).
 *
 * ---------------------------------------------------------------------------
 * Why a separate module rather than assertions in the spec
 * ---------------------------------------------------------------------------
 *
 * A passing spec and a complete capture are different claims, and conflating
 * them is the failure this story exists to prevent. `expect(...)` answers "did
 * this run behave?"; the evaluator asks "what did the browser MEASURE?" — how
 * many control nodes rendered while the flag was unknown, how many milliseconds
 * actually separated two polls, which URLs were actually requested. A green
 * report cannot answer any of those, so the measurements are accumulated here as
 * data and validated for completeness in one place at the end of the run.
 *
 * The shape is deliberately fragment-then-merge:
 *
 *   each scenario  ->  <capture-dir>/observations/<scenario>.json
 *   global teardown ->  merge, validate, write browser_control_run.json
 *
 * Two properties follow from that split, and both are requirements rather than
 * conveniences:
 *
 * 1. A scenario that never ran leaves no fragment, so a missing scenario is
 *    structurally INCOMPLETE rather than silently absent from the merge. A
 *    capture that is missing a measurement is not a capture with a `false` in
 *    it — "not measured" and "measured as zero" are different facts and only
 *    one of them is evidence.
 * 2. Completeness is checked once, at the exit, instead of being distributed
 *    across four scenarios that each believe another one covered the gap.
 *
 * On an incomplete or failed run the producer writes
 * `browser_control_run.partial.json` — a deliberately DIFFERENT filename, so a
 * consumer configured to read the real artifact cannot pick a partial one up by
 * accident — and fails the run.
 *
 * ---------------------------------------------------------------------------
 * Mocked evidence is not live evidence
 * ---------------------------------------------------------------------------
 *
 * Every capture carries `mode`. A mocked capture is real-browser evidence about
 * a real bundle whose gateway was stubbed: it can show that the documented
 * clicks reach the documented requests and that the honest wording renders. It
 * cannot show that a worker paused. `verifyForLiveAcceptance` refuses a mocked
 * capture with a specific reason, and that refusal is the documented gate on the
 * live handoff path — NOT a relaxation anywhere in the evaluator, whose
 * predicates this module treats as fixed and reuses unweakened.
 *
 * ---------------------------------------------------------------------------
 * Keys without a predicate
 * ---------------------------------------------------------------------------
 *
 * `request_destinations`, `request_bodies_contain_pod_address`,
 * `request_bodies_contain_token` and `spoofed_identity_rejected` are in
 * `REQUIRED_ARTIFACT_KEYS['browser_control_run']` but no check reads them yet
 * (W4-06 belongs to another story). They are measured and populated anyway: the
 * artifact loader rejects an incomplete artifact wholesale, so omitting them
 * would make every wave-4 check unrunnable, and a key that is required later is
 * better measured now than back-filled from memory.
 */

import { createHash } from 'node:crypto';
import { mkdirSync, readdirSync, readFileSync, writeFileSync, existsSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

const HERE = dirname(fileURLToPath(import.meta.url));

/** Files whose contents identify the scenario that produced a capture. */
const DIGEST_SOURCES = ['agent-control.spec.ts', 'agent-control-capture.ts', 'agent-control.config.ts',
  'agent-control.setup.ts', 'agent-control.teardown.ts', 'agent-control-gate.ts'];

export const CAPTURE_DIR = process.env.CONTROL_E2E_CAPTURE_DIR || '';
export const LIVE = process.env.CONTROL_E2E_LIVE === '1';

/** Filenames, fixed so a consumer config can name them. */
export const CAPTURE_FILENAME = 'browser_control_run.json';
export const PARTIAL_FILENAME = 'browser_control_run.partial.json';
const OBSERVATION_SUBDIR = 'observations';

/**
 * Sentinels for the leak checks.
 *
 * Exported so the spec's mocked gateway can offer them the way a buggy or
 * hostile worker would, and so the privacy regression can inject them
 * deliberately. They are compared in memory; see `LeakWatch`.
 */
/**
 * The mocked world's fixture run ids.
 *
 * Declared here, and imported by the spec, so the ids the capture claims to
 * describe and the ids the scenarios actually drive cannot drift apart. Two
 * independent copies would allow a capture to name one run while its observations
 * came from another, which is exactly the endpoint/run identity binding AC-03
 * requires be refusable.
 */
export const MOCK_RUN_ID = 'run-e2e-live';
export const MOCK_ABORT_RUN_ID = 'run-e2e-abort';

export const SENTINEL_POD_ADDRESS = '10.0.42.7';
export const SENTINEL_POD_TOKEN = 'pod-bearer-token-must-never-leave-the-gateway';

/** Scenario fragments that must all be present for a capture to be complete. */
export const REQUIRED_SCENARIOS = [
  'flag-gating',
  'control-lifecycle',
  'polling-lifecycle',
  'authorization',
] as const;
export type ScenarioName = (typeof REQUIRED_SCENARIOS)[number];

/**
 * Keys the evaluator requires, transcribed from
 * `REQUIRED_ARTIFACT_KEYS['browser_control_run']` in
 * `platform/scripts/agent-control-eval.py`.
 *
 * Transcribed rather than imported because the consumer is Python. The
 * duplication is checked by actually running the consumer against a produced
 * capture, which is what AC-01 asks for — a list that agreed with the
 * evaluator only in a comment would drift silently.
 */
export const REQUIRED_CAPTURE_KEYS = [
  'bundle_revision',
  'gateway_url',
  'captured_at',
  'spec_digest',
  'flag_off',
  'flag_loading',
  'flag_error',
  'advertised_capabilities',
  'rendered_controls',
  'nonowner_submit_blocked',
  'terminal_submit_blocked',
  'phase_sequence',
  'pause_copy_mentions_spend',
  'active_tool_reason',
  'steer_request',
  'steer_status_sequence',
  'poll_intervals_ms',
  'polled_while_hidden',
  'polled_after_close',
  'polled_after_terminal',
  'backoff_intervals_ms',
  'detail_refreshed_after_command',
  'request_destinations',
  'request_bodies_contain_pod_address',
  'request_bodies_contain_token',
  'spoofed_identity_rejected',
] as const;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** An input problem. Raised before any browser command runs. */
export class CaptureInputError extends Error {}

/** The run did not produce a complete set of measurements. */
export class CaptureIncompleteError extends Error {}

// ---------------------------------------------------------------------------
// Input validation — before commands, never after
// ---------------------------------------------------------------------------

export interface CaptureInputs {
  captureDir: string;
  live: boolean;
  gatewayUrl: string;
  claimedRevision: string;
  assetManifestPath: string;
  runId: string;
  abortRunId: string;
}

/**
 * Validate every producer input and claim an exclusive output directory.
 *
 * Ordering is the point: this runs in global setup, so a misconfigured run fails
 * before the browser sends a single control command. A producer that discovers
 * its output directory is unusable AFTER pausing somebody's run has already had
 * the side effect it cannot take back.
 *
 * "Exclusive" means the directory must not already hold a capture. Merging a new
 * run's fragments into an old run's leftovers would produce an artifact
 * describing two runs while naming one.
 */
export function validateInputs(env: NodeJS.ProcessEnv = process.env): CaptureInputs {
  const captureDir = (env.CONTROL_E2E_CAPTURE_DIR || '').trim();
  if (!captureDir) {
    throw new CaptureInputError(
      'CONTROL_E2E_CAPTURE_DIR is required to produce browser_control_run.json. Without an output ' +
        'directory this run measures nothing and the scenario is only a pass/fail test.',
    );
  }
  const dir = resolve(captureDir);
  for (const name of [CAPTURE_FILENAME, PARTIAL_FILENAME, OBSERVATION_SUBDIR]) {
    if (existsSync(join(dir, name))) {
      throw new CaptureInputError(
        `CONTROL_E2E_CAPTURE_DIR (${dir}) already contains ${name}. The capture directory must be ` +
          'exclusive to one run: merging fresh fragments with a previous run\'s leftovers would ' +
          'produce one artifact describing two runs.',
      );
    }
  }

  const live = env.CONTROL_E2E_LIVE === '1';
  const gatewayUrl = (env.GATEWAY_URL || '').trim();
  if (!gatewayUrl) {
    throw new CaptureInputError(
      'GATEWAY_URL is required: which deployment the browser drove is part of the observation, not ' +
        'context for it.',
    );
  }

  const claimedRevision = (env.CONTROL_E2E_BUNDLE_REVISION || '').trim();
  if (!claimedRevision) {
    throw new CaptureInputError(
      'CONTROL_E2E_BUNDLE_REVISION is required: it is the revision this capture CLAIMS to describe, ' +
        'and it is only accepted once the served asset hashes are shown to match it.',
    );
  }

  // The deployment receipt. Required in live mode, because in live mode the
  // claim is about somebody else's deployment and an environment SHA on its own
  // is a claim rather than served-source evidence.
  const assetManifestPath = (env.CONTROL_E2E_ASSET_MANIFEST || '').trim();
  if (live && !assetManifestPath) {
    throw new CaptureInputError(
      'Live capture requires CONTROL_E2E_ASSET_MANIFEST: the operator deployment receipt (asset path ' +
        '-> sha256, plus its revision). Without it the browser can only repeat the revision it was ' +
        'told, which is not evidence about the served bundle.',
    );
  }
  if (assetManifestPath) {
    const manifest = readAssetManifest(assetManifestPath);
    if (manifest.revision !== claimedRevision) {
      throw new CaptureInputError(
        `the asset manifest at ${assetManifestPath} describes revision ${manifest.revision} ` +
          `but this run claims ${claimedRevision}. A capture may not claim a revision its own ` +
          'deployment receipt does not describe.',
      );
    }
  }

  // In mocked mode the runs are this harness's own fixtures, so their ids are
  // known here rather than supplied. They are still recorded, because "which run
  // was observed" is part of the observation in either mode — a capture that
  // names no subject cannot be checked for endpoint/run identity at all, and
  // defaulting them to empty would quietly turn that check off for every mocked
  // run.
  const runId = (env.CONTROL_E2E_RUN_ID || '').trim() || (live ? '' : MOCK_RUN_ID);
  const abortRunId = (env.CONTROL_E2E_ABORT_RUN_ID || '').trim() || (live ? '' : MOCK_ABORT_RUN_ID);
  if (live) {
    if (!runId || !abortRunId) {
      throw new CaptureInputError(
        'Live capture requires CONTROL_E2E_RUN_ID and CONTROL_E2E_ABORT_RUN_ID naming two real ' +
          'controllable runs. An unconfigured live run must fail, never skip: a skipped control check ' +
          'reads as covered.',
      );
    }
    if (runId === abortRunId) {
      throw new CaptureInputError('Live pause and abort fixtures must be distinct runs');
    }
    if (!(env.CONTROL_E2E_SESSION_FILE || '').trim()) {
      throw new CaptureInputError(
        'Live capture requires CONTROL_E2E_SESSION_FILE with a real fixture-user session. Live ' +
          'authorization must be answered by the gateway, so no synthetic token is ever injected in ' +
          'live mode.',
      );
    }
    if (!(env.CONTROL_E2E_NONOWNER_SESSION_FILE || '').trim()) {
      throw new CaptureInputError(
        'Live capture requires CONTROL_E2E_NONOWNER_SESSION_FILE: "a non-owner cannot submit" has to ' +
          'be observed against a real second identity and a real gateway refusal, not inferred from a ' +
          'hidden button.',
      );
    }
    if (!(env.CONTROL_E2E_DISABLED_URL || '').trim()) {
      throw new CaptureInputError(
        'Live capture requires CONTROL_E2E_DISABLED_URL: a fixture serving the same bundle with the ' +
          'control flag off. The flag-off render is an observation about a deployment, so it needs a ' +
          'deployment with the flag off.',
      );
    }
    // Validate both sessions before the first scenario can mutate either run.
    for (const key of ['CONTROL_E2E_SESSION_FILE', 'CONTROL_E2E_NONOWNER_SESSION_FILE']) {
      let session: Record<string, unknown>;
      try {
        session = JSON.parse(readFileSync(env[key] as string, 'utf8'));
      } catch {
        throw new CaptureInputError(`${key} must name a readable fixture-session JSON file`);
      }
      if (!session || typeof session.access_token !== 'string' || !session.access_token ||
          typeof session.id_token !== 'string' || !session.id_token ||
          typeof session.expires_at_ms !== 'number' || session.expires_at_ms <= Date.now()) {
        throw new CaptureInputError(`${key} contains an incomplete or expired fixture session`);
      }
    }
  }

  mkdirSync(join(dir, OBSERVATION_SUBDIR), { recursive: true });
  return { captureDir: dir, live, gatewayUrl, claimedRevision, assetManifestPath, runId, abortRunId };
}

export interface AssetManifest {
  revision: string;
  /** Asset path (as served, e.g. `/assets/index-a1b2c3.js`) -> sha256 hex. */
  assets: Record<string, string>;
}

function readAssetManifest(path: string): AssetManifest {
  if (!existsSync(path)) {
    throw new CaptureInputError(`CONTROL_E2E_ASSET_MANIFEST points at ${path}, which does not exist`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    throw new CaptureInputError(`CONTROL_E2E_ASSET_MANIFEST at ${path} is not valid JSON: ${error}`);
  }
  const manifest = parsed as AssetManifest;
  if (!manifest || typeof manifest.revision !== 'string' || !manifest.revision.trim()) {
    throw new CaptureInputError(`the asset manifest at ${path} declares no 'revision' string`);
  }
  if (!manifest.assets || typeof manifest.assets !== 'object' || !Object.keys(manifest.assets).length) {
    throw new CaptureInputError(
      `the asset manifest at ${path} declares no 'assets' map. A receipt with no asset hashes cannot ` +
        'establish which source was served.',
    );
  }
  return manifest;
}

/** The scenario's own identity: a weakened spec must not leave identical evidence. */
export function specDigest(): string {
  const hash = createHash('sha256');
  for (const name of DIGEST_SOURCES) {
    const path = join(HERE, name);
    hash.update(name);
    hash.update('\0');
    hash.update(existsSync(path) ? readFileSync(path) : Buffer.from('<absent>'));
    hash.update('\0');
  }
  return `sha256:${hash.digest('hex')}`;
}

// ---------------------------------------------------------------------------
// Served-asset identity
// ---------------------------------------------------------------------------

export interface ServedAssetEvidence {
  /** Revision the run claims, echoed for the record. */
  claimed_revision: string;
  /** How the claim was checked. */
  method: 'asset_manifest_match' | 'local_build_hash';
  /** Assets the browser actually loaded, path -> sha256 of the bytes received. */
  observed_assets: Record<string, string>;
  /** Assets matched against the receipt, and any that disagreed. */
  matched_assets: string[];
  mismatched_assets: string[];
  /** True only when at least one asset matched and none disagreed. */
  verified: boolean;
}

/**
 * Hash the requested asset paths out of the local build directory.
 *
 * Only used when no deployment receipt was supplied, i.e. mocked mode. Paths are
 * taken from what the browser actually requested and resolved under `dist/`;
 * anything absent there is simply omitted, so a served asset with no local
 * counterpart shows up as "did not match" rather than as a spurious agreement.
 */
function hashLocalBuild(servedPaths: string[]): Record<string, string> {
  const root = join(HERE, '..', '..', 'dist');
  const hashes: Record<string, string> = {};
  for (const served of servedPaths) {
    const local = join(root, served.replace(/^\//, ''));
    if (!existsSync(local)) continue;
    hashes[served] = `sha256:${createHash('sha256').update(readFileSync(local)).digest('hex')}`;
  }
  return hashes;
}

/**
 * Decide whether the bytes the browser loaded belong to the claimed revision.
 *
 * The comparison is against hashes of RESPONSE BODIES the browser received, not
 * against a build directory on the machine running the test. That direction is
 * what makes this served-source evidence: a local `dist/` proves what someone
 * built, while a response body hash proves what the deployment handed to the
 * browser.
 *
 * A claim with no matching asset is a FAILURE rather than an unverified pass.
 * "Nothing contradicted the claim" is not the same as "the claim was checked",
 * and an evidence producer that cannot tell those apart is the problem.
 */
export function verifyServedAssets(
  claimedRevision: string,
  observed: Record<string, string>,
  manifestPath: string,
): ServedAssetEvidence {
  const evidence: ServedAssetEvidence = {
    claimed_revision: claimedRevision,
    method: manifestPath ? 'asset_manifest_match' : 'local_build_hash',
    observed_assets: observed,
    matched_assets: [],
    mismatched_assets: [],
    verified: false,
  };

  // With a receipt, the expected hashes come from the deployment that published
  // the bundle. Without one — mocked mode only — they are computed from the local
  // build directory being served. That is deliberately the WEAKER method and is
  // labelled as such: it shows the browser received the bytes this checkout
  // built, which is a real check against a stale or wrong bundle, but it says
  // nothing about anybody else's deployment. `verifyForLiveAcceptance` refuses
  // any capture verified this way, so the weaker method cannot be passed off as
  // live evidence.
  const expected: Record<string, string> = manifestPath
    ? readAssetManifest(manifestPath).assets
    : hashLocalBuild(Object.keys(observed));

  for (const [path, digest] of Object.entries(observed)) {
    const want = expected[path] ?? expected[path.replace(/^\//, '')] ?? expected[`/${path}`];
    if (!want) {
      evidence.mismatched_assets.push(path);
      continue;
    }
    if (want === digest) evidence.matched_assets.push(path);
    else evidence.mismatched_assets.push(path);
  }

  evidence.verified = evidence.matched_assets.length > 0 && evidence.mismatched_assets.length === 0;
  return evidence;
}

// ---------------------------------------------------------------------------
// Request observation
// ---------------------------------------------------------------------------

/** One request the browser made, with the timing the poll measurements need. */
export interface RequestObservation {
  url: string;
  method: string;
  /** Milliseconds since the run's epoch. Relative, so no wall clock leaks in. */
  at_ms: number;
}

/**
 * Redacted record of a request destination.
 *
 * The URL is kept because the destination IS the observation for the leak
 * checks, but query strings are dropped: a signed URL or a token passed as a
 * query parameter would otherwise be archived verbatim.
 */
export function redactUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return url.split('?')[0];
  }
}

/**
 * In-memory leak detection over request bodies.
 *
 * The requirement pulls in two directions: answering "did a pod address or
 * credential ever leave the browser?" means looking at bodies, but saving those
 * bodies would create the very exposure the check is about. So bodies are
 * examined as they stream past and only booleans plus safe metadata survive —
 * which request (by redacted path) matched, never what it contained.
 */
export class LeakWatch {
  podAddress = false;
  token = false;
  /** Redacted paths whose body matched a sentinel. Metadata, not content. */
  offendingPaths: string[] = [];
  /**
   * Session-shaped secrets supplied by the harness, e.g. a real access token.
   *
   * Readable so the pre-write check can search the serialized capture for the
   * same values this watch was comparing against — the two must agree, or the
   * producer could detect a leak into a request while archiving it in the output.
   */
  readonly extraSecrets: string[];

  constructor(extraSecrets: string[] = []) {
    this.extraSecrets = extraSecrets.filter((value) => typeof value === 'string' && value.length >= 8);
  }

  /** Inspect one request. Returns true when something leaked. */
  inspect(url: string, body: string): boolean {
    const haystack = `${url}\n${body}`;
    let leaked = false;
    if (haystack.includes(SENTINEL_POD_ADDRESS)) {
      this.podAddress = true;
      leaked = true;
    }
    if (haystack.includes(SENTINEL_POD_TOKEN)) {
      this.token = true;
      leaked = true;
    }
    for (const secret of this.extraSecrets) {
      if (body.includes(secret)) {
        // A real session token in a request body is a token leak for this
        // purpose: the check is about credentials reaching places they should
        // not, and the sentinel is only a stand-in for one.
        this.token = true;
        leaked = true;
      }
    }
    if (leaked) {
      const path = redactUrl(url);
      if (!this.offendingPaths.includes(path)) this.offendingPaths.push(path);
    }
    return leaked;
  }
}

/**
 * Compute intervals between consecutive requests, in milliseconds.
 *
 * MEASURED, never the configured constant. `CONTROL_POLL_MS` in the component
 * says what the code intends; only differences between observed timestamps say
 * what the deployed bundle did, and the failures that matter here — a poll that
 * never stops, a retry that never backs off — are invisible to a source read.
 */
export function intervalsFrom(observations: RequestObservation[]): number[] {
  const times = observations.map((observation) => observation.at_ms).sort((a, b) => a - b);
  const intervals: number[] = [];
  for (let index = 1; index < times.length; index += 1) {
    intervals.push(Math.round(times[index] - times[index - 1]));
  }
  return intervals;
}

// ---------------------------------------------------------------------------
// Observation windows
// ---------------------------------------------------------------------------

/**
 * A bounded interval during which polling was expected to stop.
 *
 * The window carries its own start, end and the visibility the browser actually
 * reported, because "zero polls" is only meaningful against a real interval. A
 * window of zero duration observed nothing at all, so it is INCOMPLETE rather
 * than a `false` — which is exactly the substitution that would let a producer
 * report perfect polling hygiene without ever having waited.
 */
export interface PollWindow {
  label: string;
  started_at_ms: number;
  ended_at_ms: number;
  duration_ms: number;
  /** `document.visibilityState` sampled inside the window, in order. */
  observed_visibility: string[];
  /** Requests seen during the window. Empty is the expected result. */
  requests: RequestObservation[];
  /** How the condition was produced, so a simulation is never read as a tab switch. */
  mechanism: string;
}

export function summarizeWindow(window: PollWindow): { polled: boolean } {
  if (window.duration_ms <= 0) {
    throw new CaptureIncompleteError(
      `the '${window.label}' observation window has a duration of ${window.duration_ms}ms, so it ` +
        'observed nothing. A zero-duration window is an incomplete measurement, not a false: reporting ' +
        '"no polling" from an interval that never elapsed would be a passing default.',
    );
  }
  return { polled: window.requests.length > 0 };
}

// ---------------------------------------------------------------------------
// Fragments
// ---------------------------------------------------------------------------

/**
 * One scenario's contribution: the measured fields plus the raw observations
 * that support them.
 *
 * `raw` is retained deliberately. A derived boolean with nothing behind it is a
 * claim, and the point of this artifact is that its claims can be checked — so
 * the redacted destinations, the window bounds and the request timings that
 * produced each field travel with it. What is NOT retained: session tokens and
 * full unfiltered request bodies.
 */
export interface Fragment {
  scenario: ScenarioName;
  /** Measured keys this scenario is responsible for. */
  measured: Record<string, unknown>;
  raw: Record<string, unknown>;
}

export function writeFragment(captureDir: string, fragment: Fragment): void {
  const dir = join(captureDir, OBSERVATION_SUBDIR);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `${fragment.scenario}.json`), `${JSON.stringify(fragment, null, 2)}\n`, 'utf8');
}

function readFragments(captureDir: string): Fragment[] {
  const dir = join(captureDir, OBSERVATION_SUBDIR);
  if (!existsSync(dir) || !statSync(dir).isDirectory()) return [];
  return readdirSync(dir)
    .filter((name) => name.endsWith('.json'))
    .map((name) => JSON.parse(readFileSync(join(dir, name), 'utf8')) as Fragment);
}

// ---------------------------------------------------------------------------
// Merge, validate, write
// ---------------------------------------------------------------------------

export interface CaptureMeta {
  mode: 'mocked' | 'live';
  gateway_url: string;
  bundle_revision: string;
  served_assets: ServedAssetEvidence;
  /** Fixture run and worker generation the observations are bound to. */
  run_id: string;
  abort_run_id: string;
  observed_generations: number[];
  /** Conditions this run produced on purpose, so they are never read as faults. */
  injected_conditions: string[];
  captured_at: string;
  spec_digest: string;
  scenarios_executed: ScenarioName[];
}

/**
 * Structural validation of a merged capture, independent of the evaluator.
 *
 * This is not a second copy of the evaluator's predicates and must not become
 * one: the evaluator decides whether the MEASUREMENTS are acceptable, while this
 * decides whether a measurement was taken at all. The distinction is the whole
 * design — a producer that filled a gap with `false` would sail through the
 * evaluator, because `false` is what several checks want to see.
 */
export function validateComplete(capture: Record<string, unknown>, meta: CaptureMeta): void {
  const missingScenarios = REQUIRED_SCENARIOS.filter(
    (name) => !meta.scenarios_executed.includes(name),
  );
  if (missingScenarios.length) {
    throw new CaptureIncompleteError(
      `scenario(s) ${missingScenarios.join(', ')} produced no observations, so the capture cannot ` +
        'describe the behaviour they cover. A missing scenario is an incomplete run, never a passing ' +
        'one: the alternative is an artifact whose silence reads as coverage.',
    );
  }

  const missingKeys = REQUIRED_CAPTURE_KEYS.filter((key) => !(key in capture));
  if (missingKeys.length) {
    throw new CaptureIncompleteError(
      `the merged capture is missing required key(s) ${missingKeys.join(', ')}. The evaluator rejects ` +
        'an incomplete artifact wholesale, and filling the gap with a default here would be the exact ' +
        'substitution this producer exists to prevent.',
    );
  }

  // Counted observations: an integer is required where the evaluator distinguishes
  // "none" from "not measured", and a boolean would silently satisfy `!== 0`.
  for (const key of ['flag_off', 'flag_loading', 'flag_error']) {
    const observation = capture[key] as Record<string, unknown> | undefined;
    for (const field of ['control_nodes', 'command_requests']) {
      const value = observation?.[field];
      if (typeof value !== 'number' || !Number.isInteger(value)) {
        throw new CaptureIncompleteError(
          `${key}.${field} is ${JSON.stringify(value)}, not a counted integer. A count that was never ` +
            'taken cannot be reported as zero.',
        );
      }
    }
  }

  // Window-backed booleans: each must trace to a window that actually elapsed.
  for (const key of ['polled_while_hidden', 'polled_after_close', 'polled_after_terminal']) {
    if (typeof capture[key] !== 'boolean') {
      throw new CaptureIncompleteError(
        `${key} is ${JSON.stringify(capture[key])}, not an observed boolean`,
      );
    }
    const windows = (capture.observation_windows as PollWindow[] | undefined) ?? [];
    const window = windows.find((entry) => entry.label === key);
    if (!window) {
      throw new CaptureIncompleteError(
        `${key} is reported but no '${key}' observation window is recorded. Without the window's start, ` +
          'end and observed visibility, "no polling" is an assertion rather than a measurement.',
      );
    }
    summarizeWindow(window);
  }

  const intervals = capture.poll_intervals_ms;
  if (!Array.isArray(intervals) || intervals.length < 2) {
    throw new CaptureIncompleteError(
      `poll_intervals_ms is ${JSON.stringify(intervals)}; at least two measured intervals are required. ` +
        'One timestamp is not an interval.',
    );
  }
  const backoff = capture.backoff_intervals_ms;
  if (!Array.isArray(backoff) || backoff.length < 2) {
    throw new CaptureIncompleteError(
      `backoff_intervals_ms is ${JSON.stringify(backoff)}; at least two intervals measured while the ` +
        'endpoint was failing are required.',
    );
  }

  if (!Array.isArray(capture.phase_sequence) || !(capture.phase_sequence as unknown[]).length) {
    throw new CaptureIncompleteError('phase_sequence is empty: no rendered phase was observed');
  }

  // Identity binding: every observation must belong to the run this capture names.
  const boundRun = String(meta.run_id || '').trim();
  if (!boundRun) {
    throw new CaptureIncompleteError(
      'the capture names no fixture run, so its observations are not bound to a subject',
    );
  }
  if (meta.observed_generations.length === 0) {
    throw new CaptureIncompleteError(
      'no worker generation was observed, so the observations cannot be bound to one attempt of the run',
    );
  }

  if (!meta.served_assets.verified) {
    throw new CaptureIncompleteError(
      `the served bundle was not shown to match claimed revision ${meta.bundle_revision}: ` +
        `${meta.served_assets.matched_assets.length} asset(s) matched, ` +
        `${meta.served_assets.mismatched_assets.length} disagreed ` +
        `(${meta.served_assets.mismatched_assets.join(', ') || 'none'}). An environment SHA on its own ` +
        'is a claim, not served-source evidence.',
    );
  }
}

/**
 * Merge the fragments, validate, and write the artifact.
 *
 * Returns the path written. On an incomplete run this writes
 * `browser_control_run.partial.json` and rethrows, so the caller can exit
 * nonzero: the diagnostic is preserved for the operator, but under a name no
 * consumer is configured to read.
 */
export function finalizeCapture(captureDir: string, meta: CaptureMeta): string {
  const fragments = readFragments(captureDir);
  const merged: Record<string, unknown> = {};
  const raw: Record<string, unknown> = {};

  for (const fragment of fragments) {
    for (const [key, value] of Object.entries(fragment.measured)) {
      if (key in merged) {
        throw new CaptureIncompleteError(
          `two scenarios both measured '${key}' (second was ${fragment.scenario}). Each measurement has ` +
            'one owning scenario so that a later write cannot quietly overwrite an earlier observation.',
        );
      }
      merged[key] = value;
    }
    raw[fragment.scenario] = fragment.raw;
  }

  const executed = fragments.map((fragment) => fragment.scenario);
  const fullMeta: CaptureMeta = { ...meta, scenarios_executed: executed };

  const capture: Record<string, unknown> = {
    ...merged,
    // Provenance last so it cannot be shadowed by a scenario's measured key.
    mode: fullMeta.mode,
    gateway_url: fullMeta.gateway_url,
    bundle_revision: fullMeta.bundle_revision,
    captured_at: fullMeta.captured_at,
    spec_digest: fullMeta.spec_digest,
    run_id: fullMeta.run_id,
    abort_run_id: fullMeta.abort_run_id,
    observed_generations: fullMeta.observed_generations,
    served_assets: fullMeta.served_assets,
    injected_conditions: fullMeta.injected_conditions,
    scenarios_executed: executed,
    raw_observations: raw,
  };
  // Windows live at the top level so validateComplete can tie each boolean to one.
  capture.observation_windows = fragments.flatMap(
    (fragment) => ((fragment.raw.observation_windows as PollWindow[] | undefined) ?? []),
  );

  mkdirSync(captureDir, { recursive: true });
  // Apply the same privacy guard to partial diagnostics as complete captures.
  assertNoSecrets(capture);
  try {
    validateComplete(capture, fullMeta);
  } catch (error) {
    const partial = join(captureDir, PARTIAL_FILENAME);
    writeFileSync(
      partial,
      `${JSON.stringify(
        { incomplete_reason: String((error as Error).message), ...capture },
        null,
        2,
      )}\n`,
      'utf8',
    );
    throw error;
  }

  assertNoSecrets(capture);

  const path = join(captureDir, CAPTURE_FILENAME);
  writeFileSync(path, `${JSON.stringify(capture, null, 2)}\n`, 'utf8');
  return path;
}

/**
 * Last line of defence before anything is written to disk.
 *
 * The leak checks must not be answerable by archiving the thing they look for,
 * so the serialized capture is searched for the sentinels and for any real
 * session material the harness was given. This runs on the SERIALIZED form
 * because that is what lands on disk — a nested field that survived redaction
 * would be invisible to a field-by-field check.
 */
export function assertNoSecrets(capture: unknown, extraSecrets: string[] = []): void {
  const serialized = JSON.stringify(capture);
  const banned: Array<[string, string]> = [
    [SENTINEL_POD_TOKEN, 'the pod credential sentinel'],
    [SENTINEL_POD_ADDRESS, 'the pod address sentinel'],
  ];
  for (const secret of extraSecrets) {
    if (secret && secret.length >= 8) banned.push([secret, 'a supplied session secret']);
  }
  for (const [needle, label] of banned) {
    if (serialized.includes(needle)) {
      throw new CaptureIncompleteError(
        `refusing to write the capture: it contains ${label}. The privacy requirement is that the leak ` +
          'checks store booleans and safe metadata only — an artifact that archives the credential it ' +
          'was looking for has created the exposure it was meant to detect.',
      );
    }
  }
}

/**
 * Gate for the live-acceptance handoff.
 *
 * A mocked capture is real evidence about a real bundle with a stubbed gateway;
 * it is not evidence that a worker paused. Feeding one to the live path must be
 * refused with a specific reason rather than accepted as "the checks passed".
 */
export function verifyForLiveAcceptance(capture: Record<string, unknown>): void {
  if (capture.mode !== 'live') {
    throw new CaptureInputError(
      `this capture was produced in ${String(capture.mode)} mode and cannot serve as live acceptance ` +
        'evidence. Mocked runs fulfil the control endpoints in the browser, so their command ' +
        'successes and authorization refusals are the mock\'s answers, not a gateway\'s.',
    );
  }
  const assets = capture.served_assets as ServedAssetEvidence | undefined;
  if (!assets?.verified || assets.method !== 'asset_manifest_match') {
    throw new CaptureInputError(
      'live acceptance requires the served bundle to be matched against a deployment receipt ' +
        '(asset path -> sha256). A local build hash describes the machine that ran the test.',
    );
  }
  const injected = (capture.injected_conditions as string[] | undefined) ?? [];
  for (const condition of injected) {
    if (condition.startsWith('command_response:')) {
      throw new CaptureInputError(
        `live acceptance refuses injected condition '${condition}': control success and authorization ` +
          'must come from actual gateway and worker responses, never from route fulfillment.',
      );
    }
  }
  validateComplete(capture, {
    mode: 'live', gateway_url: String(capture.gateway_url || ''),
    bundle_revision: String(capture.bundle_revision || ''), served_assets: assets,
    run_id: String(capture.run_id || ''), abort_run_id: String(capture.abort_run_id || ''),
    observed_generations: (capture.observed_generations as number[]) ?? [],
    injected_conditions: injected, captured_at: String(capture.captured_at || ''),
    spec_digest: String(capture.spec_digest || ''),
    scenarios_executed: (capture.scenarios_executed as ScenarioName[]) ?? [],
  });
}

// ---------------------------------------------------------------------------
// Browser visibility
// ---------------------------------------------------------------------------

/**
 * Install a page-level control over document visibility.
 *
 * Why this mechanism, documented because the issue rightly forbids the lazy one:
 * changing a React state variable is not observing browser visibility, and two
 * approaches that would be more faithful do not work in headless Chromium. A
 * second tab brought to the front leaves the backgrounded page still reporting
 * `visible` (verified), and `Emulation.setPageVisibility` does not exist in
 * current Chromium builds (verified — the command is rejected as not found).
 * `Page.setWebLifecycleState` accepts only `frozen`/`active` and moves neither
 * `document.visibilityState` nor `document.hidden`.
 *
 * So visibility is overridden at the document level and the SAME
 * `visibilitychange` event the browser fires is dispatched. What makes this a
 * browser-level mechanism rather than an application fake: the override replaces
 * the platform properties (`document.visibilityState`, `document.hidden`) and the
 * event travels through the normal DOM listener path, so `useDocumentVisible` in
 * ControlPanel.tsx — which listens for that event and reads that property —
 * receives a signal it cannot distinguish from a real tab switch. The scenario
 * touches no application state.
 *
 * What it does NOT simulate: the browser's own throttling of timers in
 * background tabs. The claim under test is the app's polling decision, not
 * Chromium's scheduler, so this is the right seam — but the capture records the
 * mechanism so a reader never has to assume it was a real tab switch.
 */
export async function installVisibilityControl(page: import('@playwright/test').Page): Promise<void> {
  await page.addInitScript(() => {
    let hidden = false;
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => (hidden ? 'hidden' : 'visible'),
    });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
    (window as unknown as Record<string, unknown>).__controlE2ESetHidden = (value: boolean) => {
      hidden = value;
      document.dispatchEvent(new Event('visibilitychange'));
    };
  });
}

export const VISIBILITY_MECHANISM =
  'document.visibilityState/hidden overridden at the page level via addInitScript, with the platform ' +
  "visibilitychange event dispatched; the app's own visibility hook consumes it unmodified. Chromium's " +
  'Emulation.setPageVisibility is unavailable and bringToFront does not background a headless page.';

/** Drive the override. Fails loudly if it was never installed. */
export async function setDocumentHidden(
  page: import('@playwright/test').Page,
  hidden: boolean,
): Promise<string> {
  const applied = await page.evaluate((value) => {
    const setter = (window as unknown as Record<string, unknown>).__controlE2ESetHidden as
      | ((v: boolean) => void)
      | undefined;
    if (typeof setter !== 'function') return null;
    setter(value);
    return document.visibilityState;
  }, hidden);
  if (applied === null) {
    throw new CaptureIncompleteError(
      'the visibility control was not installed on this page, so the hidden-tab window would measure ' +
        'nothing. installVisibilityControl must run before the first navigation.',
    );
  }
  return applied;
}

// ---------------------------------------------------------------------------
// Per-scenario recorder
// ---------------------------------------------------------------------------

/** Assets whose bytes identify the served bundle. */
const ASSET_PATTERN = /\.(?:js|css)(?:\?|$)|\/index\.html$|\/$/;

/**
 * Map the phase label the panel rendered onto the state name the gateway uses.
 *
 * The panel's labels are operator-facing copy ("Pause requested — not yet paused")
 * and the evaluator's predicate matches state names (`pause_requested`). Something
 * has to translate, and it belongs here rather than in the evaluator: the copy is
 * allowed to be reworded for humans, whereas the state vocabulary is the contract.
 *
 * The mapping is deliberately anchored on the leading words of each label rather
 * than on a substring search, so that "Pause requested — not yet paused" cannot be
 * read as `paused` just because the word appears later in the sentence. Getting
 * that backwards would manufacture the exact false `paused` reading W4-04 exists
 * to catch.
 *
 * An unrecognised label returns '' and is not recorded, because inventing a state
 * name for copy nobody has mapped would put a guess into the evidence.
 */
export function canonicalPhase(label: string): string {
  const text = label.trim().toLowerCase();
  if (/^running\b/.test(text)) return 'running';
  if (/^pause requested\b/.test(text)) return 'pause_requested';
  if (/^paused\b/.test(text)) return 'paused';
  if (/^abort requested\b/.test(text)) return 'abort_requested';
  if (/^finished\b/.test(text)) return 'terminal';
  if (/^controls unavailable\b/.test(text)) return 'unavailable';
  return '';
}

/**
 * Map the command-journal sentence the panel rendered onto its status name.
 *
 * Same division as `canonicalPhase`: the copy is deliberately hedged prose
 * ("Handed to the agent — not a confirmation the agent has acted on it") and the
 * evaluator matches status names (`delivered`). The anchors are the leading words,
 * and the `delivered` label is matched on "handed to the agent" specifically
 * because that phrase is the panel's whole point — it distinguishes handing a
 * command over from the agent having acted on it, and collapsing the two is the
 * overclaim W4-08's pending-before-delivered ordering check guards against.
 */
export function canonicalCommandStatus(label: string): string {
  const text = label.trim().toLowerCase();
  if (/^queued\b/.test(text)) return 'pending';
  if (/^handed to the agent\b/.test(text)) return 'delivered';
  if (/^applied\b/.test(text)) return 'applied';
  if (/^cancelled\b/.test(text)) return 'cancelled';
  if (/^rejected\b/.test(text)) return 'rejected';
  if (/^unknown\b/.test(text)) return 'unknown';
  return '';
}

/**
 * Collects one scenario's observations from a live page.
 *
 * Everything here is an observation a browser can make and a source file cannot:
 * the destinations actually requested, the bytes actually served, the timestamps
 * actually separating two polls. The recorder never consults the component's
 * configuration.
 */
export class ScenarioRecorder {
  readonly scenario: ScenarioName;
  readonly requests: RequestObservation[] = [];
  readonly destinations: string[] = [];
  readonly leaks: LeakWatch;
  readonly observedAssets: Record<string, string> = {};
  readonly observedGenerations: number[] = [];
  readonly injectedConditions: string[] = [];
  readonly windows: PollWindow[] = [];
  readonly measured: Record<string, unknown> = {};
  /** Capabilities the gateway advertised, per verb. Server-side observation. */
  readonly advertisedCapabilities: Record<string, boolean> = {};
  /** Run states seen in polled responses, in order, including repeats. */
  readonly observedStates: string[] = [];
  /** Phases the DOM displayed, consecutive repeats collapsed. */
  readonly phaseSequence: string[] = [];
  /**
   * Explanatory copy seen while the run was in `pause_requested`.
   *
   * Latched during sampling because the phase — and therefore its copy — is
   * transient: by the time the run has settled to `paused` the sentence about
   * spend is gone. Reading it after the fact would find nothing and report a
   * missing warning that was in fact shown.
   */
  readonly pauseRequestedCopy: string[] = [];
  /** Command-journal statuses the DOM displayed, consecutive repeats collapsed. */
  readonly commandStatuses: string[] = [];
  private readonly epoch: number;
  private readonly pendingAssets: Array<Promise<void>> = [];

  constructor(scenario: ScenarioName, sessionSecrets: string[] = []) {
    this.scenario = scenario;
    this.leaks = new LeakWatch(sessionSecrets);
    this.epoch = Date.now();
  }

  /**
   * Milliseconds since this recorder was created.
   *
   * Public because scenarios need to mark a boundary — "nothing was submitted
   * after this moment" — and that boundary has to be on the same clock as the
   * request observations it will be compared against.
   */
  now(): number {
    return Date.now() - this.epoch;
  }

  /** Wire the listeners. Call before the first navigation. */
  attach(page: import('@playwright/test').Page): void {
    page.on('request', (request) => {
      const url = request.url();
      const observation: RequestObservation = {
        url: redactUrl(url),
        method: request.method(),
        at_ms: this.now(),
      };
      this.requests.push(observation);
      if (!this.destinations.includes(observation.url)) this.destinations.push(observation.url);
      // Bodies are inspected in memory and never retained.
      this.leaks.inspect(url, request.postData() || '');
    });

    page.on('response', (response) => {
      const url = response.url();
      if (!ASSET_PATTERN.test(url)) return;
      // Hash the bytes the DEPLOYMENT served, which is what ties the capture to
      // a revision. A local build directory would only describe this machine.
      this.pendingAssets.push(
        response
          .body()
          .then((body) => {
            const path = new URL(url).pathname;
            this.observedAssets[path] = `sha256:${createHash('sha256').update(body).digest('hex')}`;
          })
          .catch(() => {
            /* A body that cannot be read is simply not evidence; absence is handled by verify. */
          }),
      );
    });

    // Worker generation and advertised capabilities, from the polled state
    // responses. Separate listener because ASSET_PATTERN deliberately does not
    // match the state endpoint: binding observations to one attempt of the run is
    // provenance, not an asset.
    //
    // Capabilities are taken from what the SERVER advertised, never from what the
    // UI drew. The evaluator's W4-02 predicate subtracts the offered controls from
    // the allowed ones, and that subtraction only means anything if the two sides
    // come from independent observations: the response body here, the rendered
    // buttons in the DOM. Reading both off the same source would make the check
    // tautological.
    page.on('response', (response) => {
      if (!/\/agent\/state(?:\?|$)/.test(response.url())) return;
      this.pendingAssets.push(
        response
          .json()
          .then((body: { generation?: unknown; capabilities?: unknown; state?: unknown }) => {
            if (typeof body?.generation === 'number' && !this.observedGenerations.includes(body.generation)) {
              this.observedGenerations.push(body.generation);
            }
            if (body?.capabilities && typeof body.capabilities === 'object') {
              for (const [verb, allowed] of Object.entries(body.capabilities as Record<string, unknown>)) {
                // Last response wins: capabilities can legitimately change as the
                // run moves through its phases (a terminal run advertises none),
                // and the capture should describe the state the operator faced.
                this.advertisedCapabilities[verb] = allowed === true;
              }
            }
            if (typeof body?.state === 'string') this.observedStates.push(body.state);
          })
          .catch(() => undefined),
      );
    });
  }

  /**
   * Sample the phase the panel is displaying, de-duplicating consecutive repeats.
   *
   * The sequence is built from what the DOM showed over time rather than from the
   * mock's transition table, so an implementation that skipped straight from
   * `running` to `paused` without ever reporting `pause_requested` would produce a
   * sequence missing that element — which is exactly what W4-04 looks for.
   */
  recordPhase(phase: string): void {
    const normalized = canonicalPhase(phase);
    if (!normalized) return;
    if (this.phaseSequence[this.phaseSequence.length - 1] === normalized) return;
    this.phaseSequence.push(normalized);
  }

  /** Record a command-journal status the DOM displayed, in order. */
  recordCommandStatus(status: string): void {
    const normalized = canonicalCommandStatus(status);
    if (!normalized) return;
    if (this.commandStatuses[this.commandStatuses.length - 1] === normalized) return;
    this.commandStatuses.push(normalized);
  }

  /** Record a condition this run produced deliberately. */
  inject(condition: string): void {
    if (!this.injectedConditions.includes(condition)) this.injectedConditions.push(condition);
  }

  /** Requests to the polled control-state endpoint, optionally bounded in time. */
  stateRequests(fromMs = 0, toMs = Number.POSITIVE_INFINITY): RequestObservation[] {
    return this.requests.filter(
      (request) =>
        /\/agent\/state$/.test(request.url) && request.at_ms >= fromMs && request.at_ms <= toMs,
    );
  }

  /** Requests that submitted a command. */
  commandRequests(fromMs = 0): RequestObservation[] {
    return this.requests.filter(
      (request) =>
        request.method === 'POST' &&
        /\/agent\/(?:pause|resume|steer|abort)$/.test(request.url) &&
        request.at_ms >= fromMs,
    );
  }

  /**
   * Observe a bounded interval during which polling is expected to stop.
   *
   * The window samples visibility while it is open and records its own bounds, so
   * the resulting boolean is tied to an interval that demonstrably elapsed. A
   * caller cannot obtain a `false` without waiting.
   */
  async observeWindow(
    page: import('@playwright/test').Page,
    label: string,
    mechanism: string,
    durationMs: number,
    samples = 3,
  ): Promise<PollWindow> {
    const started = this.now();
    const visibility: string[] = [];
    const step = Math.max(1, Math.floor(durationMs / Math.max(1, samples)));
    for (let index = 0; index < samples; index += 1) {
      await page.waitForTimeout(step);
      visibility.push(await page.evaluate(() => document.visibilityState));
    }
    const ended = this.now();
    const window: PollWindow = {
      label,
      started_at_ms: started,
      ended_at_ms: ended,
      duration_ms: ended - started,
      observed_visibility: visibility,
      requests: this.stateRequests(started, ended),
      mechanism,
    };
    this.windows.push(window);
    return window;
  }

  /**
   * Watch the panel's phase and command-journal statuses in the background.
   *
   * W4-04 needs the ORDER in which phases appeared, and an intermediate phase like
   * `pause_requested` is transient — by the time an assertion has waited for
   * `paused`, it is gone. Sampling on an interval is how a browser observes a
   * sequence rather than an end state. Returns a stop function; the interval is
   * cleared there so a scenario cannot leave it running past the page's lifetime.
   */
  watchPhases(page: import('@playwright/test').Page, everyMs = 120): () => void {
    let stopped = false;
    const tick = async (): Promise<void> => {
      while (!stopped) {
        try {
          const phase = await page.getByTestId('control-phase').first().innerText({ timeout: 1_000 });
          this.recordPhase(phase);
          // Latch the pause explanation while the phase that carries it is on
          // screen. See the field comment: this copy does not survive the run
          // settling to `paused`.
          if (canonicalPhase(phase) === 'pause_requested') {
            const panel = page.getByRole('region', { name: /live run controls/i }).first();
            const copy = await panel.innerText({ timeout: 1_000 });
            if (!this.pauseRequestedCopy.includes(copy)) this.pauseRequestedCopy.push(copy);
          }
        } catch {
          // The panel may not be mounted yet, or may have withdrawn itself on a
          // terminal run. Neither is an observation; keep sampling.
        }
        try {
          const statuses = await page
            .getByTestId('command-journal')
            .locator('[data-testid^="command-status-"]')
            .allInnerTexts();
          for (const status of statuses) this.recordCommandStatus(status);
        } catch {
          /* No journal yet. */
        }
        if (stopped) return;
        await page.waitForTimeout(everyMs).catch(() => {
          stopped = true;
        });
      }
    };
    void tick();
    return () => {
      stopped = true;
    };
  }

  /** Finish and write this scenario's fragment. */
  async finish(captureDir: string): Promise<Fragment> {
    await Promise.all(this.pendingAssets);
    const fragment: Fragment = {
      scenario: this.scenario,
      measured: this.measured,
      raw: {
        // Redacted destinations, timings and window bounds: the support for every
        // derived field above. No bodies, no tokens.
        request_observations: this.requests,
        observation_windows: this.windows,
        observed_assets: this.observedAssets,
        observed_generations: this.observedGenerations,
        injected_conditions: this.injectedConditions,
        leak_offending_paths: this.leaks.offendingPaths,
        visibility_mechanism: VISIBILITY_MECHANISM,
        observed_states: this.observedStates,
        sampled_phase_sequence: this.phaseSequence,
        sampled_command_statuses: this.commandStatuses,
      },
    };
    assertNoSecrets(fragment, this.leaks.extraSecrets);
    writeFragment(captureDir, fragment);
    return fragment;
  }
}
