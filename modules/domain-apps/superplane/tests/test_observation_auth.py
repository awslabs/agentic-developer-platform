"""Authentication: an unauthenticated or unsigned submission is refused.

Issue #5043 (U8), EPIC #4910. R11 acceptance 2.

The concrete hole being closed: today's heartbeat POST carries `Content-Type` and
nothing else, and the receiver has no auth dependency, so any caller who knows a
cluster's UUID can write that cluster's state. `test_forged_cluster_state_by_uuid_alone_fails`
is that exact attack expressed as a test — a well-formed payload naming a real
cluster, with no credential — and it must refuse.

Two requirements are asserted independently, because either alone leaves a gap a
reader might think the other covers:

* **authenticated** — the receiver knows who is calling;
* **signed** — the body being authenticated is the body that was sent, so a valid
  submitter's request cannot be replayed with a swapped payload.

The resolver is a stub. That is the point of the injected `SubmitterResolver`: the
real one is U15's, upstream, and the *rule* — both requirements, fail closed — is
what this unit owns and what these tests exercise.
"""

from __future__ import annotations

import json

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from conftest import OBSERVED_AT, TEST_SIGNING_KEY, W1, W2
from superplane_contracts import (
    AUTH_HEADER,
    CONTRACT_VERSION,
    SIGNATURE_HEADER,
    VERSION_HEADER,
    CheckResult,
    CheckStatus,
    ClusterRef,
    ContractViolation,
    Observation,
    Submitter,
    canonical_body,
    compute_signature,
    verify_signature,
    verify_submission,
)

# The one credential the stub resolver accepts. Test-only: it authenticates
# nothing outside this file.
_VALID_CREDENTIAL = "Bearer valid-test-credential"


class StubResolver:
    """Resolves exactly one credential, and records what it was asked.

    Recording the calls is what lets `test_credential_is_not_consulted_when_version_is_bad`
    assert ordering — that a bad version is refused *before* a credential is
    looked at, rather than merely also being refused.
    """

    def __init__(self, submitter: Submitter) -> None:
        self._submitter = submitter
        self.calls: list[str] = []

    def resolve(self, credential: str) -> Submitter | None:
        self.calls.append(credential)
        if credential == _VALID_CREDENTIAL:
            return self._submitter
        return None


def _resolver(workspaces: frozenset[str] = frozenset({W1})) -> StubResolver:
    return StubResolver(Submitter(submitter_id="monitor-1", workspaces=workspaces))


def _headers(observation: Observation, **overrides: str | None) -> dict[str, str]:
    """A complete, valid header set, with named fields removable via overrides.

    Building the *valid* set and then removing one field per test is deliberate:
    it means a passing negative test cannot be passing because the header set was
    incidentally broken in some other way.
    """
    headers: dict[str, str | None] = {
        VERSION_HEADER: CONTRACT_VERSION,
        AUTH_HEADER: _VALID_CREDENTIAL,
        SIGNATURE_HEADER: compute_signature(observation, TEST_SIGNING_KEY),
    }
    headers.update(overrides)
    return {k: v for k, v in headers.items() if v is not None}


