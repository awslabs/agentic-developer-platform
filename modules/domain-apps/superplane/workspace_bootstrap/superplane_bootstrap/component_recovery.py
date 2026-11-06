"""Resume failed-path grant revocation while retaining the original components.

The owner supplies separately qualified source/expiry evidence and pinned native
read/delete transports. This composition never creates grants, adopts components,
changes their documents, removes the namespace, or admits tenant workloads.
"""

import copy
from contextlib import nullcontext

from .component_journal import _contains, component_identity, component_key
from .denied_grant import digest
from .errors import BootstrapRefused
from .registry import SqlRegistrationStore, registration_lock
from .temporary_authority import TemporaryAuthority

KIND = "bootstrap-reader-components-revoked-v1"


def recover_components(
    journal, registry, eks, kube, *, revalidate, verify_absence, read_component
):
    if (
        not isinstance(registry, SqlRegistrationStore)
        or registry.store is not journal.store
    ):
        raise BootstrapRefused(
            "component recovery requires the native registration store"
        )
    evidence = copy.deepcopy(revalidate())

    def require(value):
        if not value:
            raise BootstrapRefused(
                "component cleanup differs from its qualified source"
            )

    from harness_jobs import settled_bootstrap_repeat

    closure = settled_bootstrap_repeat.validate(evidence["settled_bootstrap"])
    previous = settled_bootstrap_repeat.component_anchor(closure)
    with registration_lock(journal.store, journal.target.workspace_id):
        with journal.fenced(recovery=True):
            plan, original = journal.read_locked()
            completed = original.get("phase") == "revoked"
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
                (
                    "revoked_progress_sha256"
                    if completed
                    else "revoking_progress_sha256"
                ): digest(original),
                "retained_plan": plan,
                "retained_progress": original,
            }
            require(all(evidence.get(k) == v for k, v in expected.items()))
            require(
                original.get("phase") in {"revoking", "revoked"}
                and "retain_workspace" not in original
            )
            require(not completed or original.get("complete") is True)
            require(len(plan["grants"]) == 29)
            grants = plan["grants"]
            keys = {s["key"] for s in grants}
            namespace = grants[2]
            require(
                namespace["key"] == "workspace-namespace"
                and namespace["lifetime"] == "resource"
            )
            require(original[namespace["key"]]["phase"] == "adopted")
            require(
                original[namespace["key"]]["identity"]
                == previous["retained_progress"][namespace["key"]]["identity"]
            )
            retained = previous["retained_progress"]["components"]
            partial = "inherited_components" in evidence
            require(len(retained) == 5)
            if partial:
                require(evidence["inherited_components"] == retained)
                require(
                    isinstance(original.get("components"), dict)
                    and 1 <= len(original["components"]) <= 5
                )
                require(
                    all(
                        k in retained and v == retained[k]
                        for k, v in original["components"].items()
                    )
                )
            else:
                require(
                    original.get("component_inventory_complete") is True
                    and original.get("component_inventory_mode") == "management"
                )
                require(original.get("components") == retained)
            markers = {
                k: v
                for k, v in original.items()
                if k not in keys | {"phase", "complete"}
            }
            require(
                set(markers)
                == (
                    {"components", "prerequisite_inventory"}
                    if partial
                    else {
                        "components",
                        "component_inventory_complete",
                        "component_inventory_mode",
                        "management_observation",
                        "prerequisite_inventory",
                        "system_workload_baseline",
                    }
                )
            )
            require(
                set(original)
                == keys
                | set(markers)
                | {"phase"}
                | ({"complete"} if completed else set())
            )
            for spec in grants:
                require(
                    spec["generation"] == journal.generation
                    and spec["cluster_arn"] == journal.target.cluster_arn
                )
                if spec == namespace:
                    continue
                status = original[spec["key"]]
                require(
                    set(status) == {"phase", "identity"} and bool(status["identity"])
                )
                require(status["phase"] in {"granted", "revoke_intent", "revoked"})
                require(not completed or status["phase"] == "revoked")
                require(
                    spec["lifetime"] in {"temporary", "workspace"}
                    and spec["kind"] in {"eks-entry", "eks-policy", "kubernetes"}
                )

        def reads(bodies):
            batch = getattr(kube, "read_batch", None)
            return batch(bodies) if batch is not None else nullcontext()

        def retained_objects():
            bodies = [namespace["body"]] + [
                record["desired"] for record in retained.values()
            ]
            with reads(bodies):
                require(
                    kube.observe(namespace) == original[namespace["key"]]["identity"]
                )
                for key, record in retained.items():
                    require(record["phase"] == "owned")
                    observed = read_component(record["desired"])
                    require(
                        isinstance(observed, dict) and component_key(observed) == key
                    )
                    require(_contains(observed, record["desired"]))
                    require(component_identity(observed) == record["identity"])

        class Backend:
            def current(self):
                require(revalidate() == evidence)
                current_plan, progress = journal.read_locked()
                require(
                    current_plan == plan
                    and progress.get("phase") in {"revoking", "revoked"}
                )
                require(
                    set(progress)
                    == keys
                    | set(markers)
                    | {"phase"}
                    | ({"complete"} if progress["phase"] == "revoked" else set())
                )
                require(all(progress.get(k) == v for k, v in markers.items()))
                require(progress[namespace["key"]] == original[namespace["key"]])
                for spec in grants:
                    if spec == namespace:
                        continue
                    key = spec["key"]
                    current, initial = progress[key], original[key]
                    require(set(current) == {"phase", "identity"})
                    require(current["identity"] == initial["identity"])
                    allowed = (
                        {"revoked"}
                        if initial["phase"] == "revoked"
                        else {initial["phase"], "revoke_intent", "revoked"}
                    )
                    require(current["phase"] in allowed)

            def selected(self, spec):
                self.current()
                require(spec in grants and spec != namespace)
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
                        for s in grants
                        if s != namespace
                    )
                )
                selected = [
                    spec
                    for spec in grants
                    if spec != namespace and spec["kind"] == "kubernetes"
                ]
                # Both native verification passes still reread the whole roster.
                # The sealed transport brackets each finite read-only batch with
                # fresh authority; original per-object deletion fencing remains.
                with reads([spec["body"] for spec in selected]):
                    for spec in selected:
                        require(kube.observe(spec) is None)
                require(verify_absence(plan) is True)
                retained_objects()
                self.current()

        backend = Backend()
        with journal.fenced(recovery=True):
            backend.current()
            retained_objects()
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
        "components_retained": True,
    }
