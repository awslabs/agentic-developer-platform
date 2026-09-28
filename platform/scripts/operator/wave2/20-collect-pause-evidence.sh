#!/usr/bin/env bash
# Wave 2 fixture step 2 — collect the REAL Claude SDK pause evidence (W2-03/04/05).
#
# Issue #3968 / epic #3959.
#
# What this does: runs the repository's existing live-SDK experiment harness
# (`src/control-runtime.integration.ts`), which drives real `query()` calls under
# `permissionMode: 'bypassPermissions'` through the neutral PauseGate coordinator
# with the production spill hooks composed, and measures side effects from OUTSIDE
# the agent (filesystem, a loopback service counter, admission counts). It then
# assembles those measurements into the three artifacts the harness reads:
# pause_boundary, pause_resume, pause_expiry.
#
# Why a separate assembler: the experiment runner emits one artifact blob per
# experiment; the harness reads three consolidated files with specific key names.
# The assembler copies MEASURED values only and writes null for anything an
# experiment did not measure -- a missing observation must never read as a
# satisfied one. See 21-assemble-pause-artifacts.py.
#
# This step needs live model access (it makes real Claude calls) but NO AWS
# deployment credential.
#
# Usage:
#   ./20-collect-pause-evidence.sh --evidence-dir <dir> [--reuse <recorded.json>]
#
#   --reuse   skip the live run and assemble from an existing experiment JSON
#             (for re-assembling evidence without re-spending model calls).

set -euo pipefail

EVIDENCE_DIR=""
REUSE=""
EXPECTED_IDENTITY="${W2_EXPECTED_IDENTITY:-}"
LEDGER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --evidence-dir)      EVIDENCE_DIR="${2:?}"; shift 2 ;;
    --reuse)             REUSE="${2:?}"; shift 2 ;;
    --expected-identity) EXPECTED_IDENTITY="${2:?}"; shift 2 ;;
    --ledger)            LEDGER="${2:?}"; shift 2 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok()   { printf 'ok   %s\n' "$*"; }
note() { printf '     %s\n' "$*"; }

[ -n "$EVIDENCE_DIR" ] || fail "--evidence-dir is required"
case "${ADP_CONTROL_EVAL_SUITE:-pause}" in
  pause) ;;
  steering-input) [ -z "$REUSE" ] || fail "targeted steering observations require a fresh run" ;;
  *) fail "unknown ADP_CONTROL_EVAL_SUITE" ;;
esac

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"
AGENT_DIR="$REPO_ROOT/modules/agent-factory/agent"
ASSEMBLER="$HERE/21-assemble-pause-artifacts.py"

[ -d "$AGENT_DIR" ] || fail "cannot find $AGENT_DIR"
[ -f "$ASSEMBLER" ] || fail "assembler missing: $ASSEMBLER"

mkdir -p "$EVIDENCE_DIR/artifacts"
RAW="$EVIDENCE_DIR/artifacts/raw-pause-experiments.json"
if [ "${ADP_CONTROL_EVAL_SUITE:-pause}" = "steering-input" ]; then
  RAW="$EVIDENCE_DIR/artifacts/steering-input-stream.json"
fi

BINDING="$HERE/lib/experiment_binding.py"
[ -f "$BINDING" ] || fail "missing $BINDING"
BINDING_OUT="$EVIDENCE_DIR/artifacts/experiment-binding.json"
mkdir -p "$EVIDENCE_DIR/artifacts"

# The SDK version is an identity axis, so it must be known BEFORE deciding whether
# a reuse is honest or a live run is permitted.
sdk_version_installed() {
  ( cd "$AGENT_DIR" && node -p \
      "JSON.parse(require('fs').readFileSync(require('path').join(require('path').dirname(require.resolve('@anthropic-ai/claude-agent-sdk')), 'package.json'), 'utf8')).version" 2>/dev/null ) || echo unknown
}

