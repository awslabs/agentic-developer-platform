"""Owner-native revocation of a qualified, stopped grant acquisition.

No components may have started. Every cleanup mutation uses the original grant
plan and immutable identity. The resource-lifetime namespace is retained.
"""

import copy
import re

from .denied_grant import digest
from .errors import BootstrapRefused
from .inventory import inventory_from_mapping
from .registry import SqlRegistrationStore, registration_lock
from .temporary_authority import TemporaryAuthority

KIND = "bootstrap-acquisition-revoked-v1"


def _prerequisites(evidence, original, prefix, journal, require):
    """Bind the complete typed marker to the owner's qualified failure proof."""
    marker = original["prerequisite_inventory"]
    inventory = inventory_from_mapping(marker)
    require(inventory.workspace_id == journal.target.workspace_id)
    require(
        len(inventory.prerequisites) == 3
        and len(prefix) == (29 if evidence.get("reader_renewal") is True else 25)
    )
    items = {item.kind: item for item in inventory.prerequisites}
    require(
        set(items)
        == {"EksAccessEntry", "EksPublicEndpoint", "SecurityGroupRule/private-sts"}
    )
    require(
        items["EksAccessEntry"].ownership == "adp-created"
        and items["EksAccessEntry"].identifier
        == original["supervisor-entry"]["identity"]["arn"]
        and items["EksPublicEndpoint"].ownership == "adopted"
        and items["EksPublicEndpoint"].identifier == journal.target.cluster_arn
        and items["SecurityGroupRule/private-sts"].ownership == "adopted"
        and re.fullmatch(
            r"sgr-[a-f0-9]{8,17}", items["SecurityGroupRule/private-sts"].identifier
        )
    )
    require(
        marker == evidence.get("prerequisite_inventory")
        and original["phase"] == "revoking"
    )
    normalized = {"phase": "revoking", "prerequisite_inventory": marker}
    for spec in prefix:
        key = spec["key"]
        status = original[key]
        require(
            set(status) == {"phase", "identity"}
            and isinstance(status["identity"], dict)
        )
        if key == "workspace-namespace":
            normalized[key] = status
        else:
            require(status["phase"] in {"granted", "revoke_intent", "revoked"})
            normalized[key] = {
                **status,
                "phase": "revoke_intent" if spec == prefix[-1] else "granted",
            }
    require(
        evidence.get("precomponent_progress_sha256")
        == digest(normalized)
        == digest(original)
    )
    proofs = evidence.get("precomponent_failures", {})
    require(set(proofs) == {journal.generation})
    proof = proofs[journal.generation]
    source = evidence.get("precomponent_source", {})
    require(
        set(source)
        == {
            "source_image",
            "source_revision",
            "source_hashes",
            "source_pod_name",
            "source_pod_uid",
            "source_audit",
        }
    )
    expected = {
        "kind": "bootstrap-precomponent-permission-refusal-v1",
        **{
            k: source[k]
            for k in (
                "source_image",
                "source_revision",
                "source_pod_name",
                "source_pod_uid",
            )
        },
        **{
            k: evidence[k]
            for k in (
                "operation_id",
                "org_id",
                "workspace_id",
                "generation",
                "plan_sha256",
            )
        },
        "precomponent_progress_sha256": digest(normalized),
        "prerequisite_inventory_sha256": digest(marker),
        "source_hashes_sha256": digest(source["source_hashes"]),
        "source_audit_sha256": digest(source["source_audit"]),
    }
    require(all(proof.get(k) == v for k, v in expected.items()))
    require(
        all(
            re.fullmatch(r"[a-f0-9]{64}", proof.get(k, ""))
            for k in ("failure_message_sha256", "stopped_message_sha256")
        )
    )


