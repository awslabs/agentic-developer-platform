/**
 * Global setup for the browser evidence producer — Issue #5878.
 *
 * This exists to make invalid input fail BEFORE any browser command runs. A
 * producer that discovers its output directory is unusable after it has already
 * paused somebody's run has had a side effect it cannot take back, and in live
 * mode the commands are real. So every input is validated here, at the one point
 * in the run where nothing has happened yet.
 *
 * It also records the run's provenance for the teardown to merge, and writes it
 * to disk rather than passing it in memory because Playwright's setup and
 * teardown are separate module instances.
 */

import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

import { validateInputs, specDigest } from './agent-control-capture';

export const META_FILENAME = 'capture-meta.json';

export default function globalSetup(): void {
  // Throws CaptureInputError on any invalid or missing input. Playwright reports
  // the throw and runs no tests, which is the required "fail before commands".
  const inputs = validateInputs();

  mkdirSync(inputs.captureDir, { recursive: true });
  writeFileSync(
    join(inputs.captureDir, META_FILENAME),
    `${JSON.stringify(
      {
        mode: inputs.live ? 'live' : 'mocked',
        gateway_url: inputs.gatewayUrl,
        claimed_revision: inputs.claimedRevision,
        asset_manifest_path: inputs.assetManifestPath,
        run_id: inputs.runId,
        abort_run_id: inputs.abortRunId,
        spec_digest: specDigest(),
        // Recorded at setup so the artifact's timestamp is the run's start rather
        // than whenever the last scenario happened to finish.
        started_at: new Date().toISOString(),
      },
      null,
      2,
    )}\n`,
    'utf8',
  );
}
