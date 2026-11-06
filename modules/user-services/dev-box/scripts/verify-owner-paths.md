# Dev Box owner-path acceptance

Run from an authorized operator shell **after staging the Crew bundle** on dev. This
check uses the existing owner's box, stops it, and replaces it once. It never
creates a validation service. Have an owner ADP bearer (`adp token` or `ADP_TOKEN`),
operator AWS access for EC2 describe, SSM commands and parameter read, and the
pinned profile revision. Do not paste tokens into terminal commands, evidence,
or issue comments.

```sh
python3 modules/user-services/dev-box/scripts/verify-owner-paths.py \
  --env dev --region "$AWS_REGION" --evidence "$HOME/owner-paths-$(date -u +%Y%m%dT%H%M%SZ).json"
```

`deploy-dev-box.sh --verify-live` runs the same check after staging and writes the
receipt under the local release directory. `--box-id` selects the owner's box;
without it, exactly one unreleased box must exist. `--no-replace` stops and starts
that box instead and omits the staged-release assertion. Evidence files are
created exclusively with mode 0600. A failed check exits nonzero, names its check
in the JSON, and leaves the owner's box reserved for investigation. Do not publish
private box IDs, instance IDs, deployment addresses, or raw receipts in public
issues: link private evidence where authorized, or redact first.

The runner waits for the **observed** running state, then requests a single-use
handoff ticket from the owner API and redeems it on the Crew dashboard origin at
20 seconds (up to 60 seconds after running). It checks the ready target and owner
box generation. After 90 seconds it reads only aggregate status from the exact
running generation through SSM: an expired token before a 200 model request,
refreshed token afterward, 200 tools listing with at least 10 tools, no quarantined
servers and no recent adapter failures. On Replace it compares the installed
profile revision and current symlink to the staged revision. The SSM command
never returns token strings, challenge text, model output, process environment
or journal lines; neither the runner nor the evidence file stores them.

Before asking the owner to open Crew, verify `checks` all say `passed` and the
new generation matches the one under test. On failure, inspect the named check,
the controller and host journals through authorized access; restage the prior
bundle and Replace again to recover. The live receipt is post-deploy evidence,
not a substitute for local fixture tests.
