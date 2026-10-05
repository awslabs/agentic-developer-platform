# Regression testing

Public examples and historical evidence have sanitized deployment identities.
See [publishing documentation](../PUBLISHING.md); obtain real targets from private configuration.

Start here to run ADP's CLI regression tests, choose coverage, configure a target,
and interpret the result.

## Choose a workflow

| Workflow | Purpose | Target and execution |
| --- | --- | --- |
| [CLI Uplift](../../.github/workflows/eval-cli-uplift.yml) | Run a selected CLI suite against an already-deployed revision. | Select a configured, protected GitHub environment. Product commands run on disposable EC2. |
| [Nightly CLI Regression](../../.github/workflows/nightly-cli-regression.yml) | Coordinate onboarding/agent conversations, budget/rate-limit enforcement, then CLI Uplift. | Runs daily at 05:00 UTC against platform dev; also supports manual dispatch. Uses clean EKS pods and disposable EC2. |

CLI Uplift is one of the suites called by the combined nightly. Running CLI
Uplift with `suites=nightly` does not run the other two suites.

## Choose CLI coverage

| Scope | Coverage |
| --- | --- |
| `login` | CLI installation and release-file hashes, native login, and token refresh. |
| `nightly` | Installation/login, story reads, hosted coding, and tenant isolation. Fixture-dependent cases remain blocked when their prerequisites are missing. |
| `full` | All registered acceptance cases; every required case and cleanup must pass. |

There are also targeted scopes, which can be combined with commas. See the
[CLI Uplift runbook](cli-uplift-evaluation.md#dispatch) for the complete scope list,
commands, limits, and fixture requirements. The executable scope and case definitions
live in [cases.py](../../tests/e2e/cli_uplift/cases.py).

A passing partial suite does not establish full acceptance. Failed, blocked, and
not-run cases are reported separately. Cleanup and recovery must also succeed.

## Run against your deployment

Follow [private target setup](cli-uplift-evaluation.md#private-target-configuration).
Set `EVAL_ENVIRONMENT` to a supported, configured GitHub environment and
`EVAL_REVISION` to the verified deployed commit. Keep the target's account,
endpoint, resource IDs, and fixture references in protected environment secrets.

```bash
gh workflow run eval-cli-uplift.yml --repo aws-e/adp --ref main \
  -f environment="$EVAL_ENVIRONMENT" \
  -f expected_revision="$EVAL_REVISION" \
  -f mode=start -f suites=login
```

Use `suites=nightly` for broader coverage after reviewing its prerequisites. These
evaluations test the deployed release; they do not upgrade the platform.

## Guides and evidence

- [CLI Uplift evaluation](cli-uplift-evaluation.md): configuration, scopes, dispatch, reports, resume, and cleanup.
- [Combined nightly regression](nightly-cli-regression.md): scheduling, execution order, coverage, and combined verdict.
- [Destination-account roles](cli-uplift-destination-roles.md): cross-account routing/provisioning fixtures.
- [GitHub fixtures](cli-uplift-github-fixtures.md): isolated App and repository setup for GitHub scenarios.
- [Role lifecycle evaluation](cli-uplift-role-lifecycle.md): recorded findings and remaining acceptance work.
- [IAM artifacts and evaluation evidence](cli-uplift/README.md): reviewed policy examples, deployment receipts, and historical run records. Account-specific examples require review before reuse.