# The runtime image must come from the API SERVER. A process cannot read its own image
# digest from inside the container, and the question being settled is what is ACTUALLY
# running -- so a value derived from anything the container itself holds (an env var, a
# build label baked into the image) is a claim, not an observation.
#
# WHO ASKS THE API SERVER, AND WHY IT IS NOT THIS PROCESS
# ------------------------------------------------------
# The OPERATOR, before this script runs. An earlier revision called `kubectl get pod`
# from inside the worker. Root verified that cannot work: the protected service
# account can neither `get` nor `list` pods, and the answer is NOT to grant it that --
# "preserve the protected SA boundary; do not grant it broad pod reads just to make a
# test helper work". An in-pod query would fail on RBAC and, because an unreadable pod
# leaves the image unobserved, would refuse every protected run.
#
# So the observation still comes from the API server, one step earlier:
# lib/worker_observation.py reads the pod, resolves status.containerStatuses[].imageID
# (the digest actually running, not the requested tag) and records it in the
# expected-identity document AGAINST THE POD UID. This process reads it from there and
# proves the document is about itself by matching its own projected metadata.uid --
# which is what classify_host does.
#
# That keeps the property the kubectl call was for. The document is written OUTSIDE
# this pod by the run that provisioned it, so it is not a value this process could
# have written; and it is not merely trusted, because the uid match is what ties it to
# this container. An env var remains refused for exactly the reason it always was.
#
# Needed by BOTH paths, hence defined before the branch: a live run has classify_host
# require it, and a reuse compares it as an identifying axis. Every failure path here
# deliberately leaves OBSERVED_IMAGE EMPTY so the refusal comes from the checker.
# Guessing a value would turn an unidentified image into an apparently observed one --
# the exact substitution this step exists to prevent.
#
# It writes OBSERVED_IMAGE rather than W2_RUNTIME_IMAGE, because the two are different
# claims and collapsing them is what root objected to: "discover_runtime_image is
# BYPASSED if W2_RUNTIME_IMAGE is already set, then logs it as observed from pod
# status -- still a writable environment assertion." An env var is something this
# process (or anything that spawned it) can write; the API server's report is not.
OBSERVED_IMAGE=""
discover_runtime_image() {
  # NO in-pod kubectl. See the header: the protected SA cannot get or list pods and is
  # deliberately not granted it, so the operator's recorded observation is the source.
  local doc uid_file projected
  doc="${EXPECTED_IDENTITY:-}"
  [ -n "$doc" ] || { note "no expected-identity document, so no recorded observation"; return 0; }
  [ -f "$doc" ] || { note "expected-identity document not readable: $doc"; return 0; }

  # W2_POD_IDENTITY_DIR is the same TEST seam W2_SA_DIR was, and for the same reason:
  # root's finding that shell tests must not depend on real host mounts or fabricated
  # system credentials. It cannot widen what is admitted -- classify_host makes the
  # authoritative decision against a document this process cannot write.
  uid_file="${W2_POD_IDENTITY_DIR:-/var/run/adp-w2-identity}/pod-uid"
  projected="$(cat "$uid_file" 2>/dev/null || true)"
  if [ -z "$projected" ]; then
    note "no projected pod uid at $uid_file -- not running as the provisioned fixture pod,"
    note "  so the recorded image cannot be shown to describe THIS container"
    return 0
  fi

  # The uid match is what makes reading the document an observation about this pod
  # rather than an inherited claim. Done in python for exact JSON handling; it prints
  # the image only when the document's pod_uid IS this container's projected uid.
  OBSERVED_IMAGE="$(W2_DOC="$doc" W2_UID="$projected" python3 -c '
import json, os, sys
try:
    doc = json.load(open(os.environ["W2_DOC"]))
except Exception:
    sys.exit(0)
identity = doc.get("expected_identity", doc)
if not isinstance(identity, dict):
    sys.exit(0)
recorded_uid = (identity.get("pod_uid") or "").strip()
image = (identity.get("runtime_image") or "").strip()
# Both must be present AND equal. An absent recorded uid must not compare equal to
# anything: "" == "" would read as agreement about an identity neither side stated.
if recorded_uid and image and recorded_uid == os.environ["W2_UID"].strip():
    print(image)
' 2>/dev/null || true)"

  if [ -z "$OBSERVED_IMAGE" ]; then
    note "the expected-identity document records no image for this pod uid ($projected),"
    note "  so the runtime image is unknown. It is NOT taken from the environment."
  fi
  return 0
}