class TestAuthenticationRequired:
    """A submission with no accepted identity is refused."""

    def test_fully_valid_submission_is_authenticated(self, w1_observation) -> None:
        """The positive case, so the negatives below mean something.

        Without this, every negative test could pass because the valid path is
        also broken.
        """
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert result.authenticated
        assert result.submitter is not None
        assert result.submitter.submitter_id == "monitor-1"

    def test_forged_cluster_state_by_uuid_alone_fails(self, healthy_check) -> None:
        """R11 acc. 2, stated as the attack it prevents.

        This is today's endpoint exactly: a well-formed body naming a real
        cluster, sent with the content headers and nothing else. It must refuse,
        because knowing a cluster's UUID is not authority over that cluster —
        and a UUID appears in logs, kubeconfigs and support tickets.
        """
        forged = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-w1-a", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="not-really-the-monitor",
            checks=(healthy_check,),
        )
        result = verify_submission(
            canonical_body(forged),
            {"content-type": "application/json", VERSION_HEADER: CONTRACT_VERSION},
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert result.reason == "unauthenticated: no credential presented"
        assert result.submitter is None

    def test_missing_credential_is_refused(self, w1_observation) -> None:
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{AUTH_HEADER: None}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert "no credential presented" in result.reason

    def test_blank_credential_is_refused(self, w1_observation) -> None:
        """A whitespace credential is an absent one, not a present empty string."""
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{AUTH_HEADER: "   "}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert "no credential presented" in result.reason

    def test_unrecognized_credential_is_refused(self, w1_observation) -> None:
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{AUTH_HEADER: "Bearer not-a-real-credential"}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert result.reason == "unauthenticated: credential not accepted"

    def test_refusal_does_not_distinguish_unknown_submitter_from_bad_token(
        self, w1_observation
    ) -> None:
        """Both unresolvable-credential cases give the identical reason.

        Distinguishing them would let the endpoint be used to test whether a
        given submitter identity exists.
        """
        first = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{AUTH_HEADER: "Bearer wrong-signature-token"}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        second = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{AUTH_HEADER: "Bearer unknown-submitter"}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert first.reason == second.reason

    def test_refusal_never_echoes_the_credential(self, w1_observation) -> None:
        """A rejected credential must not appear in the reason.

        Otherwise a refusal writes the credential into the submitter's logs,
        which turns a failed auth into a disclosure.
        """
        secret_looking = "Bearer sk-do-not-log-this-value"
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{AUTH_HEADER: secret_looking}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert "sk-do-not-log-this-value" not in result.reason

    def test_header_lookup_is_case_insensitive(self, w1_observation) -> None:
        """HTTP header case is not significant.

        A case-sensitive match here would make the same submitter authenticate on
        one transport and fail on another, which is the per-transport divergence
        the MCP surface's authz module also guards against.
        """
        signature = compute_signature(w1_observation, TEST_SIGNING_KEY)
        result = verify_submission(
            canonical_body(w1_observation),
            {
                "X-Superplane-Contract-Version": CONTRACT_VERSION,
                "Authorization": _VALID_CREDENTIAL,
                "X-Superplane-Signature": signature,
            },
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert result.authenticated


class TestSignatureRequired:
    """An authenticated caller still has to sign the body it sent."""

    def test_missing_signature_is_refused(self, w1_observation) -> None:
        """Authenticated but unsigned: refused.

        The credential proves who started the call; without a signature the
        receiver does not know that the body is still what that caller sent.
        """
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{SIGNATURE_HEADER: None}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert result.reason == "unsigned or invalid body signature"

    def test_wrong_signature_is_refused(self, w1_observation) -> None:
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{SIGNATURE_HEADER: "sha256=" + "0" * 64}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert "signature" in result.reason

    def test_unprefixed_signature_is_refused(self, w1_observation) -> None:
        """A bare hex digest is not accepted.

        The `sha256=` prefix keeps the algorithm explicit, so an opaque value
        cannot later be mistaken for a signature of a different algorithm.
        """
        bare = compute_signature(w1_observation, TEST_SIGNING_KEY).removeprefix(
            "sha256="
        )
        assert not verify_signature(w1_observation, TEST_SIGNING_KEY, bare)

    def test_signature_from_a_different_key_is_refused(self, w1_observation) -> None:
        other = compute_signature(w1_observation, b"a-different-key")
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{SIGNATURE_HEADER: other}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated

    def test_signature_does_not_transfer_between_bodies(self, healthy_check) -> None:
        """A body swap is caught.

        The replay case that authentication alone misses: a legitimate
        submitter's signature, reused over a payload asserting something else
        about a different cluster.
        """
        original = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-a", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(healthy_check,),
        )
        swapped = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-b", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(healthy_check,),
        )
        stolen = compute_signature(original, TEST_SIGNING_KEY)

        result = verify_submission(
            canonical_body(swapped),
            _headers(swapped, **{SIGNATURE_HEADER: stolen}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated

    def test_signature_covers_the_reported_status(self, healthy_check) -> None:
        """Changing a check's outcome invalidates the signature.

        Asserted specifically because the status is the field an attacker would
        want to alter — flipping unreachable to healthy is the whole point of
        forging a fleet observation.
        """
        unreachable = CheckResult(
            name="api_server",
            status=CheckStatus.UNREACHABLE,
            observed_at=OBSERVED_AT,
            error="dial tcp: connection refused",
        )
        truthful = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-a", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(unreachable,),
        )
        flattering = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-a", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(healthy_check,),
        )
        assert compute_signature(truthful, TEST_SIGNING_KEY) != compute_signature(
            flattering, TEST_SIGNING_KEY
        )

    def test_canonical_body_is_deterministic_and_compact(self, w1_observation) -> None:
        """Canonicalization is stable and inserts no whitespace.

        A signature is only checkable if signer and verifier serialize
        identically — a Go sender's encoder and a Python receiver's do not agree
        on key order or spacing by default, so both properties are fixed here.
        """
        body = canonical_body(w1_observation)
        assert body == canonical_body(w1_observation)
        assert b", " not in body
        assert b": " not in body

    def test_canonical_body_sorts_keys_at_every_level(self, w1_observation) -> None:
        """Key order is sorted throughout, not just at the top level.

        Nested objects (`subject`, each check) are part of the signed bytes too,
        so an encoder that sorted only the outer object would still produce a
        signature the other end could not reproduce.
        """
        decoded = json.JSONDecoder(object_pairs_hook=list).decode(
            canonical_body(w1_observation).decode()
        )

        def assert_sorted(node: object) -> None:
            if isinstance(node, list) and node and isinstance(node[0], tuple):
                keys = [k for k, _ in node]
                assert keys == sorted(keys), f"unsorted keys: {keys}"
                for _, value in node:
                    assert_sorted(value)
            elif isinstance(node, list):
                for item in node:
                    assert_sorted(item)

        assert_sorted(decoded)


