# Proposal validation record

This file records design-package checks, not implementation or live acceptance.
The source revision is `fc5fe6f21100df5d49c29f4c3a882907ff8e659e`; the reviewed
design revision is the commit containing this file in the design PR. Approval
must name that full commit explicitly rather than assume the branch tip stays
unchanged. Working directory: repository root (`/work/repo` in this review).

## Reproducible checks

The inventory/link checker requires Python 3 and Git with the source baseline
available. It checks tracked inventory equality, migration parent completeness,
local linked files/headings and source line bounds; it does not infer semantic
correctness from a valid link. Public-document screening is repository-provided.

```bash
python3 docs/design-notes/7030-gateway-performance/validate.py
python3 scripts/check-public-docs.py
git diff --check
```

The existing SSE regression uses Locust pinned by the repository and a loopback
HTTP server. Install only into an isolated temporary virtual environment:

```bash
python3 -m venv /tmp/issue-7030-perf-venv
/tmp/issue-7030-perf-venv/bin/pip install -r tests/performance/gateway/requirements.txt
/tmp/issue-7030-perf-venv/bin/python tests/performance/gateway/test_stream_validation.py
```

No target credentials or billed model calls are needed for this command.

## Results

### Re-review checkpoint — 2026-10-06

Tested revision: the commit containing this record and R1–R3 changes; the exact
full SHA is published in PR #7039 and the completion response after commit/push.
Source baseline remains unchanged. These results supersede package counts in the
original checkpoint below, not the issue's live acceptance requirements.

| Check | Observed result / limit |
|---|---|
| Full inventory and migrations | `validate.py` passes: all 8,818 baseline artifacts and 13 explicitly mapped design artifacts; all 88 migration parents still complete. It now fails on tracked-tree/package-ledger drift. No runtime inventory deferred to implementation. |
| Local links and acceptance IDs | 245 local file/heading/source-line links pass. Validator now asserts all 19 child and 5 epic AC IDs and order. Source semantics separately inspected for reservation fallback, person identity, gap scope, quote/transport waits and settlement writers. |
| Documentation helpers | `ruff check` and `ruff format --check` pass for `audit.py` and `validate.py`. Formatting needed one small correction before the passing check. |
| Staged publication screening | `python3 scripts/check-public-docs.py` checks 1,445 public-document files with zero findings after new artifacts are staged; `git diff --cached --check` passes. Only nine intended design-package files are staged. Manual review adds no target identities, credentials or private evidence. |
| Existing loopback SSE tests | 3 tests pass in 36.083s, Python 3.13.16 / Locust 2.46.7, isolated `/tmp/issue-7030-perf-venv`. Actual command: `/tmp/issue-7030-perf-venv/bin/python tests/performance/gateway/test_stream_validation.py`; dependencies installed with `uv pip install --python /tmp/issue-7030-perf-venv/bin/python -r tests/performance/gateway/requirements.txt`. No cloud requests. |
| Proposed fault coverage | F01–F14 are specified, **not executed**: no admission schema, dispatch adapter, reconciliation API or runtime change is implemented by this design PR. |
| Source and review refresh | Read the consolidated PR review and revision amendment, re-read all six story bodies, and checked PR/#7032 discussion for a decision: no accounting-policy answer found. Compared current main at the revision named in the audit; adjacent flow-meter changes are recorded rather than silently treated as durable admission. |

Reproduce the helper checks from repository root with Python 3, Git and Ruff:

```bash
python3 docs/design-notes/7030-gateway-performance/validate.py
ruff check docs/design-notes/7030-gateway-performance/audit.py docs/design-notes/7030-gateway-performance/validate.py
ruff format --check docs/design-notes/7030-gateway-performance/audit.py docs/design-notes/7030-gateway-performance/validate.py
python3 scripts/check-public-docs.py
git diff --cached --check
```

Public-document screening must run after staging new documents because it reads
the tracked index. Publication status is recorded in the PR at its exact commit.
Remote CI status is separate from these local checks.

### Original checkpoint — 2026-10-05

| Check | Observed result / limit |
|---|---|
| Reproducible inventory | Pass: all 8,818 baseline tracked artifacts mapped; 88 migrations, complete parent graph and one head. No live-schema verification. |
| Local source/document links | Pass: 171 file/heading/line links at this checkpoint. Source line bounds do not prove every design interpretation. |
| Python lint and formatting | `ruff check` passes for both design audit helpers; formatting applied with repository Ruff and rechecked before publication. |
| Existing loopback SSE suite | 3 tests passed in 36.256s using Python 3.13.16 / Locust 2.46.7 in the isolated virtual environment. No cloud requests. Dependencies installed with `uv pip install --python /tmp/issue-7030-perf-venv/bin/python -r tests/performance/gateway/requirements.txt`. |
| Public-document checker / staged whitespace | Pass: 1,442 tracked public-document files checked, zero findings; staged `git diff --cached --check` clean. Manual review retains only public source paths and sanitized supplied statistics. |
| Acceptance mapping | Pass: all 19 child acceptance rows and all 5 epic rows retain their original IDs in the draft story/campaign tables. No issue-body amendment made. |
| Issue/source comparison | Read #7030 amendment and #7031–#7036; inspected #6994 and related #4810/#964; no diff in affected serving/accounting/profile/harness trees since the campaign reference. |
| Official external references | PostgreSQL, S3 notification, HPA, Botocore and CloudWatch authorization pages retrieved successfully; references listed in audit. |

No live inference, deployment, database experiment,
fixture mutation, issue-body update, design approval or agent dispatch is part
of this work. The [audit gaps](audit.md#full-design-coverage-and-unresolved-gaps)
remain runtime implementation/qualification work regardless of local check results.
The final review below supersedes the earlier pending-D2 decision status.

## Final review checkpoint — 2026-10-06

Reviewed architect revision `829fa7a8d2ca2e7b78f8038d354aea0a03f35bfe` and made a
documentation-only conservative resolution of R2. The reviewed assistant-authored
AC-02 now distinguishes exact durable-receipt recovery from pre-receipt unknown
containment. No manual write-off or financial-release exception is included.
R1/R3 define implementation contracts whose actual runtime proofs remain future
acceptance work; no capacity or deployment claim follows from design approval.

- Inventory and link validator: 8,818 baseline plus 13 design artifacts; all
  19 child and five epic acceptance IDs retained; 245 local links verified.
- Public documentation checker: 1,445 files, zero findings.
- Existing loopback parser suite: three tests passed in 34.866 seconds during
  the review; parser/runtime source is unchanged by either design revision.
- Whitespace verification passed. No billed requests or infrastructure changes.

The final exact design commit and approval are recorded in PR #7039. Approved
issue-body updates are a subsequent handoff action with that immutable reference,
not evidence that any runtime acceptance row has passed.