# The recorded observation is ALWAYS consulted. Previously a pre-set W2_RUNTIME_IMAGE
# skipped the lookup entirely and was then announced as "observed from the pod status",
# so the one axis that was supposed to come from the API server could be supplied by
# the environment instead. Root: "still a writable environment assertion."
discover_runtime_image

if [ -n "${W2_RUNTIME_IMAGE:-}" ] && [ -n "$OBSERVED_IMAGE" ]; then
  # Both present: they must agree on the DIGEST, which is the content identity. The
  # transport prefix and repository differ harmlessly between kubelet's imageID and a
  # registry reference, so only the sha256 is compared -- the same rule same_image()
  # applies, kept here so the disagreement is caught before anything is spent.
  env_digest="${W2_RUNTIME_IMAGE##*sha256:}"
  obs_digest="${OBSERVED_IMAGE##*sha256:}"
  if [ "$W2_RUNTIME_IMAGE" = "$env_digest" ] || [ "$OBSERVED_IMAGE" = "$obs_digest" ] \
     || [ "$env_digest" != "$obs_digest" ]; then
    fail "W2_RUNTIME_IMAGE in the environment ($W2_RUNTIME_IMAGE) is not the image the
       API server reports this pod running ($OBSERVED_IMAGE). The environment variable
       is a claim; the pod status is an observation. Where they disagree the evidence
       would be attributed to bytes that did not execute it, so this is refused rather
       than resolved in favour of either one."
  fi
  ok "runtime image observed from the pod status and confirmed by the environment: $OBSERVED_IMAGE"
elif [ -n "$OBSERVED_IMAGE" ]; then
  ok "runtime image observed from the pod status: $OBSERVED_IMAGE"
elif [ -n "${W2_RUNTIME_IMAGE:-}" ]; then
  # Deliberately NOT used. An unverifiable environment variable is exactly the
  # writable assertion this step exists to replace, and accepting it here would mean
  # the image axis could be satisfied without the API server ever being asked.
  note "W2_RUNTIME_IMAGE is set but the pod status could not be read, so the image is"
  note "  NOT treated as observed: an environment variable this process could have"
  note "  written is a claim about what is running, not a measurement of it."
fi

# One value flows onward, and it is the observed one.
W2_RUNTIME_IMAGE="$OBSERVED_IMAGE"
export W2_RUNTIME_IMAGE

if [ -z "$OBSERVED_IMAGE" ]; then
  note "runtime image NOT observed. A live run will be refused by check-host and a"
  note "  reuse will be refused as an unknown identity axis -- both intended, not"
  note "  errors to work around."
fi

if [ -n "$REUSE" ]; then
  [ -f "$REUSE" ] || fail "--reuse file not found: $REUSE"

  # Root's blocker 6b: "--reuse may assemble artifacts but cannot falsely bind old
  # outputs to a new revision/run."
  #
  # Reusing output recorded before a PauseGate change produces an artifact that
  # claims to measure the current code. That is worse than a missing artifact,
  # because it is indistinguishable from a real measurement -- the harness cannot
  # detect it and the result looks like a pass. So the recorded binding must match
  # the current one on every identity axis, and a mismatch REFUSES rather than
  # warns: a warning scrolls past, but the artifact outlives the terminal.
  RECORDED_BINDING="$(dirname "$REUSE")/experiment-binding.json"
  [ -f "$RECORDED_BINDING" ] || fail "cannot reuse $REUSE: no experiment-binding.json beside it.
       Output recorded before bindings were tracked cannot be shown to describe the
       current revision, so reusing it would assert something unverified. Re-run the
       experiment, or reuse output that carries its binding."

  # --verify-raw names the file actually being reused, which is $REUSE and not
  # necessarily the path recorded at measurement time. Without it the digest check
  # would verify the recorded path and then the cp below would install DIFFERENT
  # bytes as the evidence -- verifying one artifact and shipping another, which is
  # the substitution the digest exists to catch.
  #
  # --runtime-image is passed because runtime_image is an IDENTIFYING axis: omitting
  # it makes the current side unknown, and an unknown identifying axis is a refusal.
  # A reuse must therefore observe the image it is running as, exactly like a live run.
  python3 "$BINDING" verify-reuse \
    --recorded "$RECORDED_BINDING" \
    --repo-root "$REPO_ROOT" \
    --sdk-version "$(sdk_version_installed)" \
    --permission-mode bypassPermissions \
    --runtime-image "${W2_RUNTIME_IMAGE:-}" \
    --verify-raw "$REUSE" \
    --out "$EVIDENCE_DIR/artifacts/reuse-verdict.json" \
    || fail "refusing to reuse experiment output across an identity change (see above).
       Re-run the experiment to obtain evidence about the current revision."

  cp -f "$REUSE" "$RAW"
  # The RECORDED binding is installed verbatim, not a freshly captured one. Root's
  # finding 4: "Preserve original provenance on reuse; no relabeling." Capturing a new
  # binding here would stamp today's run nonce on yesterday's measurement, which is
  # precisely the relabeling -- the artifact would name a run that did not produce it.
  cp -f "$RECORDED_BINDING" "$BINDING_OUT"

  # And confirm that is what landed. Asserted rather than assumed because the whole
  # value of the reuse path is that the provenance survives it: a copy that silently
  # lost the nonce or the fixture run would leave an artifact claiming a measurement
  # with nothing behind it, which is the shape of every defect in this PR.
  #
  # The comparison lives in the library rather than inline here so it is reachable
  # from a hermetic unit test. Inline, its only coverage was an end-to-end reuse --
  # which a dirty working tree correctly refuses, so on a developer machine the check
  # was never exercised at all and could have been silently broken.
  python3 "$BINDING" verify-preserved \
    --recorded "$RECORDED_BINDING" \
    --written "$BINDING_OUT" \
    || fail "the reuse did not preserve the recorded run's provenance (see above).
       The reused bytes were measured by another run; an artifact that relabels them
       with this run's identity names a run that did not produce the evidence."
  ok "reusing recorded experiment output from $REUSE (binding verified, provenance preserved)"
