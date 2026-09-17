# Platform Deploy Management

Deterministic phase-verification framework for ADP deployments.

## Purpose

Runs assertions against live AWS accounts to verify deployment phases completed correctly. Produces structured JSON evidence, stores it in S3, and tracks status in DynamoDB. Replaces trust-based narration with deterministic pass/fail.

## Module Layout

```
modules/platform-deploy-mgmt/
├── checks/
│   ├── __init__.py
│   ├── shape.py           # JSON evidence shapes, enums (Severity, CostClass, Result)
│   ├── boto_helpers.py    # Context dataclass, dual-session builder (customer reads, platform writes)
│   ├── runner.py          # Orchestrator: runs checks, uploads evidence, updates DDB
│   ├── phase_1.py         # Phase 1 checks (bootstrap state backend)
│   ├── phase_12.py        # Phase 12 checks (Superplane domain app — optional)
│   └── requirements.txt   # Python dependencies
├── infra/
│   ├── main.tf            # S3 bucket + DDB table + IRSA role (platform account)
│   ├── variables.tf
│   └── outputs.tf
├── tests/
│   ├── unit/              # Mocked tests (run in CI)
│   └── integration/       # Live tests (require real AWS credentials)
└── README.md
```

## How to Add a Check

1. Create the check function in the appropriate `phase_N.py` file:

```python
def check_N_M_description(ctx: Context) -> CheckResult:
    start = time.perf_counter_ns()
    # ... your boto3 read-only call against ctx.customer_session ...
    duration_ms = (time.perf_counter_ns() - start) // 1_000_000
    return CheckResult(
        id="N.M",
        name="Human-readable description",
        result=Result.PASS,  # or FAIL or SKIP
        severity=Severity.HARD,  # or SOFT
        duration_ms=duration_ms,
        detail="What happened",
        evidence={"key": "value"},
    )
```

2. Add it to the `CHECKS` list at the bottom of the file:

```python
CHECKS = [
    ...
    ("N.M", "Description", check_N_M_description, Severity.HARD, CostClass.CHEAP),
]
```

3. Write a unit test in `tests/unit/test_phase_N_checks.py` mocking the boto3 calls.

## How to Add a Phase

1. Create `checks/phase_N.py` with a `CHECKS` list following the same tuple format.
2. Register it in `checks/runner.py`:

```python
PHASE_REGISTRY: dict[int, tuple[str, str]] = {
    ...
    N: ("platform_deploy_mgmt.checks.phase_N", "Phase display name"),
}
```

3. Add the step in `.github/workflows/platform-deploy-mgmt-verify.yml` (replace the stub).
4. Write unit tests.

Phase numbers match the step numbering in `platform/scripts/deploy-all.sh`, so an operator
reading `Step 12/12: Superplane` in a deploy log knows which phase verifies it. Gaps in
`PHASE_REGISTRY` are expected while intermediate phases are unimplemented.

## Registered Phases

| Phase | Verifies | Deploy step | Notes |
|-------|----------|-------------|-------|
| 1 | Bootstrap state backend | Step 1 | |
| 12 | Superplane domain app | `Step 12/12: Superplane` | **Optional module.** Absence is a SKIP unless `SUPERPLANE_ENABLED` is set, so `phase: all` on a gateway-only account does not report a false failure. No workflow step yet — invoke as `python -m platform_deploy_mgmt.checks.runner --phase=12`. |

## Running Locally

```bash
cd modules/platform-deploy-mgmt
pip install -r checks/requirements.txt
pip install pytest

# Unit tests (no AWS required)
pytest tests/unit/ -v

# Integration tests (requires AWS credentials + target account)
INTEGRATION_TEST_ACCOUNT=443458828159 pytest tests/integration/ -v
```

## Architecture

### Dual-session contract

- **customer_session**: Read-only calls against the customer's AWS account (assumed via `load-deploy-config` action)
- **platform_session**: Writes evidence to S3 and status to DynamoDB (runner's own IRSA credentials)

This separation is enforced by unit tests and is critical for security.

### Evidence flow

```
Check execution → CheckResult list → PhaseEvidence JSON
    ↓                                        ↓
Console output                      S3: adp-platform-deploy-evidence/<account>/<mode>/phase-N/<ts>.json
    ↓                                        ↓
GitHub Step Summary                 DDB: adp-platform-deployments (deployment_id, phase)
```

## Workflows

| Workflow | Purpose |
|----------|---------|
| `platform-deploy-mgmt-verify.yml` | Run phase checks (dispatched per-phase or "all") |
| `platform-deploy-mgmt-infra-apply.yml` | Deploy the infrastructure (S3 + DDB + IRSA role) |


Phase 12's secret-grant audit reads both inline policies and attached managed
policies at their default versions, including all list pages. Its read-only
identity needs `iam:ListAttachedRolePolicies`, `iam:GetPolicy`, and
`iam:GetPolicyVersion` in addition to the existing role/inline-policy reads.
Unreadable or incomplete evidence fails verification. This is a conservative
allow-grant audit: it does not evaluate deny precedence, permission boundaries or
organization policies, and unsupported complement grants require investigation.
