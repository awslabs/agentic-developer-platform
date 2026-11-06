# FinOps design verification and handoff

## Scope and reproduction

Working directory: repository root (`/work/repo` in the architecture run), branch
`agent/issue-9079`. Audited main revision:
`388584bdd80a4a5ff142abe3234e7d1eac1cfd64`. F1 is a documentation/design delivery,
not runtime implementation or live acceptance. The reviewed commit is linked from
the design PR and every child; an approval requires actual review evidence.

From a checkout of that design revision, use Python 3.12+ with PyYAML 6.x,
jsonschema 4.x, openapi-spec-validator 0.7.x and pytest. These are the existing
offline contract-check dependencies; use an isolated virtual environment if needed.
The audit additionally needs the pinned main Git object in the clone.

```sh
python3 docs/l5-services/finops/audit_inventory.py --check
python3 docs/l5-services/finops/validate_design.py
python3 docs/l5-services/contracts/v1/validate.py
python3 -m pytest docs/l5-services/contracts/v1/test_contracts.py -q
python3 scripts/check-public-docs.py
git diff --check
```

Inventory check expects exact equality with the committed pinned-tree tables.
Design validation checks draft schema/example validity, synthetic Decimal fixture
oracles, all seven story/eight EC mappings and local documentation links/anchors.
It is **not** an implementation of the native adapter or billing comparator.
Shared contract checks verify the unchanged common specification; they do not
establish that proposed FinOps routes or payload variants are deployed.

## Execution record

Executed offline checks on F1 in this working tree:
- Complete inventory regeneration matches all committed tables: pass.
- Draft design: two valid and five rejected schema examples, three synthetic Decimal
  oracles, seven stories/eight ECs and 66 local links/anchors: pass.
- Shared validator: 97 schemas, 31 operations, 11 schema fixtures, 20 specification
  scenarios, 176 local links and 21 tests: pass; supplemental public-data scan clean.
- Shared contract pytest: 21 passed, 17 subtests passed.
- All seven child bodies pass the optional draft-format check; this is not catalog,
  project-placement or implementation readiness proof.

The design PR records the tested immutable commit and final public-data/diff checks. Provider calls, AWS changes,
installed-client acceptance, worker deployment and Context/Insights reuse are not
run by this design assignment. The backlog keeps all live scenarios planned.

GitHub source inspection verifies the epic, amendments, parent, sibling epics and
open PR revisions listed in [audit.md](audit.md). Vendor documentation URLs returned
HTTP 200 during this run; that does not qualify service access, supported automation
or current pricing. Historical POC outcomes remain reported, not reproduced.

The seven native children are [FO01 #9083](https://github.com/aws-e/adp/issues/9083),
[FO02 #9084](https://github.com/aws-e/adp/issues/9084),
[FO03 #9085](https://github.com/aws-e/adp/issues/9085),
[FO04 #9086](https://github.com/aws-e/adp/issues/9086),
[FO05 #9087](https://github.com/aws-e/adp/issues/9087),
[FO06 #9088](https://github.com/aws-e/adp/issues/9088) and
[FO07 #9089](https://github.com/aws-e/adp/issues/9089). Native parent relationships
were read back through GitHub after creation. The optional issue format validator
checks six-section/AC/scenario structure only; it cannot verify project/catalog
membership, test existence, review approval or live behavior.

Final compatibility correction before handoff: FinOps retains the shared CLI exit
codes (including conflict 4, unavailable 5, terminal failure 6 and bounded wait 7),
MCP `assessment_ref`/ETag envelopes and binding `link` action. The initial F1 draft's
service-specific exit-code wording was wrong and is superseded by the PR head;
epic/children are pinned to the corrected canonical revision.

## Remaining handoff

- **Design approved:** actual review and immutable accepted revision are recorded
  in [review.json](review.json) and every child/epic. Branch-policy inspection remains
  unavailable; ordinary GitHub merge requirements apply without bypass.
- **Roadmap placement verified:** coordinator added all seven children to existing
  project 4 and independently verified membership on 2026-10-27.
- **Provider support/scope/recovery unqualified:** FO01 and authorized fixture owner
  resolve current eligibility, effective view/data role, automation and read-only
  turn recovery before native activation. Preserve existing budgets/permissions.
- **Consumers/release/live fixtures not verified:** FO06 coordinates Context/Insights;
  FO07 integrates compatible workers/hosts/migrations and tests two non-ADP scopes.
  These are implementation acceptance, not reasons to hide incomplete design review.

Do not close implementation stories or the epic based on this design PR. Keep
blocked/not-run distinct from observed proof; publish operational evidence privately.
