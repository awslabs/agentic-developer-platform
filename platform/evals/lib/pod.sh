# shellcheck shell=bash
# =============================================================================
# lib/pod.sh — the clean room: a pod the harness spawns, and laptop()
# =============================================================================
# THE SPLIT (#4171)
#   HARNESS — the eval process, on the ARC runner. Holds the credentials,
#     resolves SSM, seeds Cognito, reads Postgres, asserts.
#   LAPTOP  — a pod created from a stock public image with
#     `automountServiceAccountToken: false` and no env, reached ONLY via
#     `kubectl exec`. It is the emulated developer machine.
#
# Three mechanisms enforce the boundary:
#   1. The pod is created from a stock image with no service-account token and no
#      environment, so it HAS no platform credential to borrow.
#   2. `--assert-clean-room` is the FIRST command exec'd in that pod.
#   3. `laptop()` — every command emulating the developer's laptop goes through
#      it, and it can only reach the pod. Only h_aws/h_kubectl/h_psql see creds.
#
# Secrets never travel on an exec'd command line: anything in the argv of
# `kubectl exec` is visible in the exec API and in the runner's process table.
# Tokens reach the pod on STDIN (laptop_put_file) and live in 0600 files.
#
# CONTRACT WITH THE CALLER — define before use:
#   AWS_REGION, POD_NAMESPACE, POD_IMAGE, POD_LABEL_APP, POD_RUN_LABEL,
#   LAPTOP_POD, POD_WORKDIR, POD_HOME, POD_PATH, EVAL_SCRIPT_PATH, EVAL_LIB_DIR
#   WORKDIR (scratch), and POD_ASSERT_ENV (array; EMPTY in a real run)
# =============================================================================

# laptop() runs a command as the emulated developer — INSIDE the clean-room pod,
# via `kubectl exec -i`. This is the mechanism that makes "harness credentials are
# never visible to the laptop steps" a property of the code rather than a promise
# in a doc, and it is strictly stronger than the env-scrubbing wrapper it replaced
# (#4171): `kubectl exec` forwards NO environment from the runner at all, and the
# pod is created with `automountServiceAccountToken: false` and no env, so there is
# no platform credential in there to borrow in the first place.
#
# `-i` is always passed so stdin piping keeps working: that is how a refresh token
# reaches the pod (`laptop ... < file`). Never put a secret in the argv of a
# laptop command — an exec'd command line is visible in the exec API and in the
# runner's own process table.
laptop() {
  h_kubectl exec -i "$LAPTOP_POD" -n "$POD_NAMESPACE" -- \
    env AWS_EC2_METADATA_DISABLED=true \
        AWS_DEFAULT_REGION="$AWS_REGION" \
        HOME="$POD_HOME" \
        PATH="$POD_PATH" \
        "$@"
}

# laptop_put_file <local-src> <pod-dst> [mode] — stream a file into the pod on
# STDIN. Used for tokens and curl configs, so the content never appears in an
# exec'd command line. The `sh -c '...' _ "$@"` form keeps even the paths out of
# the snippet body, so nothing here can be mis-quoted.
laptop_put_file() {
  local src="$1" dst="$2" mode="${3:-600}"
  # shellcheck disable=SC2016  # $1/$2 are expanded by the POD's shell, not here
  laptop sh -c 'umask 077; mkdir -p "$(dirname "$1")"; cat > "$1"; chmod "$2" "$1"' \
    _ "$dst" "$mode" < "$src"
}

# laptop_get_file <pod-src> <local-dst> — bring a response body back to the
# harness so the harness (which has jq) can assert on it.
laptop_get_file() {
  laptop cat "$1" > "$2"
}

# The pod-side twin of write_curl_auth_config(). The token is read from a pod file
# into a pod shell variable — never into argv, and never back to the runner.
laptop_write_curl_auth_config() {
  # shellcheck disable=SC2016  # the token is expanded pod-side; interpolating it here would put it in argv
  laptop sh -c 'umask 077; IFS= read -r t < "$1"; printf "header = \"Authorization: Bearer %s\"\n" "$t" > "$2"; chmod 600 "$2"' \
    _ "$1" "$2"
}

# laptop_http_post_json <pod-cfg> <url> <pod-body> <pod-out> -> echoes status.
# The pod-side twin of http_post_json(): same flags, same 0600-config discipline.
laptop_http_post_json() {
  laptop curl -sS -o "$4" -w '%{http_code}' -X POST \
    -K "$1" \
    -H 'content-type: application/json' \
    --data-binary "@$3" \
    --max-time 120 \
    "$2" || echo "000"
}

