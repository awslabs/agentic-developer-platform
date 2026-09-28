/**
 * Global teardown for the browser evidence producer — Issue #5878.
 *
 * The single place where a run is judged complete. Merges the scenario fragments,
 * verifies the served bundle against the claimed revision, checks that every
 * required measurement was actually taken, and only then writes
 * `browser_control_run.json`.
 *
 * Throwing here fails the whole run, which is the intent: an incomplete capture
 * must exit nonzero rather than leave a plausible artifact behind. The partial
 * diagnostic `finalizeCapture` writes on failure keeps the evidence for whoever
 * has to diagnose it, under a filename no consumer reads.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import {
  CaptureIncompleteError,
  finalizeCapture,
  verifyServedAssets,
  type CaptureMeta,
  type Fragment,
  type ServedAssetEvidence,
} from './agent-control-capture';
import { META_FILENAME } from './agent-control.setup';

export default function globalTeardown(): void {
  const captureDir = (process.env.CONTROL_E2E_CAPTURE_DIR || '').trim();
  if (!captureDir) return; // Setup would already have failed.

  const metaPath = join(captureDir, META_FILENAME);
  if (!existsSync(metaPath)) {
    throw new CaptureIncompleteError(
      `no ${META_FILENAME} in ${captureDir}: the run's provenance was never recorded, so any ` +
        'observations in this directory cannot be attributed to a deployment or a revision.',
    );
  }
  const meta = JSON.parse(readFileSync(metaPath, 'utf8'));

  // Gather what the browser actually loaded, across every scenario, and hold the
  // claimed revision to it.
  const fragments = readFragments(captureDir);
  const observedAssets: Record<string, string> = {};
  const generations: number[] = [];
  const injected: string[] = [];
  for (const fragment of fragments) {
    for (const [path, digest] of Object.entries(
      (fragment.raw.observed_assets as Record<string, string>) ?? {},
    )) {
      if (observedAssets[path] && observedAssets[path] !== digest) {
        throw new CaptureIncompleteError(`served asset changed between scenarios: ${path}`);
      }
      observedAssets[path] = digest;
    }
    for (const generation of ((fragment.raw.observed_generations as number[]) ?? [])) {
      if (!generations.includes(generation)) generations.push(generation);
    }
    for (const condition of ((fragment.raw.injected_conditions as string[]) ?? [])) {
      if (!injected.includes(condition)) injected.push(condition);
    }
  }

  const served: ServedAssetEvidence = verifyServedAssets(
    meta.claimed_revision,
    observedAssets,
    meta.asset_manifest_path || '',
  );

  const captureMeta: CaptureMeta = {
    mode: meta.mode,
    gateway_url: meta.gateway_url,
    bundle_revision: meta.claimed_revision,
    served_assets: served,
    run_id: meta.run_id,
    abort_run_id: meta.abort_run_id,
    observed_generations: generations,
    injected_conditions: injected,
    captured_at: meta.started_at,
    spec_digest: meta.spec_digest,
    scenarios_executed: [],
  };

  const path = finalizeCapture(captureDir, captureMeta);
  // The one line an operator needs in order to find the artifact they just made.
  console.log(`browser_control_run written: ${path} (mode=${captureMeta.mode})`);
}

function readFragments(captureDir: string): Fragment[] {
  const dir = join(captureDir, 'observations');
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .filter((name) => name.endsWith('.json'))
    .map((name) => JSON.parse(readFileSync(join(dir, name), 'utf8')) as Fragment);
}
