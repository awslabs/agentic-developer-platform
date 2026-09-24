#!/usr/bin/env python3
"""Bind SDK experiment output to the revision that produced it (issue #3968).

Root's blocker 6, two halves:

  (a) "Never run bypassPermissions SDK experiments on root's administrator host.
       Run in a protected/scoped fixture with verified image/source/SDK identity."
  (b) "`--reuse` may assemble artifacts but cannot falsely bind old outputs to a
       new revision/run."

WHY (b) IS THE DANGEROUS ONE
---------------------------
`--reuse` exists for a good reason: re-assembling artifacts without re-spending
paid model calls. But the published provenance block records only the SDK version
and the raw file path -- not the revision of the code under test. So reusing
yesterday's experiment output after changing the PauseGate produces an artifact
that claims to be evidence about today's code. It is not lying about the SDK; it
is silent about the thing that changed.

That is worse than a missing artifact, because it is indistinguishable from a real
measurement. The harness cannot detect it, the reviewer cannot see it, and the
result looks like a pass.

So a reuse is permitted only when the recorded binding MATCHES the current one on
every identity axis, and a mismatch names which axis moved. `--reuse` across a
revision boundary is refused, not warned about: a warning in a long script scrolls
past, and the artifact it produces outlives the terminal it was printed in.

WHY (a) NEEDS A CHECK AT ALL
----------------------------
`permissionMode: 'bypassPermissions'` disables the tool-permission prompts. On a
host with administrator credentials, an agent that decides to call the AWS CLI is
an unreviewed administrator action. The experiment is not malicious, but it is
unsupervised by construction -- that is the point of the mode.

`classify_host` therefore looks for the signals of a privileged host and refuses.
It is not a sandbox and does not pretend to be; it is a guard against the specific
mistake of running the experiment in the operator's own shell. The real isolation
is the scoped fixture (see FIXTURE-ROUTING-CONSTRAINT.md); this refuses the
obviously-wrong host before any model call is made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]

# Identity axes that must match for a reuse to be honest. Each is a (key, label)
# pair; the label is what the refusal message names so the operator knows what
# moved.
BINDING_AXES: tuple[tuple[str, str], ...] = (
    ("source_revision", "the source revision under test"),
    ("source_dirty", "whether the working tree had uncommitted changes"),
    ("sdk_version", "the Claude Agent SDK version"),
    ("permission_mode", "the SDK permission mode"),
    ("runtime_image", "the container image the experiment ran in"),
)

# raw_output_sha256 and run_nonce are NOT equality axes, and the distinction is
# substantive rather than cosmetic.
#
# A reuse happens in a NEW run, which by definition has a new nonce, so comparing
# nonces for equality would refuse every reuse -- including the honest ones the flag
# exists for. What must hold instead is:
#
#   * the recorded raw output still hashes to the digest the binding recorded (the
#     bytes have not changed since they were measured), and
#   * the recorded run nonce is PRESENT and known, so the artifact states which run
#     actually produced the evidence rather than implying it was this one.
#
# Both are verified in evaluate_reuse against the file on disk.
PROVENANCE_AXES: tuple[tuple[str, str], ...] = (
    ("raw_output_sha256", "the digest of the raw experiment output being reused"),
    ("run_nonce", "the nonce of the run that produced the raw output"),
    # A nonce alone is a unique label; these say what it is a label FOR. Root's
    # finding 4: evidence must be bound to the "actual fixture run/nonce/target", and
    # the instruction for this path specifically was "preserve original provenance on
    # reuse; no relabeling". Preservation is only meaningful if there was something to
    # preserve -- a recorded binding carrying a minted nonce and no fixture run or
    # target names nothing, so copying it forward carries nothing forward.
    ("fixture_run_id", "the fixture run whose target the reused output measured"),
    ("fixture_target", "the target (workload/queue identities) the reused output measured"),
)

# Axes on which an UNKNOWN value must never satisfy a match. Root's finding:
# "reject unknown SDK/source/image". Two unknowns are not an agreement -- they are
# two absent observations, and `None == None` is the vacuous pass this whole PR
# exists to remove. A dirty flag of None means the tree state was never determined,
# which is equally unusable.
IDENTIFYING_AXES: frozenset[str] = frozenset({
    "source_revision", "sdk_version", "runtime_image", "permission_mode",
    "raw_output_sha256", "run_nonce",
})

# Values that LOOK like an observation but are not one.
UNKNOWN_VALUES: frozenset[str] = frozenset({
    "", "unknown", "none", "null", "unspecified", "n/a", "na", "-", "undefined", "latest",
})


# Axes whose value must pin CONTENT, not merely be present and equal. An image
# reference like "myrepo/agent:latest" is a pointer: it is not unknown, and it
# compares equal to itself, but it does not identify the bytes that ran -- the same
# tag resolves to different images between the experiment and any later reuse. So
# `latest == latest` is agreement about the pointer while the code underneath is free
# to have changed, which is the vacuous pass in its most literal form.
DIGEST_PINNED_AXES: frozenset[str] = frozenset({"runtime_image"})


def identifies_content(value: Any) -> bool:
    """Does this reference pin specific bytes, rather than point at whatever is current?

    Kubernetes reports the resolved digest in `imageID`, commonly prefixed
    (``docker-pullable://repo@sha256:...``), so the digest is looked for anywhere in
    the string rather than anchored.
    """
    return isinstance(value, str) and "@sha256:" in value


def is_unknown(value: Any) -> bool:
    """Is `value` an absent observation dressed as a present one?

    ``None`` and the empty string are obvious. The string ``"unknown"`` matters
    because `current_binding` itself produces it when `git rev-parse` fails, and
    ``"latest"`` matters because a floating tag identifies no particular bytes.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in UNKNOWN_VALUES
    # An empty container is a present field with nothing in it, which is the same
    # non-observation as a missing one -- and it compares equal to another empty
    # container, so without this `{} == {}` would read as agreement about a target
    # neither side ever recorded.
    if isinstance(value, (dict, list, tuple)):
        return not value
    return False


def names_a_target(value: Any) -> bool:
    """Does this fixture_target identify at least one resource that was measured?

    ``{"k8s": [], "queues": []}`` is a populated dict whose every list is empty: it
    has the shape of a target and the content of none. Shape is not identity, which
    is the same distinction root drew about the token file and the role name.
    """
    if not isinstance(value, dict) or not value:
        return False
    return any(isinstance(v, (list, tuple)) and v for v in value.values())


@dataclass
class HostVerdict:
    """Whether this host may run an unsupervised bypassPermissions experiment."""

    allowed: bool
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


# The service-account token a pod receives. Its presence is a fact about the
# process's mount namespace rather than a claim in its environment.
# W2_SA_DIR relocates the service-account directory for TESTS only, so a test never
# needs the real mount to exist on the host running it (root's finding: the previous
# shell tests "rely on /var/run/secrets/.../namespace existing on developer host",
# and the fix must be an injected seam, not fabricated credentials on a real host).
#
# It cannot widen what is admitted. Every value read from here is compared against the
# expected-identity document, which is written by the provisioner OUTSIDE this process
# -- so pointing this at a directory of chosen files changes only which values are
# offered, never which are accepted.
SA_DIR = os.environ.get("W2_SA_DIR") or "/var/run/secrets/kubernetes.io/serviceaccount"
SA_TOKEN_PATH = f"{SA_DIR}/token"
SA_NAMESPACE_PATH = f"{SA_DIR}/namespace"

# THE POD'S OWN UID, projected by the fixture's pod spec through a downwardAPI
# volume. This replaces an earlier `{SA_DIR}/service-account.name` check that could
# never have worked: root confirmed a downwardAPI volume fieldRef supports only
# annotations, labels, name, namespace and uid -- NOT spec.serviceAccountName -- and
# no service-account mount provides such a file either. A check reading a file
# nothing writes is a check that always denies.
#
# metadata.uid is the right substitute because it is the one identity a process
# cannot rewrite for itself, and because it is server-assigned: a same-named pod
# created after a deletion has a different uid, so matching on name alone would
# accept a replacement.
#
# The service-account NAME is now verified on the OPERATOR's side instead
# (lib/worker_observation.py reads spec.serviceAccountName from the API server and
# records it against this uid). That split is deliberate: the protected service
# account can neither get nor list pods, and is not granted it. The container proves
# it IS this uid; everything the operator observed about this uid then follows.
POD_IDENTITY_DIR = os.environ.get("W2_POD_IDENTITY_DIR") or "/var/run/adp-w2-identity"
POD_UID_PATH = f"{POD_IDENTITY_DIR}/pod-uid"
# The pod name, as kubelet set the hostname. W2_HOSTNAME_PATH is the same test seam.
HOSTNAME_PATH = os.environ.get("W2_HOSTNAME_PATH") or "/etc/hostname"

# AWS identities that are NOT scoped enough to host an unsupervised agent. Retained
# as a SUBORDINATE signal that can deny but never admit: root demonstrated that a
# denylist of role names is defeated by any privileged role with an unremarkable
# name (`Worker`). Exact matching against the provisioned role is the real check.
ADMIN_ROLE_MARKERS = ("admin", "administrator", "poweruser", "root", "breakglass")


def _read_file_or_none(path: str) -> str | None:
    """Read a file, or None if it cannot be read. The default file-reading seam.

    Named rather than nested so tests can substitute a mapping-backed reader and
    never depend on files existing on the host running them. Root's finding: five
    shell tests "rely on /var/run/secrets/kubernetes.io/serviceaccount/namespace
    existing on developer host", and the fix must be an injected seam, NOT fabricated
    system credentials on a real host.
    """
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return None


def parse_assumed_role_arn(arn: str) -> tuple[str | None, str | None]:
    """Extract ``(account_id, role_name)`` from an STS caller ARN.

    Handles both shapes ``get-caller-identity`` returns:
      ``arn:aws:sts::123456789012:assumed-role/RoleName/session``
      ``arn:aws:iam::123456789012:role/RoleName``
    Anything else yields ``(None, None)`` -- unparsed, therefore unmatched, therefore
    denied by the caller.
    """
    parts = (arn or "").split(":")
    if len(parts) < 6:
        return None, None
    account = parts[4] or None
    resource = parts[5].split("/")
    role = resource[1] if len(resource) > 1 and resource[0] in ("assumed-role", "role") else None
    return account, role


def same_role(observed_arn: str, expected_role_arn: str) -> bool:
    """Is the caller the principal named by ``expected_role_arn``?

    Compared on (account, role name) rather than by string equality, because an
    assumed-role ARN carries a per-session suffix that the provisioned role ARN
    cannot know in advance. Both sides must parse: an unparsed ARN matches nothing.
    """
    observed = parse_assumed_role_arn(observed_arn)
    expected = parse_assumed_role_arn(expected_role_arn)
    if None in observed or None in expected:
        return False
    return observed == expected


def same_image(observed: str, expected: str) -> bool:
    """Do these two image references name the same BYTES?

    Compared on the ``sha256:`` digest alone, because kubelet reports the resolved
    image with a transport prefix (``docker-pullable://repo@sha256:...``) and may
    report a different registry alias for the same content than the provisioner
    recorded. The digest is the content identity; the prefix and repository are
    presentation. Both sides must carry one -- an unpinned side matches nothing.
    """
    def digest(value: str) -> str | None:
        if not isinstance(value, str) or "@sha256:" not in value:
            return None
        return "sha256:" + value.split("@sha256:", 1)[1].strip()

    left, right = digest(observed), digest(expected)
    return left is not None and left == right


def load_expected_identity(
    path: str | None,
    *,
    read_file: Callable[[str], str | None] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Load the AUTHORITATIVE description of the pod this experiment may run as.

    Root's findings 1 and 2, which are one defect seen from two sides:

      1. "namespace/token presence alone establishes no fixture ownership or actual
         executing identity. Reading any nonempty token file is not API
         authentication of its subject."
      2. "classify_host checks only that caller ARN does not contain
         admin/administrator/poweruser/root/breakglass. A privileged role with
         another name (e.g. Worker) passes; wrong-account/ordinary pod role also
         passes. Match exact verified scoped fixture identity/role from
         authoritative run configuration, with root-owned provisioning/policy
         verification, not role-name heuristics."

    Both say the same thing: the previous checks were SHAPE checks. "A token file
    exists", "the ARN does not contain the substring admin" -- neither names WHICH
    pod or WHICH role, so any pod in any namespace of any account with any
    sufficiently blandly-named privileged role satisfied them. A denylist of role
    names cannot work: the set of privileged names is open (root demonstrated it
    with ``Worker``), and the safe set is exactly one entry long.

    So identity is now compared against an expected-identity document produced by
    the run that PROVISIONED the fixture -- outside the SDK pod, by the root-owned
    launcher -- and handed to the experiment by path. The experiment cannot write it
    for itself: it is the reference, and a subject that authors its own reference
    has asserted nothing.

    Deliberately NOT done here: granting the SDK pod IAM or Kubernetes read
    permissions so it could look its own identity up. Root ruled that out ("No need
    to grant SDK pod IAM inspection permissions"), and it would not help -- a
    self-lookup answers "who am I", never "am I the pod that was authorised".

    Returns ``(identity, error)``. A missing path, unreadable file, bad JSON, or
    missing required field yields ``(None, reason)`` and therefore a refusal: with
    no reference there is nothing to match against, and an unmatched identity must
    never admit.
    """
    if read_file is None:
        read_file = _read_file_or_none

    if not path:
        return None, (
            "no expected-identity document was provided (W2_EXPECTED_IDENTITY). The pod this "
            "experiment may run as has to be named by the run that provisioned it, because a "
            "process cannot establish its own authorisation: a mounted token proves only that "
            "SOME service account is mounted, and a role name proves nothing at all. Without "
            "this reference there is nothing to compare against."
        )
    raw = read_file(path)
    if raw is None:
        return None, (
            f"the expected-identity document at {path} could not be read. Refusing rather than "
            "falling back to self-description."
        )
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"the expected-identity document at {path} is not valid JSON ({exc})"
    if not isinstance(doc, dict):
        return None, f"the expected-identity document at {path} must be a JSON object"
    identity = doc.get("expected_identity", doc)
    if not isinstance(identity, dict):
        return None, f"{path} has no expected_identity object"

    # pod_uid is required, not optional. It is the only axis the experiment can prove
    # with something it did not author (its projected metadata.uid), so a document
    # omitting it would leave the unforgeable check matched against nothing -- and an
    # absent expected value compared with `!=` against an absent observed value is
    # exactly the vacuous pass this whole module exists to remove.
    required = ("run_id", "account_id", "namespace", "pod_name", "pod_uid",
               "service_account", "aws_role_arn", "runtime_image")
    missing = [field_name for field_name in required if not identity.get(field_name)]
    if missing:
        return None, (
            f"the expected-identity document at {path} does not state {', '.join(missing)}. "
            "Every one of these is part of naming a single pod; an unstated field would be "
            "matched against nothing and would silently admit anything."
        )
    return identity, None


def classify_host(
    env: dict[str, str],
    *,
    run: Runner | None = None,
    read_file: Callable[[str], str | None] | None = None,
    expected_identity: dict[str, Any] | None = None,
    expected_identity_path: str | None = None,
) -> HostVerdict:
    """Refuse a host that is not an AUTHORITATIVELY identified scoped fixture.

    WHAT CHANGED AND WHY
    --------------------
    Root's finding, quoted: "two env vars are not identity".

    The previous revision admitted any host where ``W2_EXPERIMENT_FIXTURE=1`` and
    ``KUBERNETES_SERVICE_HOST`` were both set. Both are ordinary environment
    variables. Any process -- including the operator's own shell, one `export`
    away -- can present that shape, and the thing being gated is an agent running
    with tool permission prompts DISABLED. An assertion the subject controls is not
    an identity; it is a request.

    It also carried ``W2_ALLOW_UNSAFE_EXPERIMENT_HOST=i-understand-the-risk``, which
    turned the refusal into a suggestion and explicitly permitted the one thing root
    prohibited: "Never run bypassPermissions SDK experiments on root's administrator
    host." That override is REMOVED, not renamed. A guard against a specific
    catastrophic mistake cannot ship with a documented way to make the mistake.

    Admission now requires four facts the process cannot fabricate about itself:

      1. a mounted service-account token (a property of the mount namespace);
      2. a namespace file naming the namespace it actually runs in;
      3. an authoritative runtime image digest, passed in from the pod spec by the
         caller that read it from the API server (see `20-collect-pause-evidence.sh`);
      4. an AWS identity that resolves, and resolves to a SCOPED role -- not an
         administrator, and not absent.

    Every one of these is verified, and any that cannot be established is a refusal.
    Fails CLOSED throughout: an unreadable file, an unresolvable identity or a
    missing digest all deny.
    """
    signals: dict[str, Any] = {}
    reasons: list[str] = []

    if read_file is None:
        read_file = _read_file_or_none

    # ---- 0. the reference this host is measured AGAINST ----
    # Loaded first: without it nothing below can be checked for identity, only for
    # shape, and shape is exactly what root's reproductions defeated.
    if expected_identity is None:
        expected_identity, identity_error = load_expected_identity(
            expected_identity_path if expected_identity_path is not None
            else env.get("W2_EXPECTED_IDENTITY"),
            read_file=read_file,
        )
        if identity_error:
            reasons.append(identity_error)
    expected = expected_identity or {}
    signals["expected_identity_source"] = (
        expected_identity_path if expected_identity_path is not None
        else env.get("W2_EXPECTED_IDENTITY"))
    signals["expected_run_id"] = expected.get("run_id")

    # ---- 1/2. pod identity: the mounted service account, MATCHED ----
    token = read_file(SA_TOKEN_PATH)
    namespace = (read_file(SA_NAMESPACE_PATH) or "").strip()
    # The token's VALUE is never recorded or logged -- it is a live credential.
    # Only the fact of its presence is evidence, and that is all that is kept.
    signals["service_account_token_mounted"] = bool(token and token.strip())
    signals["pod_namespace"] = namespace or None

    if not (token and token.strip()):
        reasons.append(
            f"no service-account token at {SA_TOKEN_PATH}, so this process is not running as a "
            "pod. Pod identity has to come from the mount namespace, not from environment "
            "variables the process could set for itself."
        )
    if not namespace:
        reasons.append(
            f"no namespace at {SA_NAMESPACE_PATH}; the namespace this experiment runs in "
            "cannot be established."
        )
    elif expected.get("namespace") and namespace != expected["namespace"]:
        # Root's finding 1: a mounted token in an ORDINARY namespace satisfied the
        # old check. Presence is not ownership -- it has to be the right namespace.
        reasons.append(
            f"this process runs in namespace {namespace!r}, but the fixture provisioned for "
            f"this run is in {expected['namespace']!r}. A mounted service-account token proves "
            "only that some service account is mounted; it does not make this the pod that was "
            "authorised to run an unsupervised experiment."
        )

    # THE POD UID -- the identity check that replaced the unworkable
    # `service-account.name` read. This is the ONE value this process can offer that
    # it cannot have authored: kubelet writes it from metadata.uid via downwardAPI.
    #
    # Note what is and is not being proven here. Matching the uid does not prove this
    # pod's service account, image or role directly -- it proves this process is the
    # pod the OPERATOR observed, and the operator recorded those properties against
    # that uid by reading the API server (which the protected SA cannot do). So the
    # service account IS verified, on the side that can verify it.
    observed_uid = (read_file(POD_UID_PATH) or "").strip()
    signals["pod_uid"] = observed_uid or None
    expected_uid = (expected.get("pod_uid") or "").strip()
    if not observed_uid:
        reasons.append(
            f"this pod's uid could not be read from {POD_UID_PATH}. The fixture projects "
            "metadata.uid through a downwardAPI volume precisely so identity does not rest "
            "on anything the process asserts about itself; with no uid there is nothing "
            "unforgeable to match, so this cannot be shown to be the authorised pod."
        )
    elif not expected_uid:
        # Deliberately a REFUSAL and not a skipped check. load_expected_identity
        # requires pod_uid, so a document arriving without one was injected directly by
        # a caller -- and comparing an observed value against an absent expected value
        # is the vacuous pass this module exists to remove. `"" != ""` would be False,
        # which would read as agreement.
        reasons.append(
            "the expected-identity document names no pod_uid, so this pod's projected uid has "
            "nothing to be matched against. The uid is the only axis the experiment can prove "
            "with a value it did not author; unmatched, it admits any pod."
        )
    elif observed_uid != expected_uid:
        reasons.append(
            f"this pod's uid is {observed_uid!r}, but the run provisioned {expected_uid!r}. "
            "A pod NAME can be reused -- a Job that replaces a deleted pod creates a same-named "
            "one -- so the uid is what distinguishes this pod from a replacement, and this is a "
            "different pod from the one authorised to run an unsupervised experiment."
        )

    # The service account, as the OPERATOR observed it for that uid. Recorded as a
    # signal rather than re-derived here: this process has no way to read
    # spec.serviceAccountName (the token file proves a mount, not which subject it
    # belongs to) and is deliberately not granted one.
    signals["service_account_name"] = expected.get("service_account")
    signals["service_account_verified_by"] = (
        "the operator, from the API server, against pod_uid -- not self-reported: the "
        "protected service account cannot get or list pods and is not granted it"
    )

    # ---- 3. the runtime image, read from the API server by the caller ----
    # Passed in rather than read here: only the API server knows what image the
    # running container was actually started from, and a process cannot read its own
    # image digest from inside the container.
    image = (env.get("W2_RUNTIME_IMAGE") or "").strip()
    signals["runtime_image"] = image or None
    if not image:
        reasons.append(
            "W2_RUNTIME_IMAGE is not set. The caller must read the running container's "
            "image from the pod status (the API server is the only authority on what is "
            "actually running) and pass it in; an unidentified image cannot be bound to "
            "the evidence."
        )
    elif not identifies_content(image):
        reasons.append(
            f"runtime image {image!r} is not digest-pinned. A tag is mutable, so it does not "
            "identify the code that ran: the same tag can resolve to different bytes between "
            "the experiment and any later reuse."
        )
    elif expected.get("runtime_image") and not same_image(image, expected["runtime_image"]):
        # Pinned to SOME digest is not pinned to the RIGHT digest. The provisioner
        # observed the image the fixture was built from; anything else is a different
        # workload wearing the fixture's name.
        reasons.append(
            f"the observed runtime image {image!r} is not the image provisioned for this run "
            f"({expected['runtime_image']!r}). Digest-pinned to something else is still the "
            "wrong code."
        )

    # ---- 4. a scoped, resolvable AWS identity ----
    # An unsupervised agent with bypassPermissions will, if it decides to, call the
    # AWS CLI. What matters is what that call would be able to DO. Absent ambient
    # credentials is not automatically safe either -- the pod may hold an IRSA role --
    # so the identity is resolved and inspected rather than inferred from env vars.
    privileged_env = [
        name for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                          "AWS_SESSION_TOKEN")
        if env.get(name)
    ]
    signals["privileged_credential_env"] = privileged_env
    if privileged_env:
        reasons.append(
            f"administrator-shaped credentials are present in the environment "
            f"({', '.join(privileged_env)}). bypassPermissions means an agent that reaches "
            "for the AWS CLI performs an unreviewed privileged action."
        )

    arn = None
    if run is not None:
        identity = run(["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"])
        if identity.returncode == 0 and identity.stdout.strip():
            arn = identity.stdout.strip()
    signals["aws_caller_arn"] = arn
    if arn is None:
        reasons.append(
            "the AWS identity available to this experiment could not be resolved, so the "
            "blast radius of an unsupervised tool call is unknown. Refusing rather than "
            "assuming it is harmless."
        )
    else:
        # Root's finding 2, verbatim: the old check only asked whether the ARN
        # CONTAINED an administrative-looking substring, and root defeated it with
        # `arn:aws:sts::111111111111:assumed-role/Worker/session` -- wrong account,
        # unremarkable role name, host_allowed:true. A denylist cannot work here: the
        # set of privileged role names is open, while the set of permitted identities
        # for this experiment has exactly one member. So this is an ALLOWLIST of one,
        # taken from the provisioning record.
        account, role = parse_assumed_role_arn(arn)
        signals["aws_account_id"] = account
        signals["aws_role_name"] = role
        if expected.get("account_id") and account != expected["account_id"]:
            reasons.append(
                f"the AWS identity here is in account {account!r}, but this run was provisioned "
                f"in {expected['account_id']!r}. The same role name in another account is a "
                "different principal with different permissions."
            )
        if expected.get("aws_role_arn"):
            if not same_role(arn, expected["aws_role_arn"]):
                reasons.append(
                    f"the AWS identity available here ({arn}) is not the scoped fixture role "
                    f"provisioned for this run ({expected['aws_role_arn']}). Matching is exact "
                    "because a role name that merely looks unprivileged proves nothing -- root "
                    "demonstrated a role called 'Worker' passing the old substring check."
                )
        # The substring check is KEPT, as a subordinate belt-and-braces signal only.
        # It can deny but can no longer admit: an ARN that matches the provisioned
        # role and also reads as administrative means the provisioning itself is
        # wrong, which is worth refusing loudly rather than trusting the reference.
        lowered = arn.lower()
        matched = [marker for marker in ADMIN_ROLE_MARKERS if marker in lowered]
        signals["admin_role_markers_matched"] = matched
        if matched:
            reasons.append(
                f"the AWS identity available here looks administrative ({', '.join(matched)} "
                "in the caller ARN). This is the host root specifically prohibited for a "
                "bypassPermissions run."
            )

    # ---- 5. this is the provisioned POD, not merely a pod in the right namespace ----
    # The hostname IS the pod name for a Kubernetes pod (kubelet sets it), so it is a
    # property of the runtime rather than an env var the process chose. Root's finding:
    # "W2_EXPERIMENT_POD/container can currently select an unrelated pod/container
    # too" -- so the caller's selector is checked against the reference as well, and a
    # selector pointing anywhere other than the provisioned pod is a refusal rather
    # than a redirection.
    hostname = (read_file(HOSTNAME_PATH) or "").strip()
    signals["pod_name"] = hostname or None
    if expected.get("pod_name"):
        if not hostname:
            reasons.append(
                f"the pod name could not be read from {HOSTNAME_PATH}, so this process cannot be "
                f"shown to be the provisioned pod {expected['pod_name']!r}."
            )
        elif hostname != expected["pod_name"]:
            reasons.append(
                f"this process is running in pod {hostname!r}, not the provisioned "
                f"{expected['pod_name']!r}."
            )
        selector = (env.get("W2_EXPERIMENT_POD") or "").strip()
        signals["pod_selector"] = selector or None
        if selector and selector != expected["pod_name"]:
            reasons.append(
                f"W2_EXPERIMENT_POD selects pod {selector!r}, which is not the provisioned "
                f"{expected['pod_name']!r}. Evidence read from another pod describes another "
                "workload; the selector is refused rather than followed."
            )

    # W2_EXPERIMENT_FIXTURE is still RECORDED, because an operator's declaration is
    # useful context, but it is no longer evidence: it cannot admit a host on its own
    # and its absence cannot deny one that satisfies everything above.
    signals["declared_fixture"] = env.get("W2_EXPERIMENT_FIXTURE") == "1"

    if reasons:
        return HostVerdict(False, reasons, signals)
    return HostVerdict(
        True,
        [
            f"this is pod {hostname} (uid {observed_uid}) in namespace {namespace}, matching "
            f"the fixture provisioned for run {expected.get('run_id')} -- the uid is projected "
            f"by the kubelet, so it is the pod the operator observed as service account "
            f"{expected.get('service_account')} running the provisioned digest-pinned image "
            f"{image}, with the provisioned scoped AWS identity ({arn}) and no ambient "
            "administrator credentials"
        ],
        signals,
    )


def sha256_file(path: str) -> str | None:
    """Digest a raw artifact's BYTES, or None if it cannot be read.

    The digest is what ties a binding to a specific artifact. Without it, a binding
    describes a revision and an SDK but names no particular output, so any raw file
    on disk can be presented as the evidence it vouches for -- including one from a
    different run, or one edited after the fact.
    """
    import hashlib

    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


def current_binding(
    *,
    run: Runner,
    repo_root: str,
    sdk_version: str,
    permission_mode: str,
    runtime_image: str | None,
    env: dict[str, str] | None = None,
    raw_output_path: str | None = None,
    run_nonce: str | None = None,
    expected_identity_path: str | None = None,
    read_file: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Capture the identity axes of the code about to be exercised.

    `source_dirty` is an axis in its own right, not a footnote: evidence produced
    from an uncommitted tree cannot be reproduced from the recorded revision, so a
    later reuse against the same SHA would be binding to code nobody can recover.

    `raw_output_path` and `run_nonce` close root's fourth finding: "bind raw artifact
    digest plus original run nonce". A binding that names neither identifies no
    particular output and no particular run, so `--reuse` could attach yesterday's
    raw file -- or any file -- to today's binding and the artifact would look
    identical to a real measurement. The digest pins the BYTES; the nonce pins the
    RUN that produced them, which a digest alone cannot (the same deterministic
    output could be produced by a run that was never authorised).
    """
    env = dict(env if env is not None else os.environ)

    revision = "unknown"
    result = run(["git", "-C", repo_root, "rev-parse", "HEAD"])
    if result.returncode == 0 and result.stdout.strip():
        revision = result.stdout.strip()

    dirty: bool | None = None
    status = run(["git", "-C", repo_root, "status", "--porcelain"])
    if status.returncode == 0:
        dirty = bool(status.stdout.strip())

    expected, _ = load_expected_identity(
        expected_identity_path if expected_identity_path is not None
        else env.get("W2_EXPECTED_IDENTITY"),
        read_file=read_file,
    )
    host = classify_host(env, run=run, read_file=read_file, expected_identity=expected,
                         expected_identity_path=expected_identity_path)

    return {
        # The reference this run was measured against, recorded so a reviewer can see
        # WHICH provisioned fixture the evidence claims to come from rather than only
        # that some check passed.
        "expected_identity": expected,
        "source_revision": revision,
        "source_dirty": dirty,
        "sdk_version": sdk_version,
        "permission_mode": permission_mode,
        "runtime_image": runtime_image,
        # None when no raw output was named. Deliberately NOT defaulted to a
        # placeholder string: is_unknown() must be able to tell that nothing was
        # observed, and a placeholder would match another placeholder.
        "raw_output_path": raw_output_path,
        "raw_output_sha256": sha256_file(raw_output_path) if raw_output_path else None,
        "run_nonce": run_nonce,
        "host_allowed": host.allowed,
        "host_signals": host.signals,
        "host_reasons": host.reasons,
    }


def compare_bindings(recorded: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Which identity axes moved between a recorded run and now.

    A missing recorded axis counts as a mismatch, not a match: output from before
    the axis was tracked cannot be shown to agree with it.
    """
    drift: list[dict[str, Any]] = []
    for key, label in BINDING_AXES:
        if key not in recorded:
            drift.append({
                "axis": key,
                "label": label,
                "recorded": None,
                "current": current.get(key),
                "reason": "the recorded output predates this axis being tracked, so it cannot "
                          "be shown to match",
            })
            continue
        # An UNKNOWN value on an identifying axis is a mismatch even against an
        # identical unknown. Equality of two non-observations is not agreement: with
        # `source_revision: None` on both sides the old code reported "every identity
        # axis matches" and permitted the reuse, which is the strongest possible claim
        # built from the complete absence of evidence.
        if key in IDENTIFYING_AXES:
            unknown_side = [
                name for name, value in (("recorded", recorded.get(key)), ("current", current.get(key)))
                if is_unknown(value)
            ]
            if unknown_side:
                drift.append({
                    "axis": key,
                    "label": label,
                    "recorded": recorded.get(key),
                    "current": current.get(key),
                    "reason": (
                        f"{label} is unknown on the {' and '.join(unknown_side)} side. An "
                        "unobserved value cannot establish a match, even against an identical "
                        "unobserved value: two absences are not an agreement."
                    ),
                })
                continue
        # A value that is known but does not pin content cannot establish a match
        # either, for the same reason: equality of two mutable pointers says nothing
        # about whether what they point at is the same.
        if key in DIGEST_PINNED_AXES:
            unpinned = [
                name for name, value in (("recorded", recorded.get(key)),
                                         ("current", current.get(key)))
                if not identifies_content(value)
            ]
            if unpinned:
                drift.append({
                    "axis": key,
                    "label": label,
                    "recorded": recorded.get(key),
                    "current": current.get(key),
                    "reason": (
                        f"{label} is not digest-pinned on the {' and '.join(unpinned)} side. A "
                        "tag is a mutable pointer, so two equal tags can resolve to different "
                        "bytes -- matching on it would assert agreement about code that is free "
                        "to have changed underneath."
                    ),
                })
                continue
        if recorded.get(key) != current.get(key):
            drift.append({
                "axis": key,
                "label": label,
                "recorded": recorded.get(key),
                "current": current.get(key),
                "reason": f"{label} changed",
            })
    return drift


def verify_provenance(
    recorded: dict[str, Any],
    *,
    digest_of: Callable[[str], str | None] = sha256_file,
    verify_path: str | None = None,
) -> list[dict[str, Any]]:
    """Check that the recorded output still IS the output that was measured.

    Root's finding: "bind raw artifact digest plus original run nonce". Without
    these, a binding names a revision and an SDK but no particular output -- so a
    reuse can attach any raw file to it, including one from another run or one
    edited after it was written, and the resulting artifact is indistinguishable
    from a real measurement.

    `verify_path` is the file the caller is ACTUALLY going to reuse, when that
    differs from the path recorded at measurement time (an evidence directory that
    moved, or a `--reuse` pointed at a copy). It must be digested INSTEAD of the
    recorded path, because the recorded path is not what becomes the evidence:
    digesting a good file at the old location and then consuming a different file
    verifies one artifact and ships another. That is the same substitution this
    function exists to prevent, one level up.
    """
    problems: list[dict[str, Any]] = []

    for key, label in PROVENANCE_AXES:
        if is_unknown(recorded.get(key)):
            problems.append({
                "axis": key,
                "label": label,
                "recorded": recorded.get(key),
                "current": None,
                "reason": (
                    f"{label} was never recorded, so this output is not bound to any "
                    "particular artifact or run and cannot be attributed to one now"
                ),
            })

    # A target with the right shape and no entries is not a target. Checked separately
    # from is_unknown because the failure is one level in: the field is present, the
    # dict is non-empty, and every list inside it is empty.
    target = recorded.get("fixture_target")
    if not is_unknown(target) and not names_a_target(target):
        problems.append({
            "axis": "fixture_target",
            "label": "the target (workload/queue identities) the reused output measured",
            "recorded": target,
            "current": None,
            "reason": (
                "the recorded binding carries a fixture_target that names no resource, so the "
                "reused output cannot be shown to have measured anything. Reuse must preserve "
                "the original provenance; there is none here to preserve."
            ),
        })

    path = recorded.get("raw_output_path")
    recorded_digest = recorded.get("raw_output_sha256")
    # What gets digested is the file that will BE the evidence, not the file named at
    # measurement time, whenever the caller supplies the former.
    target = verify_path if verify_path is not None else path
    if is_unknown(path):
        problems.append({
            "axis": "raw_output_path",
            "label": "the path of the raw output being reused",
            "recorded": path,
            "current": None,
            "reason": "the binding names no raw output file, so there is nothing to re-verify",
        })
    elif not is_unknown(recorded_digest):
        actual = digest_of(str(target))
        if actual is None:
            problems.append({
                "axis": "raw_output_sha256",
                "label": "the digest of the raw experiment output being reused",
                "recorded": recorded_digest,
                "current": None,
                "reason": (
                    f"the raw output being reused ({target!r}) could not be read, so the "
                    "evidence is not present to verify"
                ),
            })
        elif actual != recorded_digest:
            problems.append({
                "axis": "raw_output_sha256",
                "label": "the digest of the raw experiment output being reused",
                "recorded": recorded_digest,
                "current": actual,
                "reason": (
                    f"the file being reused ({target!r}) does not hash to the digest recorded "
                    "when the experiment was measured. These are not the bytes this binding "
                    "describes -- they are either a different run's output or the same output "
                    "edited after the fact."
                ),
            })

    return problems


def evaluate_reuse(
    recorded: dict[str, Any],
    current: dict[str, Any],
    *,
    digest_of: Callable[[str], str | None] = sha256_file,
    verify_path: str | None = None,
) -> dict[str, Any]:
    """Decide whether reusing `recorded` output is honest evidence for `current`.

    Permitted only when NOTHING moved, every identifying axis was actually OBSERVED
    on both sides, and the recorded raw output still hashes to its recorded digest.
    A partial match is not a partial pass: there is no meaningful sense in which
    evidence is 80% about the current code.
    """
    drift = compare_bindings(recorded, current)
    provenance = verify_provenance(recorded, digest_of=digest_of, verify_path=verify_path)

    if drift:
        return {
            "reuse_permitted": False,
            "drift": drift,
            "provenance_problems": provenance,
            "explanation": (
                "the recorded experiment output does not describe the current revision. "
                "Re-running is the only way to obtain evidence about it; reusing this output "
                "would produce an artifact that claims to measure code it never ran against."
            ),
        }
    if provenance:
        return {
            "reuse_permitted": False,
            "drift": [],
            "provenance_problems": provenance,
            "explanation": (
                "every identity axis matches, but the recorded output cannot be shown to BE "
                "the artifact that was measured. A binding that names no digest or no "
                "originating run vouches for any file presented to it."
            ),
        }
    if current.get("source_dirty") or recorded.get("source_dirty"):
        return {
            "reuse_permitted": False,
            "drift": [],
            "provenance_problems": [],
            "explanation": (
                "the working tree has uncommitted changes, so the recorded revision does not "
                "identify the code that ran. The binding would name a SHA nobody can reproduce."
            ),
        }
    return {
        "reuse_permitted": True,
        "drift": [],
        "provenance_problems": [],
        "explanation": (
            "every identity axis matches, was observed on both sides, and the recorded raw "
            f"output still hashes to {recorded.get('raw_output_sha256')} from run "
            f"{recorded.get('run_nonce')}"
        ),
    }


# The axes a reuse must carry forward UNCHANGED from the recorded run. Root's
# finding 4: "Preserve original provenance on reuse; no relabeling." Installing a
# freshly captured binding beside reused bytes would stamp this run's identity on
# yesterday's measurement -- the artifact would name a run that did not produce it.
PRESERVED_AXES: tuple[str, ...] = (
    "run_nonce", "fixture_run_id", "fixture_target", "raw_output_sha256",
)


def relabeling_problems(recorded: dict[str, Any], written: dict[str, Any]) -> list[str]:
    """Did the binding installed beside the reused bytes keep the original provenance?

    Checked rather than assumed: the whole value of the reuse path is that the
    provenance survives it, so a copy that lost or rewrote the nonce would leave an
    artifact claiming a measurement with nothing behind it -- the shape of every
    defect in this file.
    """
    problems: list[str] = []
    for axis in PRESERVED_AXES:
        if recorded.get(axis) != written.get(axis):
            problems.append(
                f"{axis} was relabeled on reuse: the recorded run has {recorded.get(axis)!r} "
                f"but the installed binding says {written.get(axis)!r}. A reuse must carry the "
                "original provenance forward; rewriting it attributes the measurement to a run "
                "that did not produce it."
            )
    return problems


def resolve_run_binding(
    *,
    ledger_path: str | None,
    expected: dict[str, Any] | None,
    supplied_nonce: str | None,
    read_file: Callable[[str], str | None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Take the run's identity from the FIXTURE, not from a freshly minted number.

    Root's finding 4: "Bind experiment evidence to actual fixture run/nonce/target,
    not independently minted W2_EXPERIMENT_NONCE with no ledger/target relation."

    The nonce the 20- script minted was unique, which made it look like provenance.
    It was not: it named nothing. A nonce is only provenance if it identifies
    something independently known -- and the thing that must be identified here is
    the fixture run whose target the experiment measured. Otherwise the evidence says
    "produced by run 4f3a..." and no record anywhere says what run 4f3a was, or which
    queue and workload it created. A unique label with no referent is decoration.

    So the nonce comes from the ledger the fixture wrote when it created the target,
    and the target itself is carried alongside it, so the evidence states WHAT it
    measured as well as which run produced it.

    Returns ``(fields, problems)``. Problems are refusals, not warnings.
    """
    if read_file is None:
        read_file = _read_file_or_none
    problems: list[str] = []
    fields: dict[str, Any] = {}

    if not ledger_path:
        problems.append(
            "no --ledger was given, so the run nonce could not be taken from the fixture that "
            "created the target. A minted nonce is unique but names nothing: it cannot tie this "
            "evidence to the run whose workload and queue were actually measured."
        )
        return fields, problems

    raw = read_file(ledger_path)
    if raw is None:
        problems.append(f"the fixture ledger at {ledger_path} could not be read")
        return fields, problems
    try:
        ledger = json.loads(raw)
    except json.JSONDecodeError as exc:
        problems.append(f"the fixture ledger at {ledger_path} is not valid JSON ({exc})")
        return fields, problems
    if not isinstance(ledger, dict):
        problems.append(f"the fixture ledger at {ledger_path} must be a JSON object")
        return fields, problems

    nonce = ledger.get("run_nonce")
    run_id = ledger.get("run_id")
    if is_unknown(nonce):
        problems.append(f"the fixture ledger at {ledger_path} records no run_nonce")
    if is_unknown(run_id):
        problems.append(f"the fixture ledger at {ledger_path} records no run_id")

    # A nonce passed in must AGREE with the ledger rather than override it. Silently
    # preferring the argument would restore exactly the defect: the caller's number
    # winning over the fixture's record.
    if supplied_nonce and not is_unknown(nonce) and supplied_nonce != nonce:
        problems.append(
            f"the supplied run nonce {supplied_nonce!r} is not the fixture run's nonce "
            f"({nonce!r}). The evidence must be bound to the run that created the target it "
            "measured, so a conflicting nonce is refused rather than preferred."
        )

    # And the ledger must be the one for the run that was PROVISIONED, or the
    # experiment measured a target belonging to some other run.
    if expected and expected.get("run_id") and not is_unknown(run_id) \
            and run_id != expected["run_id"]:
        problems.append(
            f"the ledger describes run {run_id!r} but the provisioned identity is for "
            f"{expected['run_id']!r}. These are different fixture runs; evidence from one does "
            "not describe the target of the other."
        )

    # The ACCOUNT. The same run id and the same resource names exist in any account,
    # so a ledger written against a different one describes different objects. Root
    # required this axis explicitly ("Refuse ... foreign accounts"): ownership.py
    # already refuses to LOAD a ledger from another account, but that check runs in
    # the operator's own session, and nothing re-established it here, where the
    # ledger arrives as a path from outside.
    ledger_account = ledger.get("account_id")
    if is_unknown(ledger_account):
        problems.append(
            f"the fixture ledger at {ledger_path} records no account_id, so the objects it "
            "names cannot be placed in an account. A resource name is only unique within one."
        )
    elif expected and expected.get("account_id") and ledger_account != expected["account_id"]:
        problems.append(
            f"the ledger was written against account {ledger_account!r} but the provisioned "
            f"identity is in {expected['account_id']!r}. The same run id and the same resource "
            "names exist in both; these are different objects, and evidence from one does not "
            "describe the other."
        )

    fields["run_nonce"] = nonce
    fields["fixture_run_id"] = run_id
    fields["fixture_account_id"] = ledger_account
    # The TARGET, so the evidence names what it measured and not merely when.
    # Carried as identities, never as bare names: a name is reusable, a uid is not.
    entries = ledger.get("k8s") or []
    fields["fixture_target"] = {
        "k8s": [{"kind": e.get("kind"), "name": e.get("name"),
                 "namespace": e.get("namespace"), "uid": e.get("uid")}
                for e in entries],
        "queues": [{"name": e.get("name"), "url": e.get("url")}
                   for e in (ledger.get("queues") or [])],
    }
    if not fields["fixture_target"]["k8s"] and not fields["fixture_target"]["queues"]:
        problems.append(
            f"the fixture ledger at {ledger_path} records no created resources, so there is no "
            "target for this experiment to have measured."
        )

    # Every recorded object must actually be identified. Root's executed
    # counter-example was `k8s: [{}]`: a single empty entry made the list non-empty,
    # so the "records no created resources" check above passed while the target named
    # nothing at all. A list of anonymous entries is the same evidence as an empty
    # list, dressed to look like more.
    anonymous = [i for i, e in enumerate(entries)
                 if is_unknown(e.get("kind")) or is_unknown(e.get("name"))
                 or is_unknown(e.get("namespace")) or is_unknown(e.get("uid"))]
    if anonymous:
        problems.append(
            f"the fixture ledger at {ledger_path} has {len(anonymous)} k8s entr"
            f"{'y' if len(anonymous) == 1 else 'ies'} (index {anonymous}) missing kind, name, "
            "namespace or uid. An entry that does not identify an object contributes no target: "
            "a non-empty list of anonymous entries is the same evidence as an empty one."
        )

    # THE NAMED TARGET: the ledger must name the very pod the experiment ran in, and
    # the Job this run created it from.
    #
    # This is the gap root kept reproducing. The checks above prove the ledger and the
    # identity document agree about the run, the nonce and the account -- but a ledger
    # listing only an unrelated ConfigMap, or a workload in some other namespace,
    # satisfied all of them. The evidence then said "measured run X's target" while
    # naming no object the experiment ever touched. Agreement about labels is not a
    # binding to a workload.
    #
    # Matching is by SERVER-ASSIGNED UID, never by name: a Job recreates a same-named
    # pod after a deletion, so a name match accepts the replacement. Comparing the
    # observer's uid to the uid recorded AT CREATION is what detects a replacement
    # between creation and observation -- the two reads are independent, and until now
    # nothing compared them.
    def _uids_of(kind: str) -> set[str]:
        return {e.get("uid") for e in entries
                if e.get("kind") == kind and not is_unknown(e.get("uid"))}

    if expected and expected.get("pod_uid"):
        pod_uids = _uids_of("Pod")
        if expected["pod_uid"] not in pod_uids:
            problems.append(
                f"the fixture ledger at {ledger_path} does not record the Pod the experiment "
                f"ran in (uid {expected['pod_uid']}). Pods recorded: "
                f"{sorted(pod_uids) or 'none'}. Without it this evidence names no workload it "
                "measured: a ledger that agrees about the run id while listing only other "
                "objects describes the run, not the thing observed."
            )
        else:
            fields["fixture_target"]["bound_pod_uid"] = expected["pod_uid"]

    if expected and expected.get("job_uid"):
        job_uids = _uids_of("Job")
        if expected["job_uid"] not in job_uids:
            problems.append(
                f"the observed Job uid {expected['job_uid']} is not the uid this run recorded "
                f"when it CREATED the Job. Jobs recorded: {sorted(job_uids) or 'none'}. A "
                "same-named Job deleted and recreated between creation and observation is a "
                "different object, and the pod observed under it was never the one this run "
                "admitted."
            )
        else:
            fields["fixture_target"]["bound_job_uid"] = expected["job_uid"]

    return fields, problems


def binding_problems(binding: dict[str, Any], *, require_output: bool) -> list[str]:
    """Why this binding cannot stand as the identity of a real experiment run.

    Root's finding 3: "capture ALWAYS writes binding and returns0, including
    source_revision=unknown, source_dirty=true/None, host_allowed=false, empty/raw
    digest. Live 20script checks host before capture but NEVER validates clean/known
    pre-run source axes before spending; post-run capture likewise exits0. Reject
    unknown/dirty identity before running and verify post-run axes/bytes before
    admitting output. The current unknown refusal exists for reuse only."

    That last sentence is the whole defect. All the rigour about unknown and unpinned
    axes lived in `compare_bindings`, which only ever runs on the `--reuse` path. The
    LIVE path -- the one that spends money and produces the evidence everything else
    is derived from -- wrote whatever it found and exited 0. A run whose revision was
    `unknown` on a dirty tree with `host_allowed: false` produced a binding that
    looked structurally identical to a good one, and the refusal only arrived later,
    if anyone ever tried to reuse it.

    So the same standard is applied at capture time, in both directions:

      * PRE-RUN (`require_output=False`): refuse before spending. An unknown revision
        or a dirty tree means the evidence could never be attributed to recoverable
        code, so the money should not be spent at all -- and `host_allowed: false`
        must not be merely recorded next to a run that happened anyway.
      * POST-RUN (`require_output=True`): additionally require that the bytes were
        observed. A binding with no digest names no artifact, so the output it
        vouches for is unidentified.

    Returns a list of reasons; empty means admissible.
    """
    problems: list[str] = []

    revision = binding.get("source_revision")
    if is_unknown(revision):
        problems.append(
            f"the source revision is {revision!r}: the code under test was never identified, so "
            "nothing this run produces could be attributed to a recoverable revision."
        )
    dirty = binding.get("source_dirty")
    if dirty is None:
        problems.append(
            "whether the working tree is clean was never determined (source_dirty is null), so "
            "the recorded revision cannot be shown to describe the code that actually ran."
        )
    elif dirty:
        problems.append(
            "the working tree has uncommitted changes, so the recorded revision names a state "
            "nobody can reproduce. Evidence from it would cite a SHA that never contained it."
        )
    for axis in ("sdk_version", "permission_mode", "runtime_image"):
        if is_unknown(binding.get(axis)):
            problems.append(f"{axis} was not observed ({binding.get(axis)!r})")
    image = binding.get("runtime_image")
    if not is_unknown(image) and not identifies_content(image):
        problems.append(
            f"runtime_image {image!r} is a mutable tag, not a digest, so it does not identify "
            "the bytes that ran."
        )
    if binding.get("host_allowed") is not True:
        problems.append(
            "the host was not admitted (host_allowed is not true). Recording this next to a run "
            "that proceeded anyway is how an unsupervised bypassPermissions experiment ends up "
            "on a host nothing verified."
        )
    if is_unknown(binding.get("run_nonce")):
        problems.append(
            "no run nonce was supplied, so the output cannot name the run that produced it."
        )
    # The binding must name the workload it measured, not merely the run it belongs
    # to. `resolve_run_binding` refuses when the ledger fails to record the observed
    # pod, but a binding assembled by some other path could omit `fixture_target`
    # entirely and reach here with every other axis satisfied -- which is the shape
    # root's `k8s: [{}]` counter-example produced. An absent target compared against
    # nothing is the vacuous pass this module exists to remove.
    target = binding.get("fixture_target")
    if not isinstance(target, dict) or not names_a_target(target):
        problems.append(
            "the binding names no fixture target, so it does not state WHAT was measured. A run "
            "id says when; only the target says which workload."
        )
    elif not target.get("bound_pod_uid"):
        problems.append(
            "the binding's target records no bound_pod_uid, so the evidence is not tied to the "
            "pod the experiment ran in. A target listing other objects of the same run describes "
            "the fixture, not the thing observed."
        )
    if require_output:
        if is_unknown(binding.get("raw_output_sha256")):
            problems.append(
                "the raw output's digest was not recorded, so this binding names no particular "
                "artifact and would vouch for any file presented to it."
            )
        if not binding.get("raw_output_path"):
            problems.append("no raw output path was recorded")
    return problems


def write_atomic(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".binding-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _real_runner(argv: Sequence[str]) -> CommandResult:
    import subprocess

    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=60)
    except Exception as exc:  # pragma: no cover
        return CommandResult(1, "", str(exc))
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="capture or verify the identity binding of an SDK experiment")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-host", help="refuse an unsafe bypassPermissions host")
    p.add_argument("--out")
    p.add_argument("--expected-identity", default=None,
                   help="path to the expected-identity document written by the run that "
                        "PROVISIONED the fixture. Required: this process cannot establish its "
                        "own authorisation, so there must be an external reference to match "
                        "against. Defaults to $W2_EXPECTED_IDENTITY.")

    p = sub.add_parser("capture", help="write the current binding")
    p.add_argument("--repo-root", required=True)
    p.add_argument("--sdk-version", required=True)
    p.add_argument("--permission-mode", default="bypassPermissions")
    p.add_argument("--runtime-image", default=None)
    p.add_argument("--raw-output", default=None,
                   help="the raw experiment output this binding vouches for; its bytes are "
                        "digested so a later reuse can prove it is the same artifact")
    p.add_argument("--run-nonce", default=None,
                   help="nonce of the run producing the output, so a reuse names the run that "
                        "actually measured it rather than implying it was the reusing run")
    p.add_argument("--expected-identity", default=None,
                   help="the provisioning record for this fixture run. Also supplies the "
                        "authoritative run_id/run_nonce the evidence is bound to.")
    p.add_argument("--ledger", default=None,
                   help="the fixture ownership ledger. The run nonce is taken from HERE rather "
                        "than minted, so the evidence is bound to the fixture run that created "
                        "the target rather than to a number this process invented.")
    p.add_argument("--require-output", action="store_true",
                   help="post-run mode: additionally require that the raw output's bytes were "
                        "digested, so the binding names a particular artifact")
    p.add_argument("--allow-inadmissible", action="store_true",
                   help="write the binding and exit 0 even when it is not admissible as the "
                        "identity of a real run. For diagnostics ONLY: the artifact is stamped "
                        "admissible:false and no evidence may be derived from it.")
    p.add_argument("--out", required=True)

    p = sub.add_parser(
        "verify-preserved",
        help="confirm a reuse carried the recorded provenance forward unchanged")
    p.add_argument("--recorded", required=True, help="the binding from the recorded run")
    p.add_argument("--written", required=True,
                   help="the binding that was installed beside the reused bytes")

    p = sub.add_parser("verify-reuse", help="refuse a reuse that crosses a revision boundary")
    p.add_argument("--recorded", required=True, help="binding JSON from the recorded run")
    p.add_argument("--verify-raw", default=None,
                   help="the raw output file that will actually be reused, when it is not at "
                        "the path recorded at measurement time. Digested INSTEAD of the "
                        "recorded path, so the bytes verified are the bytes consumed.")
    p.add_argument("--repo-root", required=True)
    p.add_argument("--sdk-version", required=True)
    p.add_argument("--permission-mode", default="bypassPermissions")
    p.add_argument("--runtime-image", default=None)
    p.add_argument("--out")

    args = parser.parse_args(argv)

    if args.command == "check-host":
        verdict = classify_host(dict(os.environ), run=_real_runner,
                                expected_identity_path=args.expected_identity)
        payload = {"host_allowed": verdict.allowed, "reasons": verdict.reasons,
                   "signals": verdict.signals}
        if args.out:
            write_atomic(args.out, payload)
        if verdict.allowed:
            print("ok   host permitted for a bypassPermissions experiment")
            for reason in verdict.reasons:
                print(f"     {reason}")
            return 0
        print("FAIL: refusing to run a bypassPermissions experiment on this host.",
              file=sys.stderr)
        for reason in verdict.reasons:
            print(f"  - {reason}", file=sys.stderr)
        # There is deliberately NO override to document here. The previous revision
        # offered W2_ALLOW_UNSAFE_EXPERIMENT_HOST=i-understand-the-risk, which made
        # the refusal advisory and permitted precisely what root prohibited: an
        # unsupervised bypassPermissions agent on an administrator host. A guard
        # against one catastrophic mistake cannot ship with instructions for making it.
        print("\n  Run it in a scoped in-cluster fixture: a pod with a mounted service account,\n"
              "  a digest-pinned image passed in as W2_RUNTIME_IMAGE, a scoped (non-admin) AWS\n"
              "  identity and no ambient administrator credentials. There is no override.",
              file=sys.stderr)
        return 1

    if args.command == "verify-preserved":
        def load(path: str) -> dict[str, Any]:
            with open(path) as handle:
                doc = json.load(handle)
            return doc.get("binding", doc)

        problems = relabeling_problems(load(args.recorded), load(args.written))
        if not problems:
            print("ok   the reuse preserved the recorded run's provenance")
            return 0
        print("FAIL: the reuse rewrote the recorded provenance instead of preserving it.",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    binding = current_binding(
        run=_real_runner,
        repo_root=args.repo_root,
        sdk_version=args.sdk_version,
        permission_mode=args.permission_mode,
        runtime_image=args.runtime_image,
        raw_output_path=getattr(args, "raw_output", None),
        run_nonce=getattr(args, "run_nonce", None),
        expected_identity_path=getattr(args, "expected_identity", None),
    )

    if args.command == "capture":
        # The run's identity comes from the fixture ledger, not from a minted number.
        run_fields, run_problems = resolve_run_binding(
            ledger_path=args.ledger,
            expected=binding.get("expected_identity"),
            supplied_nonce=args.run_nonce,
        )
        binding.update(run_fields)

        problems = run_problems + binding_problems(
            binding, require_output=args.require_output)
        binding["admissible"] = not problems
        binding["admissibility_problems"] = problems
        # Written either way: a refusal that leaves no record is hard to diagnose, and
        # the artifact is explicitly stamped inadmissible so nothing downstream can
        # read it as an identity. What changes is the EXIT CODE, which is what the
        # calling script branches on.
        write_atomic(args.out, binding)
        print(json.dumps(binding, indent=2, sort_keys=True))
        if not problems:
            return 0
        phase = "after" if args.require_output else "before"
        print(f"\nFAIL: this binding is not admissible as the identity of a real experiment "
              f"run ({phase} the run):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        if args.allow_inadmissible:
            print("\n  --allow-inadmissible was given, so this exits 0. The artifact is stamped\n"
                  "  admissible:false; no evidence may be derived from it.", file=sys.stderr)
            return 0
        if not args.require_output:
            print("\n  Nothing has been spent. Fix the above and re-run: an experiment whose\n"
                  "  identity is unknown produces evidence that cannot be attributed to any\n"
                  "  revision, which is worse than no evidence because it looks the same as a\n"
                  "  real measurement.", file=sys.stderr)
        return 1

    with open(args.recorded) as handle:
        recorded_doc = json.load(handle)
    recorded = recorded_doc.get("binding", recorded_doc)
    outcome = evaluate_reuse(recorded, binding, verify_path=getattr(args, "verify_raw", None))
    payload = {"recorded": recorded, "current": binding, **outcome}
    if args.out:
        write_atomic(args.out, payload)

    if outcome["reuse_permitted"]:
        print("ok   reuse permitted: every identity axis matches the recorded run")
        return 0

    print("FAIL: refusing to reuse this experiment output.", file=sys.stderr)
    print(f"  {outcome['explanation']}", file=sys.stderr)
    for item in outcome["drift"]:
        print(f"  - {item['label']}: recorded {item['recorded']!r}, now {item['current']!r}",
              file=sys.stderr)
        print(f"    {item['reason']}", file=sys.stderr)
    for item in outcome.get("provenance_problems") or []:
        print(f"  - {item['label']}: {item['reason']}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
