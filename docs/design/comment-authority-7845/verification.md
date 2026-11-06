# Design verification record

Date: 2026-10-16 UTC. Working directory: repository root (`/work/repo` in the
design worker). Reviewed/tested production source:
`c958000f0829b1462c2f4ce23bfb743676b97306`. `main` was fetched again before
publication and had not advanced. The branch began with the same source tree.
Only this design directory is intended for the design commit.

## Checks performed

- Static audit reproduction: passed. Reconciled 10,472 source artifacts,
  2,259 build/deployment/script candidates, 285 comment-influence candidate files
  and 343 authority/lineage candidate files; zero unclassified tracked paths.
- Link checks: immutable GitHub source paths/line bounds and local document paths
  validated against the pinned source and design directory. Remote URL reachability
  and Markdown anchor rendering are not claimed by this checker.
- Issue contract: one snapshot acceptance table with AC-01 through AC-10,
  12 stable scenario rows in the backlog, body within 8 KiB. Native parent #7840
  verified with the GitHub API; original source had no numbered AC/scenario IDs.
- Existing permission/approval helper: **16 assertions passed**, using Node
  `v24.21.0` type stripping and mocked fetch only. Three accepted permission tiers,
  four denied tiers, non-OK and throwing lookups, valid approval/rejection and
  five invalid approval forms were checked. The command below reproduces the check.
  Node reported its existing typeless-package module warning; no package change
  was made to suppress it.
- Public-document scanner: passed, 1,548 tracked public documentation files,
  zero findings. Staged Git whitespace check: passed. Manual review found no
  copied private scan content, deployment identities or credential values.

These are checks of the design artifacts and existing reusable source, **not**
passes for the proposed security fix. No application changes were made. The
full Jest/Python suites, new scenarios, installed-package build, staging controls,
cloud state, effective deployed authority flags, private scanner findings and
live credentials were not tested. The workspace initially lacked Jest dependencies
and Python pytest/boto3; none were installed just to manufacture a design pass.

## Reproduction

Prerequisites: Git and Python 3, reviewed commit locally available; Node supporting
TypeScript type stripping (the worker used Node 24). Run at the repository root.
The fake fetch below prevents GitHub requests and uses only fictional fixture data.

```bash
python3 docs/design/comment-authority-7845/audit.py
python3 scripts/check-public-docs.py
git diff --check
```

```bash
node --experimental-strip-types --input-type=module <<'JS'
import assert from 'node:assert/strict';
import {hasRepositoryWritePermission, parsePlanApproval} from './modules/agent-factory/agent/src/utils/comment-authority.ts';
let cases = 0;
for (const permission of ['admin', 'maintain', 'write', 'read', 'triage', 'none', 'unknown']) {
  globalThis.fetch = async () => ({ok:true,json:async()=>({permission})});
  assert.equal(await hasRepositoryWritePermission('example-owner','example-repo','writer-example','fixture-token'), ['admin','maintain','write'].includes(permission));
  cases++;
}
for (const fetcher of [async()=>({ok:false}), async()=>{throw new Error('synthetic unavailable')}]) {
  globalThis.fetch = fetcher;
  assert.equal(await hasRepositoryWritePermission('example-owner','example-repo','writer-example','fixture-token'),false);
  cases++;
}
assert.deepEqual(parsePlanApproval('/approve request-example','request-example'), {approved:true,feedback:''}); cases++;
assert.deepEqual(parsePlanApproval('/reject request-example revise','request-example'), {approved:false,feedback:'revise'}); cases++;
for (const body of ['/approve', '/approve old-request', 'feedback only', '/approve request-example extra', 'quoted /approve request-example']) {
  assert.equal(parsePlanApproval(body,'request-example'),null); cases++;
}
console.log(`${cases} existing-helper assertions passed; mocked fetch only; no network`);
JS
```

Expected helper result: `16 existing-helper assertions passed; mocked fetch only;
no network`. Re-run against the pinned source if main changes. Do not mistake the
helper's correct behavior for coverage of callers that currently bypass it.