else
  # ---- the cheapest checks first, before anything is installed or spent ----
  #
  # The provisioning record is required on the live path, because the host check is
  # only about identity if there is something to compare against. Root's finding:
  # namespace/token presence "establishes no fixture ownership or actual executing
  # identity", and a role-name heuristic admitted an arbitrary `Worker` role in the
  # wrong account. The reference is written OUTSIDE this pod by the root-owned
  # launcher that provisioned the fixture; this process cannot author its own.
  #
  # Ordered ahead of `npm ci` and the lockfile comparison deliberately: those take
  # minutes and mutate node_modules, and a run that was always going to be refused
  # should cost neither. Argument validation is free, so it goes first.
  [ -n "$EXPECTED_IDENTITY" ] || fail "--expected-identity is required for a live run.
       The launcher that provisioned the fixture must write the expected identity
       (run_id, account_id, namespace, pod_name, service_account, aws_role_arn,
       runtime_image) and pass its path. Without it this process can only describe
       itself, and self-description is not authorisation."
  [ -f "$EXPECTED_IDENTITY" ] || fail "expected-identity document not found: $EXPECTED_IDENTITY"
  [ -n "$LEDGER" ] || fail "--ledger is required for a live run. The run nonce and the
       target the evidence describes come from the fixture ledger, not from a number
       this script mints: a minted nonce is unique but names nothing, so it cannot tie
       the evidence to the run whose workload and queue were actually measured."
  [ -f "$LEDGER" ] || fail "fixture ledger not found: $LEDGER"

  # The integration file is deliberately NOT named *.test.ts so jest cannot
  # collect it; it is run explicitly.
  cd "$AGENT_DIR"
  if [ ! -d node_modules ]; then
    note "node_modules absent -- installing (no AWS credential required)"
    npm ci --include=dev >/dev/null 2>&1 || fail "npm ci failed in $AGENT_DIR"
  fi

  # Confirm the installed SDK matches the lockfile pin BEFORE spending model
  # calls: evidence from another SDK version does not carry over (the harness
  # rejects it), so discovering a mismatch afterwards wastes the whole run.
  INSTALLED="$(node -p "JSON.parse(require('fs').readFileSync(require('path').join(require('path').dirname(require.resolve('@anthropic-ai/claude-agent-sdk')), 'package.json'), 'utf8')).version" 2>/dev/null || echo missing)"
  PINNED="$(python3 - <<'PY'