class TestVersionIsCheckedFirst:
    """Version discipline precedes authentication, and says so."""

    def test_version_mismatch_is_refused_even_with_a_valid_credential(
        self, w1_observation
    ) -> None:
        """Header rewritten in flight, payload untouched: refused.

        Overriding only the header is exactly what a proxy or an attacker
        rewriting one surface produces, and the two copies disagreeing is its own
        refusal — distinct from an unsupported version, asserted below.
        """
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{VERSION_HEADER: "v99"}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert result.reason == "contract version mismatch"

    def test_consistently_unsupported_version_is_refused(self, healthy_check) -> None:
        """A submitter written against a version this receiver does not implement.

        Both copies agree, so this is not a mismatch — it is a submitter the
        receiver cannot safely interpret, and it is refused rather than
        best-effort parsed.
        """
        future = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-w1-a", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="platform-monitor",
            checks=(healthy_check,),
            contract_version="v99",
        )
        result = verify_submission(
            canonical_body(future),
            _headers(future, **{VERSION_HEADER: "v99"}),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert not result.authenticated
        assert "unsupported contract version" in result.reason

    def test_credential_is_not_consulted_when_version_is_bad(
        self, w1_observation
    ) -> None:
        """Ordering, not just outcome.

        A submitter sending an unsupported version should be told that, rather
        than getting an authentication error that sends it looking in the wrong
        place — and the receiver should not spend a token verification on a
        submission it will refuse anyway.
        """
        resolver = _resolver()
        verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation, **{VERSION_HEADER: None}),
            resolver,
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert resolver.calls == []


class TestSubmitterGrant:
    """The grant comes from the resolver, never from the payload."""

    def test_resolved_grant_is_what_the_resolver_returned(self, w1_observation) -> None:
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation),
            _resolver(frozenset({W1, W2})),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert result.authenticated
        assert result.submitter is not None
        assert result.submitter.workspaces == frozenset({W1, W2})

    def test_payload_reporter_does_not_become_the_identity(self, healthy_check) -> None:
        """A submission naming itself proves nothing.

        `reporter` is a label for display; the authenticated identity is the
        resolver's answer. This asserts they are not the same value, so no
        receiver can be tempted to read authority off the payload.
        """
        claiming = Observation(
            kind="fleet_health",
            subject=ClusterRef(cluster_id="cluster-w1-a", workspace=W1),
            reported_at=OBSERVED_AT,
            reporter="monitor-with-a-flattering-name",
            checks=(healthy_check,),
        )
        result = verify_submission(
            canonical_body(claiming),
            _headers(claiming),
            _resolver(),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert result.authenticated
        assert result.submitter is not None
        assert result.submitter.submitter_id == "monitor-1"
        assert result.submitter.submitter_id != claiming.reporter

    def test_anonymous_submitter_cannot_be_constructed(self) -> None:
        """A `Submitter` with no id is refused at construction.

        An identity that names nobody would satisfy the `submitter is not None`
        check every caller makes, then carry a grant that no scoping refusal or
        audit line could attribute. Refused with `ContractViolation` like every
        other guard in the package, so a receiver catching one exception type
        catches all of them.
        """
        with pytest.raises(ContractViolation, match="submitter_id"):
            Submitter(submitter_id="  ", workspaces=frozenset({W1}))

    def test_submitter_with_no_workspaces_still_authenticates(
        self, w1_observation
    ) -> None:
        """Authentication and scoping are separate outcomes.

        A submitter with an empty grant is a real, authenticated caller who is
        authorized for nothing. Scoping is what refuses it — see
        `test_observation_scoping.py`. Keeping these separate is why neither can
        be satisfied by the other's evidence.
        """
        result = verify_submission(
            canonical_body(w1_observation),
            _headers(w1_observation),
            _resolver(frozenset()),
            TEST_SIGNING_KEY,
            now=OBSERVED_AT,
        )
        assert result.authenticated
        assert result.submitter is not None
        assert result.submitter.workspaces == frozenset()