# The pod-side twin of port_is_open(). /dev/tcp is a bash feature, hence bash.
laptop_port_is_open() {
  # shellcheck disable=SC2016  # $1 is expanded by the pod's bash (/dev/tcp is a bash feature)
  laptop bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"' _ "$1" >/dev/null 2>&1
}

# -----------------------------------------------------------------------------
# Pod lifecycle
# -----------------------------------------------------------------------------
# Replaces the job-level `container:` that PR #4165 shipped. The ARC scale set has
# no Docker daemon and no containerMode, so a `container:` job cannot start at all
# (run 32987705457: "failed to connect to the docker API"). A pod the runner
# creates with kubectl is something the runner demonstrably CAN do, and it is a
# better clean room besides: no service-account token, no env, no volumes.
laptop_pod_spec() {
  # Built with jq rather than a heredoc so the JSON is valid by construction.
  # `containers` must be specified in FULL: overriding it without `command` would
  # make the pod run the image's ENTRYPOINT and exit immediately (the same trap
  # documented in the ARC runner's pod template).
  jq -nc \
    --arg image "$POD_IMAGE" \
    --arg app "$POD_LABEL_APP" \
    --arg run "$POD_RUN_LABEL" \
    '{
      metadata: {
        labels: {app: $app, run: $run},
        annotations: {"karpenter.sh/do-not-disrupt": "true"}
      },
      spec: {
        # No token to steal: the single most important line in this file.
        automountServiceAccountToken: false,
        # Service links would inject <SERVICE>_SERVICE_HOST env vars from the
        # namespace into the pod. A clean room has no ambient env at all, and the
        # clean-room gate should not have to know which services happen to exist.
        enableServiceLinks: false,
        restartPolicy: "Never",
        # Requests so Karpenter right-sizes rather than packing the laptop onto a
        # saturated node; limits so an npm install cannot starve its neighbours.
        containers: [{
          name: "laptop",
          image: $image,
          command: ["sleep", "3600"],
          resources: {
            requests: {cpu: "500m", memory: "1Gi"},
            limits:   {cpu: "2",    memory: "4Gi"}
          }
        }]
      }
    }'
}