import json
lock = json.load(open("package-lock.json"))
for key in ("node_modules/@anthropic-ai/claude-agent-sdk",):
    entry = lock.get("packages", {}).get(key)
    if entry and entry.get("version"):
        print(entry["version"]); break
else:
    print("unknown")
PY
)"
  [ "$INSTALLED" = "$PINNED" ] \
    || fail "installed SDK $INSTALLED != lockfile pin $PINNED; the harness rejects
       pause evidence from a version other than the pinned one"
  ok "installed SDK $INSTALLED matches lockfile pin"

  # Root's blocker 6a: "Never run bypassPermissions SDK experiments on root's
  # administrator host." bypassPermissions disables the tool-permission prompts, so
  # an agent that reaches for the AWS CLI on a credentialed host performs an
  # unreviewed administrator action. The experiment is not malicious; it is
  # unsupervised by construction, which is the whole point of the mode.
  #
  # Checked BEFORE any model call, so a refusal costs nothing. Fails closed: a host
  # that cannot be positively identified as a scoped fixture is treated as
  # possibly-privileged.
  printf '\n== experiment host ==\n'
  python3 "$BINDING" check-host \
    --expected-identity "$EXPECTED_IDENTITY" \
    --out "$EVIDENCE_DIR/artifacts/host-verdict.json" \
    || fail "refusing to run the experiment on this host (see above). Nothing was spent."

  # Captured before the run, so the artifact is bound to the revision that produced
  # it rather than to whatever HEAD happens to be at assembly time. The raw output
  # does not exist yet, so its digest is recorded in a SECOND capture below -- this
  # one deliberately carries no digest rather than a placeholder, because
  # is_unknown() must be able to tell that nothing was observed.
  #
  # The nonce is NOT passed: `capture` takes it from --ledger, so the evidence is
  # bound to the fixture run that created the target rather than to a fresh number.
  #
  # And `capture` now EXITS NONZERO when the identity is unknown or the tree is dirty.
  # That refusal used to exist only on the --reuse path, so the live path -- the one
  # that spends money -- recorded source_revision=unknown, source_dirty=true and even
  # host_allowed=false and exited 0. The check is worth most here, where it is free:
  # a refusal before the run costs nothing, whereas discovering afterwards that the
  # evidence cannot be attributed to a revision means the spend is already gone and
  # the artifact still looks like a real measurement.
  python3 "$BINDING" capture \
    --repo-root "$REPO_ROOT" \
    --sdk-version "$INSTALLED" \
    --permission-mode bypassPermissions \
    --runtime-image "${W2_RUNTIME_IMAGE:-}" \
    --expected-identity "$EXPECTED_IDENTITY" \
    --ledger "$LEDGER" \
    --out "$BINDING_OUT.pre" >/dev/null \
    || fail "refusing to run the experiment: its identity is not established (see above).
       Nothing was spent. An experiment whose revision, tree state, SDK, image or host
       is unknown produces evidence that cannot be attributed to any code -- which is
       worse than no evidence, because it is indistinguishable from a real measurement."
  ok "identity binding captured and admissible ($BINDING_OUT.pre)"

  # Read back the nonce the LEDGER supplied, so the post-run capture binds the same
  # run. Taken from the artifact rather than recomputed: one source, one value.
  RUN_NONCE="$(python3 -c '
