"""Persist component creation before I/O and retain exact identities for retirement.

The authority journal owns the reservation lock. Existing objects may be reused
unchanged, but only a prior durable creation record can confer deletion ownership.
API defaulting is allowed when verifying the submitted document; subsequent reads
must match the entire originally observed spec as well as its UID.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from .errors import BootstrapRefused

ANNOTATION = "superplane.aws-e/component-creation"
KINDS = {
    "ServiceAccount": "v1",
    "Role": "rbac.authorization.k8s.io/v1",
    "RoleBinding": "rbac.authorization.k8s.io/v1",
    "ClusterRole": "rbac.authorization.k8s.io/v1",
    "ClusterRoleBinding": "rbac.authorization.k8s.io/v1",
    "Deployment": "apps/v1",
}


def component_key(body):
    metadata = body.get("metadata", {})
    kind = body.get("kind")
    namespace = metadata.get("namespace", "")
    name = metadata.get("name")
    if (
        kind not in KINDS
        or body.get("apiVersion") != KINDS[kind]
        or not isinstance(name, str)
        or not name
        or not isinstance(namespace, str)
        or (kind.startswith("Cluster") and namespace)
        or (not kind.startswith("Cluster") and not namespace)
    ):
        raise BootstrapRefused("component document has an invalid identity")
    return json.dumps([kind, namespace, name], separators=(",", ":"))


def _contains(observed, desired):
    if isinstance(desired, dict):
        return isinstance(observed, dict) and all(
            key in observed and _contains(observed[key], value)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(observed, list)
            and len(observed) == len(desired)
            and all(_contains(a, b) for a, b in zip(observed, desired, strict=True))
        )
    return type(observed) is type(desired) and observed == desired


def component_identity(body):
    component_key(body)
    metadata = body["metadata"]
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise BootstrapRefused("component read did not establish an immutable UID")
    # ResourceVersion/status/managedFields change during normal reconciliation.
    # Spec, RBAC rules/subjects, labels and annotations remain pinned. The
    # deployment controller's revision annotation is its own bookkeeping.
    fields = {k: v for k, v in body.items() if k not in {"metadata", "status"}}
    annotations = {
        k: v
        for k, v in metadata.get("annotations", {}).items()
        if k != "deployment.kubernetes.io/revision"
    }
    fields["metadata"] = {
        "name": metadata["name"],
        "namespace": metadata.get("namespace", ""),
        "labels": metadata.get("labels", {}),
        "annotations": annotations,
        "ownerReferences": metadata.get("ownerReferences", []),
    }
    return {
        "uid": uid,
        "digest": hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "creation": annotations.get(ANNOTATION),
    }


class ComponentJournal:
    def __init__(self, authority, access):
        self.authority, self.access = authority, access
        self.journal = authority.journal

    def _active(self):
        _, progress = self.journal.read_locked()
        self.authority._require_phase(progress, "active")
        self.authority.backend.verify_worker_binding()
        return progress

    def _previous(self, key):
        rows = self.journal.store.execute(
            "SELECT progress_json FROM workspace_bootstrap_authority "
            "WHERE workspace_id=:workspace_id AND org_id=:org_id "
            "AND cluster_arn=:cluster_arn AND generation<>:generation",
            {
                **self.journal.key,
                "org_id": self.journal.target.org_id,
                "cluster_arn": self.journal.target.cluster_arn,
            },
        )
        records = []
        for row in rows:
            progress = json.loads(row["progress_json"])
            # Shared bootstrap recovery may positively remove this exact member
            # component before releasing its claim. A new attempt creates a new
            # UID; it must not adopt a deleted predecessor's credential identity.
            if (
                progress.get("phase") == "revoked"
                and progress.get("complete")
                and progress.get("component_cleanup", {}).get(key) == "absent"
            ):
                continue
            previous = progress.get("components", {}).get(key)
            if previous:
                records.append(previous)
        return merge_component_records(records)

    def ensure(self, desired):
        body = deepcopy(desired)
        key = component_key(body)
        if ANNOTATION in body["metadata"].get("annotations", {}):
            raise BootstrapRefused("component creation marker is service-owned")
        namespace = body["metadata"].get("namespace")
        if namespace and namespace != self.authority.backend.release.namespace:
            raise BootstrapRefused("component targets another namespace")
        with self.journal.fenced():
            progress = self._active()
            components = progress.setdefault("components", {})
            previous = components.get(key) or self._previous(key)
            if previous:
                if previous["desired"] != body:
                    raise BootstrapRefused("component release changed during bootstrap")
                components[key] = previous
                self.journal.write_locked(progress)
            else:
                observed = self.access.read_component(body)
                if observed is not None:
                    self._matches(observed, body)
                    components[key] = {
                        "desired": body,
                        "phase": "adopted",
                        "identity": component_identity(observed),
                    }
                    self.journal.write_locked(progress)
                    return deepcopy(components[key])
                # Committed in its own transaction before create. The create
                # never overwrites an object that appeared after this read.
                components[key] = {
                    "desired": body,
                    "phase": "intended",
                    "creation": uuid4().hex,
                }
                self.journal.write_locked(progress)
        with self.journal.fenced():
            progress = self._active()
            record = progress["components"][key]
            observed = self.access.read_component(body)
            if record["phase"] in {"owned", "adopted"}:
                if (
                    observed is None
                    or component_identity(observed) != record["identity"]
                ):
                    raise BootstrapRefused(
                        "recorded component identity or specification changed"
                    )
                self._matches(observed, body)
                return deepcopy(record)
            if record["phase"] != "intended":
                raise BootstrapRefused("component ownership phase is invalid")
            if observed is None:
                submitted = deepcopy(body)
                submitted["metadata"].setdefault("annotations", {})[ANNOTATION] = (
                    record["creation"]
                )
                observed = self.access.create_component(submitted)
            identity = component_identity(observed)
            if identity["creation"] != record["creation"]:
                raise BootstrapRefused("component creation outcome is ambiguous")
            self._matches(observed, body)
            record.update(phase="owned", identity=identity)
            self.journal.write_locked(progress)
            return deepcopy(record)

    @staticmethod
    def _matches(observed, desired):
        if component_key(observed) != component_key(desired) or not _contains(
            observed, desired
        ):
            raise BootstrapRefused(
                "existing component differs from the selected release"
            )
        if desired["kind"] in {"Role", "ClusterRole"} and (
            observed.get("rules") != desired.get("rules")
            or observed.get("aggregationRule")
            or any(
                k.startswith("rbac.authorization.k8s.io/aggregate-to-")
                for k in observed["metadata"].get("labels", {})
            )
        ):
            raise BootstrapRefused("existing component has different RBAC authority")


def merge_component_records(records):
    """A lost create reply may become owned in a later journal, never another UID."""
    if not records:
        return None
    records = sorted(records, key=lambda r: r.get("phase") == "intended")
    selected = records[0]
    if selected.get("phase") not in {"intended", "owned", "adopted"}:
        raise BootstrapRefused("component ownership phase is invalid")
    for record in records:
        if (
            record.get("desired") != selected.get("desired")
            or record.get("creation") != selected.get("creation")
            or (record.get("phase") != "intended" and record != selected)
            or (record.get("phase") == "intended" and not record.get("creation"))
        ):
            raise BootstrapRefused(
                "component ownership differs across bootstrap attempts"
            )
    return deepcopy(selected)


def expected_component_keys(namespace, service_account, controller=None):
    role = service_account + "-workspace"
    cluster_role = service_account + "-" + namespace + "-cluster"
    return {
        json.dumps([kind, ns, name], separators=(",", ":"))
        for kind, ns, name in (
            ("ServiceAccount", namespace, service_account),
            ("Role", namespace, role),
            ("RoleBinding", namespace, role),
            ("ClusterRole", "", cluster_role),
            ("ClusterRoleBinding", "", cluster_role),
            *(([("Deployment", namespace, controller)]) if controller else []),
        )
    }