def recover_acquisition(journal, registry, eks, kube, *, revalidate, verify_absence):
    if (
        not isinstance(registry, SqlRegistrationStore)
        or registry.store is not journal.store
    ):
        raise BootstrapRefused(
            "acquisition recovery requires the native registration store"
        )
    evidence = copy.deepcopy(revalidate())

    def require(value):
        if not value:
            raise BootstrapRefused(
                "acquisition cleanup differs from its qualified prefix"
            )

    with registration_lock(journal.store, journal.target.workspace_id):
        with journal.fenced(recovery=True):
            plan, original = journal.read_locked()
            expected = {
                "evidence_kind": KIND,
                "original_authority_expired": True,
                "issued_sessions_expired": True,
                "operation_id": journal.binding.operation_id,
                "org_id": journal.target.org_id,
                "workspace_id": journal.target.workspace_id,
                "cluster_arn": journal.target.cluster_arn,
                "generation": journal.generation,
                "claim": journal.claim,
                "plan_sha256": digest(plan),
                "acquiring_progress_sha256": digest(original),
            }
            require(all(evidence.get(k) == v for k, v in expected.items()))
            prefix = evidence.get("acquisition_prefix", [])
            require(
                len(plan["grants"])
                in ({21, 29} if evidence.get("reader_renewal") is True else {17, 25})
                and 4 <= len(prefix) <= len(plan["grants"])
                and prefix == plan["grants"][: len(prefix)]
            )
            keys = {s["key"] for s in prefix}
            marker = (
                {"prerequisite_inventory"}
                if "prerequisite_inventory" in original
                else set()
            )
            require(
                len(keys) == len(prefix) and set(original) == keys | marker | {"phase"}
            )
            require(original["phase"] in {"acquiring", "revoking"})
            if marker:
                _prerequisites(evidence, original, prefix, journal, require)
            namespace = prefix[2]
            require(
                namespace["key"] == "workspace-namespace"
                and namespace["lifetime"] == "resource"
            )
            require(
                evidence.get("retained_namespace")
                == {"grant": namespace, "progress": original[namespace["key"]]}
            )
            require(original[namespace["key"]]["phase"] in {"adopted", "granted"})
            for index, spec in enumerate(prefix):
                require(
                    spec["generation"] == journal.generation
                    and spec["cluster_arn"] == journal.target.cluster_arn
                )
                if spec == namespace:
                    continue
                status = original[spec["key"]]
                require(set(status) <= {"phase", "identity"})
                require(
                    status["phase"]
                    in {"granted", "grant_intent", "revoke_intent", "revoked"}
                )
                require(status["phase"] != "grant_intent" or index == len(prefix) - 1)
                require(spec["lifetime"] in {"temporary", "workspace"})
                require(spec["kind"] in {"eks-entry", "eks-policy", "kubernetes"})

        class Backend:
            def current(self):
                require(revalidate() == evidence)
                current_plan, progress = journal.read_locked()
                require(
                    current_plan == plan
                    and progress.get("phase") in {"acquiring", "revoking", "revoked"}
                )
                require(
                    set(progress)
                    == keys
                    | marker
                    | {"phase"}
                    | ({"complete"} if progress["phase"] == "revoked" else set())
                )
                if marker:
                    require(
                        progress["prerequisite_inventory"]
                        == original["prerequisite_inventory"]
                    )
                require(progress[namespace["key"]] == original[namespace["key"]])
                for spec in prefix:
                    key = spec["key"]
                    if spec == namespace:
                        continue
                    status, initial = progress[key], original[key]
                    require(set(status) <= {"phase", "identity"})
                    require(
                        status["phase"]
                        in {initial["phase"], "revoke_intent", "revoked"}
                    )
                    if initial.get("identity") is not None:
                        require(status.get("identity") == initial["identity"])

            def selected(self, spec):
                self.current()
                require(spec in prefix and spec != namespace)
                return eks if spec["kind"].startswith("eks-") else kube

            def observe(self, spec):
                return self.selected(spec).observe(spec)

            def verify(self, spec, identity):
                self.selected(spec).verify(spec, identity)

            def delete(self, spec, identity):
                self.selected(spec).delete(spec, identity)

            def verify_revoked(self, current_plan, progress):
                self.current()
                require(current_plan == plan)
                require(
                    all(
                        progress[s["key"]]["phase"] == "revoked"
                        for s in prefix
                        if s != namespace
                    )
                )
                # The independent owner cleanup transport remains available
                # after the original registrar entry has been removed.
                for spec in prefix:
                    if spec != namespace and spec["kind"] == "kubernetes":
                        require(kube.observe(spec) is None)
                require(verify_absence(plan) is True)
                self.current()

        backend = Backend()
        with journal.fenced(recovery=True):
            backend.current()
            observed = kube.observe(namespace)
            require(observed == original[namespace["key"]]["identity"])
            if original[namespace["key"]]["phase"] == "adopted":
                require(
                    all(
                        observed.get("labels", {}).get(k) == v
                        for k, v in namespace["body"]["metadata"]["labels"].items()
                    )
                )
            else:
                kube.verify(namespace, observed)
        TemporaryAuthority(journal, backend).revoke()

        def release_check():
            current_plan, progress = journal.read()
            require(
                progress.get("phase") == "revoked" and progress.get("complete") is True
            )
            backend.verify_revoked(current_plan, progress)
            return True

        require(
            registry.recover_claim(
                journal.target.workspace_id, journal.claim, restore=release_check
            )
            == (True, True, True)
        )
    return {
        "evidence_kind": KIND,
        "generation": journal.generation,
        "plan_sha256": evidence["plan_sha256"],
        "revoked": True,
        "claim_released": True,
        "journal_retained": True,
        "namespace_retained": True,
    }
