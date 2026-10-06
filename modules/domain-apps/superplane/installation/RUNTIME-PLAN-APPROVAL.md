# Prepare and approve the isolated domain runtime

This command prepares only the five queue/role/policy resources owned by
`runtime_preparation.py`. It does not install a worker, register Gateway
principals, change bindings, install Secrets, enable admission, or approve a
user workspace operation. The existing installer modes remain separate.

Use the already selected AWS connection, with its independently verified IAM
RoleId in the private environment file. The CLI uses one command adapter for
both the read-only inspector and Terraform; preparation validates the exact
selected STS account/session/RoleId before its commands. It never assumes an
ambient administrator is the selected operator or creates a new credential.

```bash
./modules/domain-apps/superplane/deploy.sh --prepare-domain-runtime \
  --environment /private/environment.yaml \
  --release-lock /private/release-lock.yaml \
  --runtime-operator /private/runtime-operator.yaml \
  --output /private/runtime-preparation
```

The operator file is the closed reference mapping documented in
[RUNTIME-PREPARATION.md](RUNTIME-PREPARATION.md). Its `review_id` is a stable
identifier such as `demo-runtime-20261006`, never an approval assertion. Planning
reads the selected account and uses the isolated Terraform backend; it applies
nothing. The output directory must be private. The result prints the canonical
inspected plan digest, saved binary plan digest, private receipt/manifest paths,
and the required repository manifest path.

The output `runtime-plan-manifest.json` contains only target references and
digests: installation/request/proposal/Terraform-source identity, selected
connection and RoleId, exact named resources, semantic plan digest, and exact
saved `installation.tfplan` SHA-256. Secret values and raw Terraform state/plan
files must not be published. Preserve the original private plan and receipt.

Create a plan-review PR in **aws-e/adp**, based on `main`, committing this exact
JSON document at the printed `docs/runtime-plan-reviews/<review_id>.json` path.
Keep the PR open and ready for review. The reviewer must inspect the saved-plan
rendering, resource/policy bounds, target and source, and issue an actual GitHub
`APPROVED` review on that exact PR head. A code review of the installer alone,
a comment, label, local dict, matching hash, or supplied reviewer name is not
plan approval.

The maintained trust authority is repository ID `1186991269` (`aws-e/adp`), not
a repository or approver selected by environment configuration. The adapter
reads GitHub's authenticated repository, PR, commit-addressed contents, reviews
and collaborator-permission APIs. The approver must differ from the PR author
and currently have the repository's `maintain` or `admin` role. The latest
review from that principal must be `APPROVED` for the current head; a trusted
reviewer's changes request refuses. It rechecks the PR head, selected review,
permission and local plan bytes before returning approval.

GitHub App bots do not inherit maintainer authority merely because they can
post a review. No bot allowlist or App authority is invented here. A plan PR
authored by a hosted developer bot can receive a genuine approval from a
different repository maintainer. A plan PR authored by the same maintainer
cannot be self-approved. The authenticated GitHub credential must be able to
read collaborator permissions; unavailable authority refuses.

After that real approval, apply the same private saved plan:

```bash
./modules/domain-apps/superplane/deploy.sh --prepare-domain-runtime \
  --environment /private/environment.yaml \
  --release-lock /private/release-lock.yaml \
  --runtime-operator /private/runtime-operator.yaml \
  --output /private/runtime-preparation --resume --execute \
  --approved-plan-sha256 '<printed semantic plan digest>' \
  --plan-review 'https://github.com/aws-e/adp/pull/<plan-review-number>'
```

The CLI authenticates approval before resumed Terraform work. The preparation
hook authenticates it again before apply, then refreshes selected AWS identity.
`runtime-plan-review-evidence.json` is a private audit record, never a substitute
for live approval. Changed plan bytes, proposal/source/target, reviewer authority,
PR head or review state require renewed review. Missing evidence refuses.
A lost apply response uses preparation's existing same-receipt reconciliation;
it never authorizes an unconditional repeat apply.

This CLI requires the preparation receipt's `binary_plan_sha256` contract:
initial planning records exact saved bytes; planned resume inspects that saved
file without regenerating it; preparation rechecks its digest immediately before
apply. Older receipts without that field cannot resume through this CLI. The
command reports `apply_supported: false` for older preparation implementations
and refuses before any resumed Terraform command. Obtain reviewed source with
the saved-plan preservation fix and create a fresh plan; do not edit the receipt
to imitate that contract.


## Authentication availability for operations runs

The GitHub reader uses only the caller's legitimately configured GitHub identity.
A personal-repository installation token that cannot read private `aws-e/adp`
metadata cannot execute this approval bridge. A real user-owned vault GitHub
connection with that access, or a separately maintained authenticated review
service, must be established first. Do not borrow a supervisor's/root session
token, replace authority with a cached approval dict, or change the trusted
repository to evade this requirement. The source tests do not attest that live
credential path for any operations run.