import json, sys
doc = json.load(open(sys.argv[1]))
print((doc.get("binding") or doc).get("run_nonce") or "")
' "$BINDING_OUT.pre")"
  [ -n "$RUN_NONCE" ] || fail "the pre-run binding recorded no run nonce"

  note "running the live-SDK pause experiments (real Claude calls, bypassPermissions)"
  note "  this makes paid model calls -- it is the only way to obtain AC-P1 evidence;"
  note "  unit mocks are explicitly not acceptable for these checks."
  # Non-zero exit means one or more experiments failed. We still assemble, so the
  # artifact records the real (failing) observation rather than nothing.
  set +e
  if [ "${ADP_CONTROL_EVAL_SUITE:-pause}" = "steering-input" ]; then
    npx --no-install ts-node src/steering-input.integration.ts --json "$RAW"
  else
    npx --no-install ts-node src/control-runtime.integration.ts --heartbeat --json "$RAW"
  fi
  RUN_RC=$?
  set -e
  [ -f "$RAW" ] || fail "the experiment run produced no JSON at $RAW (rc=$RUN_RC)"
  if [ "$RUN_RC" -ne 0 ]; then
    note "one or more experiments FAILED (rc=$RUN_RC); assembling the real observations anyway"
  fi

  # The FINAL binding, captured now that the raw output exists so its digest can be
  # recorded. The pre-run binding could not carry one -- the bytes did not exist yet --
  # and writing a placeholder would have been worse than writing nothing, because a
  # later reuse cannot distinguish a placeholder from an observation.
  #
  # Same RUN_NONCE as the pre-run capture: the nonce names THIS run, and a reuse of
  # this output must be able to say which run produced it. (A reuse mints its own new
  # nonce, which is exactly why the nonce is not an equality axis -- it is carried.)
  # --require-output additionally demands that the bytes were digested: post-run, a
  # binding with no digest names no artifact and would vouch for any file presented to
  # it. Pre-run that is expected (the output does not exist yet), which is why the two
  # phases have different requirements rather than one lenient standard.
  #
  # --run-nonce is passed here and is CHECKED against the ledger rather than trusted:
  # a conflicting value is refused, not preferred, so this cannot re-open the
  # minted-nonce hole by supplying its own number.
  python3 "$BINDING" capture \
    --repo-root "$REPO_ROOT" \
    --sdk-version "$INSTALLED" \
    --permission-mode bypassPermissions \
    --runtime-image "${W2_RUNTIME_IMAGE:-}" \
    --expected-identity "$EXPECTED_IDENTITY" \
    --ledger "$LEDGER" \
    --raw-output "$RAW" \
    --run-nonce "$RUN_NONCE" \
    --require-output \
    --out "$BINDING_OUT" >/dev/null \
    || fail "the experiment ran but its output could not be bound to this revision and
       run (see above). The raw JSON is at $RAW; it is NOT usable as evidence, because
       nothing ties those bytes to the code that produced them."

  # If HEAD moved while the experiment was running, the output straddles two
  # revisions and belongs to neither. Compared rather than assumed: a long live run
  # gives a checkout or a rebase ample time to land underneath it.
  PRE_REV="$(python3 -c '
import json, sys
# capture writes the binding unwrapped; verify-reuse wraps it. Same fallback the
# reuse path uses, so this reads either shape.
doc = json.load(open(sys.argv[1]))
print((doc.get("binding") or doc).get("source_revision") or "")
' "$BINDING_OUT.pre" 2>/dev/null || true)"
  POST_REV="$(python3 -c '
import json, sys
# capture writes the binding unwrapped; verify-reuse wraps it. Same fallback the
# reuse path uses, so this reads either shape.
doc = json.load(open(sys.argv[1]))
print((doc.get("binding") or doc).get("source_revision") or "")
' "$BINDING_OUT" 2>/dev/null || true)"
  if [ -n "$PRE_REV" ] && [ -n "$POST_REV" ] && [ "$PRE_REV" != "$POST_REV" ]; then
    fail "the source revision changed while the experiment was running
       ($PRE_REV -> $POST_REV). This output measures neither revision cleanly, so
       binding it to either would misattribute it. Re-run on a settled tree."
  fi
  ok "experiment output bound to revision ${POST_REV:0:12} (raw digest recorded)"
fi

if [ "${ADP_CONTROL_EVAL_SUITE:-pause}" = "steering-input" ]; then
  note "Targeted steering input observations only; no pause or wave acceptance claimed."
  exit "${RUN_RC:-1}"
fi

printf '\n== assembling harness artifacts ==\n'
# `set -e` suspended: the assembler exits non-zero precisely when a required field
# was never measured, and that is the case the operator most needs the explanation
# below for. Under `set -e` the script would die on that exit and print nothing.
set +e
python3 "$ASSEMBLER" --raw "$RAW" --out-dir "$EVIDENCE_DIR/artifacts"
rc=$?
set -e

printf '\n'
if [ "$rc" -eq 0 ]; then
  ok "pause artifacts assembled into $EVIDENCE_DIR/artifacts/"
else
  note "assembly reported unmeasured required fields (exit $rc). The artifacts were"
  note "written with explicit nulls for those fields. The harness will FAIL the"
  note "owning check, which is correct: the observation was not made."
fi
exit "$rc"
