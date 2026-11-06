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
the public review document path and its required repository path.

The private output `runtime-plan-manifest.json` contains target references and
digests: installation/request/proposal/Terraform-source identity, selected
connection and RoleId, exact named resources, semantic plan digest, and exact
saved `installation.tfplan` SHA-256. Secret values and raw Terraform state/plan
files must not be published. Deployment identifiers in this manifest must also
remain private. Preserve the original private plan and receipt in the selected
access-controlled artifact store, such as S3.

The separate `runtime-plan-review.json` contains only its schema version, scope
and a SHA-256 commitment to the complete private manifest. The digest uses UTF-8
JSON with sorted keys and compact separators. Every target, resource, source,
operator and saved-plan binding remains covered without publishing those values.
Create a plan-review PR in **aws-e/adp**, based on `main`, committing this public
review document at `docs/runtime-plan-reviews/<review_id>.json`. Choose a review
ID without private deployment identifiers. Never commit the private manifest.
An authorized maintainer must inspect the saved-plan rendering, resource/policy
bounds, target and source, then either approve the exact PR head with a GitHub
`APPROVED` review or merge the plan PR into `main`. The maintainer may also be
the PR author. GitHub does not allow authors to submit approval reviews on their
own PRs, so use the normal merge flow for a same-author approval. A code review
or merge of the installer alone, a comment, label, local dict, matching hash,
or supplied reviewer name is not plan approval.

The maintained trust authority is repository ID `1186991269` (`aws-e/adp`), not
a repository or approver selected by environment configuration. The adapter
reads GitHub's authenticated repository, PR, commit-addressed contents, reviews
and collaborator-permission APIs. The reviewer or merger must currently have
the repository's `maintain` or `admin` role. For an open PR, the latest review
from that principal must be `APPROVED` for the current head. For a merged PR,
GitHub must report an actual merge into `main`, and the review document at both
the PR head and merge commit must match the saved private manifest exactly. Closing a PR
without merging does not approve it. A trusted reviewer's outstanding changes
request refuses either path. The adapter rechecks the PR head, review or merge
record, current permission and local plan bytes before returning approval.

GitHub App bots do not inherit maintainer authority merely because they can
post a review or merge a PR. No bot allowlist or App authority is invented here.
Both approval paths use the same current maintainer/admin permission check.
The authenticated GitHub credential must be able to read collaborator
permissions; unavailable authority refuses.

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

For an existing unexecuted plan with a full-manifest review PR, run preparation
with `--resume` and without `--execute` to generate `runtime-plan-review.json`
from the same preserved binary. Replace the PR file with this public commitment
and obtain approval of the new head or merge it. Old full-manifest PR documents
are no longer accepted as public review documents; local approval evidence is
never reused as authorization.

The CLI authenticates approval before resumed Terraform work. The preparation
hook authenticates it again before apply, then refreshes selected AWS identity.
`runtime-plan-review-evidence.json` is a private audit record, never a substitute
for live approval. Changed plan bytes, proposal/source/target, reviewer authority,
PR head, merge record or review state require renewed approval. Evidence records
whether approval came from a review or merge, including the merge commit when
applicable. Missing evidence refuses.
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

For an admitted operations run with a legitimate private-repository vault
connection, add `--github-vault-label '<exact GitHub connection label>'` to the
`--resume --execute` command. This explicitly selects service `github` through
the maintained `adp_cred.client.proxy_http` broker. No raw token is exported or
written to `gh` configuration. The run must already have its own authenticated
invocation binding and permission to use that connection. The reader allows
only the trusted repository's exact approval GET routes, refuses redirects and
non-200 responses, bounds response JSON to 2 MiB, and preserves every manifest,
head, reviewer and current-permission check. A missing client, unavailable vault
connection or denied response refuses before Terraform; it never falls back to
`gh` or another connection. The flag applies only to execution, since initial
planning does not authenticate approval.

Without this flag, the GitHub reader uses the caller's legitimately configured
`gh` GitHub identity, as before.
A personal-repository installation token that cannot read private `aws-e/adp`
metadata cannot execute this approval bridge. A real user-owned vault GitHub
connection with that access, or a separately maintained authenticated review
service, must be established first. Do not borrow a supervisor's/root session
token, replace authority with a cached approval dict, or change the trusted
repository to evade this requirement. The source tests do not attest that live
credential path for any operations run.
