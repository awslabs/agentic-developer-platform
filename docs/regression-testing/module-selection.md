# Run regression tests by module

In **Actions → ADP Regression → Run workflow**, enter module IDs in `modules`,
for example `MOD-001,MOD-002`. You can also enter names separated by semicolons,
such as `Agent Models; Organizations`, or `all`. Blank input retains the existing
daily/weekly profile. Scheduled runs retain their existing coverage.

This interface is implemented on the regression coordinator branch. It becomes
available on the default branch after the coordinator changes are merged.

The [master register](master-test-coverage.md) lists the 48 audited modules,
133 features and 629 scenarios. The versioned [test registry](../../tests/regression/catalog.json)
stores those same IDs, each scenario's action and expected result, and the 99
confirmed mapped test definitions from the audit. It is a mapped minimum, not a
complete count of tests in the repository or proof of full E2E coverage.

## Test storage and tags

Executable tests stay beside the fixtures and helpers they need. The registry
references their exact source path and selector, rather than copying tests into
another directory. A definition can map to several scenarios and modules; it
runs once per selected lane. Parameter variants retain their own runtime results
while sharing a source definition ID.

| Item | Identity example | Stored in |
|---|---|---|
| Module | `MOD-001` | `tests/regression/catalog.json` |
| Feature | `MOD-001-F001` | Same registry, with parent module and name |
| Scenario | `MOD-001-F001-S001` | Same registry, with action, expected result and test references |
| Test definition | `TEST-087` | Same registry, with path, selector, kind and all three coverage tags |
| CLI journeys | Existing `E38`, `D03`, etc. | `tests/e2e/cli_uplift/remote/` |
| Browser tests | Pytest function/class selectors | `tests/e2e/chat/`, `tests/e2e/new_ui/` |
| Module API/local tests | Pytest function/class selectors | `modules/<module>/tests/` |
| Shell evaluations | Named case selectors | `platform/evals/` |

For example, `TEST-087` references CLI case `E38` in
`tests/e2e/cli_uplift/remote/story_reads.py`. Its tags include `MOD-001`,
`MOD-001-F001` and the mapped catalogue scenarios. This case performs the
catalogue reads described in the audit; its tag does not imply all persona and
browser states are covered.

The pytest tagging plugin attaches `adp_test`, `adp_module`, `adp_feature` and
`adp_scenario` markers during collection. JUnit includes the corresponding
`adp_test_id`, `adp_module_id`, `adp_feature_id` and `adp_scenario_id` properties.
The coordinated pytest suites also store an inventory `.tags.json` sidecar.
CLI reports include a `tags` object per case and the same JUnit properties.
Shell evaluation mappings are stored in the registry and run plan; their native
reports retain their existing format.

## What happens when a module run starts

1. Validate and normalize the module input. Unknown IDs fail before live work.
2. Store `module-plan.json` as the **module-test-plan** artifact. It lists selected
   tests, their tags, execution lanes and coverage gaps.
3. Check the deployed revision and run the shared gateway smoke prerequisite.
4. Execute the configured lanes. CLI runs use the exact mapped cases plus clean
   installation/login prerequisites (`E01`, `C01`) on disposable EC2. Browser
   suites filter collected tests by module and E2E kind. Onboarding and budget
   shell evaluations run their whole suite because their harnesses share setup
   and cleanup; the plan makes this broader scope visible.
5. Preserve existing protected environments, private fixture configuration,
   cleanup gates and shared live-test locks. Missing fixtures or a skipped
   selected lane cannot satisfy the execution verdict.
6. Check the deployed revision again and aggregate the selected lane results.
   A successful execution verdict covers the selected tests. The summary reports
   partial coverage and the plan retains unimplemented/unmapped scenarios.

The module coordinator currently supports mapped CLI, Chat, New UI, onboarding,
and budget/rate-limit lanes. Other stored definitions, including module-specific
Agent Context and Agent Factory E2E tests, local tests, checks, blocked cases and
placeholders, are explicitly listed as **outside the module coordinator**. A
selection containing only such definitions exits incomplete without live work.
`all` selects all configured lanes and reports the remaining gaps; it does not
claim all features have E2E tests. Daily/weekly profiles still run their broader
existing suite set when `modules` is blank.

## Preview selection locally

From the repository root, this command writes a plan without running tests or
accessing cloud services:

```bash
python3 .github/scripts/regression_modules.py --modules MOD-001,MOD-002
```

After installing the selected suite's documented dependencies, the shared pytest
plugin can filter a local suite and emit the same tags:

```bash
python -m pytest modules/gateway/tests/admin/persona_models/ \
  -p tests.regression.pytest_plugin --adp-modules MOD-001 --adp-kind LOCAL \
  --junitxml=test-results/agent-models.xml
```

This example runs local API tests. Live suites continue to require their own
protected target, credentials, feature flags and fixtures; tags supply selection,
not environment setup.

## Maintaining the registry

Keep IDs stable. Add new features and scenarios under their existing module;
assign a new `TEST-nnn` only for a new source definition. Record its exact path,
selector and kind (`E2E`, `LOCAL`, `CHECK`, `BLOCKED`, or `PLACEHOLDER`), then link
both the scenario's `test_ids` and the definition's module/feature/scenario tags.
Do not give a stub an implemented kind. Shared definitions reuse the same test ID.
Keep the human-readable master register aligned when changing scenario scope.

The regression contract job checks parent IDs, bidirectional mappings, duplicate
definitions, source paths and Python selectors. It also verifies selection,
report tags, zero-test handling and workflow cleanup guards. No live target is
needed for those checks.