# Copy the harness INTO the pod, mirroring the repo layout so the in-pod copy
# resolves its own lib by the same relative path the runner copy does:
#
#   $POD_WORKDIR/harness/<eval-name>/run-eval.sh
#   $POD_WORKDIR/harness/lib/*.sh
#
# The alternative — passing a lib path in as an env var — would mean adding
# environment to the clean room in order to check that the clean room has no
# environment. The relative layout needs none.
laptop_put_harness() {
  local eval_dir_name lib_file
  eval_dir_name="$(basename "$(dirname "$EVAL_SCRIPT_PATH")")"
  POD_EVAL_SCRIPT="$POD_WORKDIR/harness/${eval_dir_name}/$(basename "$EVAL_SCRIPT_PATH")"

  laptop_put_file "$EVAL_SCRIPT_PATH" "$POD_EVAL_SCRIPT" 755 \
    || return 1
  for lib_file in "$EVAL_LIB_DIR"/*.sh; do
    [ -f "$lib_file" ] || continue
    laptop_put_file "$lib_file" "$POD_WORKDIR/harness/lib/$(basename "$lib_file")" 644 \
      || return 1
  done
}

laptop_pod_create() {
  log "creating the clean-room pod ${POD_NAMESPACE}/${LAPTOP_POD} (${POD_IMAGE})"

  # Recorded BEFORE the create call, for the same reason a mutated flag is: if the
  # create half-succeeds, cleanup must still know to sweep.
  state_set LAPTOP_POD "$LAPTOP_POD"
  state_set POD_NAMESPACE "$POD_NAMESPACE"

  h_kubectl run "$LAPTOP_POD" -n "$POD_NAMESPACE" \
    --image="$POD_IMAGE" --restart=Never \
    --overrides="$(laptop_pod_spec)" \
    --command -- sleep 3600 >/dev/null \
    || die "could not create the clean-room pod ${POD_NAMESPACE}/${LAPTOP_POD} — does the runner's RBAC allow 'create pods' in ${POD_NAMESPACE}?"

  h_kubectl wait --for=condition=Ready "pod/${LAPTOP_POD}" -n "$POD_NAMESPACE" --timeout=180s >/dev/null \
    || die "the clean-room pod never became Ready in 180s"

  # THE CONTAMINATION GATE, run INSIDE the pod as its first exec'd command and
  # deliberately BEFORE anything installs tooling or mutates the target env — so a
  # contaminated clean room burns seconds, not fifteen minutes, and leaves the
  # target environment untouched. The same files the harness runs from, copied in
  # and exec'd there, so the check can never drift from a re-implementation.
  laptop_put_harness \
    || die "could not copy the eval harness into the clean-room pod"

  # The gate is pointed at the pod's OWN login HOME, not the laptop HOME the
  # journey is about to create: what it is looking for is CLI config the IMAGE
  # shipped, and an empty directory that does not exist yet is pristine by
  # construction and would prove nothing. Discovered rather than hardcoded to
  # /root so a future non-root image is still checked in the right place.
  # Named image_home, not pod_home: POD_HOME (the journey's HOME, set by laptop())
  # and a local pod_home differ only by case, and the two mean OPPOSITE things
  # here — this one is the HOME the IMAGE shipped, which the gate must find
  # pristine; POD_HOME is the tree the journey is about to populate. Confusing
  # them would point the contamination gate at a directory that does not exist
  # yet and pass vacuously, so the names are kept far apart on purpose.
  local image_home
  # shellcheck disable=SC2016  # reads the POD's $HOME, so it must not expand on the runner
  image_home="$(h_kubectl exec "$LAPTOP_POD" -n "$POD_NAMESPACE" -- sh -c 'printf "%s" "${HOME:-/root}"' 2>/dev/null || echo "/root")"

  # `env` applies assignments left to right, so this HOME= wins over the POD_HOME
  # laptop() sets. The `[@]+` guard keeps an empty POD_ASSERT_ENV from tripping
  # `set -u` on bash < 4.4.
  # EVAL_WORKDIR is pinned inside the pod so the in-pod run scratches in its own
  # tree. In a real run the pod has no EVAL_WORKDIR at all; under --dry-run the
  # exec stub would otherwise leak the harness's, and the nested run would
  # truncate the harness's own trace/results files.
  if laptop HOME="$image_home" EVAL_WORKDIR="$POD_WORKDIR/gate" \
       ${POD_ASSERT_ENV[@]+"${POD_ASSERT_ENV[@]}"} \
       bash "$POD_EVAL_SCRIPT" --assert-clean-room; then
    pass "clean room is a fresh pod: no service-account token, no ambient env, nothing pre-installed (HOME=${image_home})"
  else
    die "the clean-room pod failed --assert-clean-room — refusing to run the matrix (see the violations above)"
  fi

  # The emulated developer's HOME and the npm prefix the journey installs into.
  # shellcheck disable=SC2016  # $1 is expanded by the pod's shell
  laptop sh -c 'mkdir -p "$1/bin" "$1/.npm-global" && chmod 700 "$1"' _ "$POD_HOME" \
    || die "could not prepare the laptop HOME in the clean-room pod"
}

# The tools a real developer's laptop already has, installed AFTER the
# contamination gate has passed. Deliberately NOT claude/codex where a journey
# under test installs those itself. The aws CLI is here for
# cognito-idp:InitiateAuth, which is an UNSIGNED API and needs no credentials — if
# a laptop step ever started needing signed calls, it would fail, which is exactly
# the signal we want.
laptop_provision() {
  log "provisioning the laptop pod with the tools a developer already has (jq, curl, aws)"
  laptop bash -c '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends jq curl unzip ca-certificates procps
    if ! command -v aws >/dev/null 2>&1; then
      curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
      unzip -q /tmp/awscli.zip -d /tmp
      /tmp/aws/install >/dev/null
    fi
    aws --version >/dev/null && jq --version >/dev/null
  ' >"$WORKDIR/pod-provision.log" 2>&1 \
    || die "could not provision the laptop pod: $(tail -3 "$WORKDIR/pod-provision.log" | tr '\n' ' ')"
}

# Sweep by LABEL, not by name: a pod leaked by a crashed run is caught even though
# this process never learned its run id. Safe because `concurrency` in the
# workflow guarantees at most one live run per environment.
laptop_pod_delete() {
  h_kubectl delete pod -n "$POD_NAMESPACE" \
    -l "app=${POD_LABEL_APP}" --ignore-not-found --wait=false >/dev/null 2>&1
}
