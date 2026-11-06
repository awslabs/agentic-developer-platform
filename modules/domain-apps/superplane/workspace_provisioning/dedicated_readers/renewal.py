"""CAS publication of bounded reader tokens; no worker or operator credentials.

Only the exact installed Secret/key is writable. A receipt in its annotations
binds the public token digest, native journal generation and immutable SA UID.
The receipt and token change atomically. Uncertain writes are observed once and
never blindly replayed. Token bytes are neither logged nor written to scratch.
"""

import base64
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta


class ReaderRefused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ReaderRefused(message)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def timestamp(value):
    value = datetime.fromisoformat(value)
    require(value.tzinfo is not None, "Reader timestamp requires a timezone")
    return value


def binding(target):
    return {
        key: target[key]
        for key in (
            "workspace_id",
            "org_id",
            "cluster_arn",
            "endpoint",
            "namespace",
            "namespace_uid",
            "service_account",
            "service_account_uid",
            "generation",
            "operation_id",
            "registration_claim",
        )
    }


def readonly(rules):
    if rules.get("incomplete") or rules.get("evaluationError"):
        return False
    for rule in rules.get("resourceRules", []):
        groups, resources = rule.get("apiGroups", []), rule.get("resources", [])
        observation_resources = {
            "": {"nodes", "namespaces", "pods", "pods/log"},
            "apps": {"deployments", "replicasets"},
            "batch": {"jobs"},
            "superplane.ai": {"nodepools", "superplanenodes"},
        }
        review = (
            groups == ["authorization.k8s.io"]
            and set(resources)
            <= {"selfsubjectaccessreviews", "selfsubjectrulesreviews"}
        ) or (
            groups == ["authentication.k8s.io"] and resources == ["selfsubjectreviews"]
        )
        if not review and any(
            resource not in observation_resources.get(group, set())
            for group in groups
            for resource in resources
        ):
            return False
        if any(
            v not in ({"create"} if review else {"get", "list", "watch"})
            for v in rule.get("verbs", [])
        ):
            return False
    return all(
        set(rule.get("verbs", [])) <= {"get"}
        for rule in rules.get("nonResourceRules", [])
    )


class Renewal:
    def __init__(self, config, registry, issuer, projector, *, now=None):
        self.config, self.registry, self.issuer, self.projector = (
            config,
            registry,
            issuer,
            projector,
        )
        self.now = now or (lambda: datetime.now(UTC))

    def current(self, selected):
        document = self.registry.read(selected["workspace_id"])
        require(
            document.get("version") == 1
            and document.get("workspace_id") == selected["workspace_id"],
            "Reader registry identity differs",
        )
        if document.get("active") is False:
            return None
        require(
            document.get("active") is True
            and self.now()
            < timestamp(document["expires_at"])
            <= self.now() + timedelta(seconds=35),
            "Reader registry authority expired",
        )
        target = document["target"]
        for key in ("workspace_id", "org_id", "cluster_arn", "endpoint", "namespace"):
            require(
                target.get(key) == selected[key],
                "Reader target differs from installed scope",
            )
        require(
            target.get("service_account") == "superplane-controller",
            "Reader service account differs",
        )
        return binding(target)

    def owned(self, secret, selected):
        meta = secret["metadata"]
        projection = self.config["projection"]
        require(
            meta.get("uid") == projection["secret_uid"]
            and meta.get("name") == projection["secret_name"]
            and meta.get("namespace") == projection["namespace"],
            "Reader projection was replaced",
        )
        require(
            meta.get("labels", {}).get("adp.aws-e.io/installation")
            == self.config["installation_id"]
            and meta.get("resourceVersion"),
            "Reader projection ownership differs",
        )
        key = selected["workspace_id"] + ".kubeconfig"
        annotation = "superplane.aws-e/reader-" + selected["workspace_id"]
        value = secret.get("data", {}).get(key)
        raw = meta.get("annotations", {}).get(annotation)
        require(
            (value is None) == (raw is None),
            "Reader projection lacks an ownership receipt",
        )
        receipt = json.loads(raw) if raw else None
        if receipt:
            require(
                receipt.get("owner") == self.config["installation_id"]
                and receipt.get("sha256") == digest(value),
                "Reader projection was changed outside its owner",
            )
        return key, annotation, receipt

    def publish(self, before, after):
        # CAS is the concurrency fence. A lost response can only be recovered by
        # observing the complete intended data/annotations on the original UID.
        try:
            self.projector.replace(after)
        except Exception:  # noqa: BLE001 -- uncertain writes require observation, never replay
            observed = self.projector.read()
            require(
                observed["metadata"].get("uid") == before["metadata"]["uid"]
                and observed.get("data", {}) == after.get("data", {})
                and observed["metadata"].get("annotations", {})
                == after["metadata"].get("annotations", {}),
                "Reader projection write was not acknowledged; no replay",
            )
        observed = self.projector.read()
        require(
            observed["metadata"].get("uid") == before["metadata"]["uid"]
            and observed.get("data", {}) == after.get("data", {})
            and observed["metadata"].get("annotations", {})
            == after["metadata"].get("annotations", {}),
            "Reader projection acknowledgement differs",
        )

    def reconcile(self, selected):
        target = self.current(selected)
        secret = self.projector.read()
        key, annotation, old = self.owned(secret, selected)
        if target is None:
            if old:
                require(
                    self.current(selected) is None,
                    "Reader target changed during revocation",
                )
                changed = deepcopy(secret)
                del changed["data"][key]
                del changed["metadata"]["annotations"][annotation]
                self.publish(secret, changed)
            return "inactive"
        if (
            old
            and old.get("binding") == target
            and timestamp(old["expires_at"]) > self.now() + timedelta(minutes=5)
        ):
            return "current"
        require(
            self.current(selected) == target,
            "Native reader identity changed before issuance",
        )

        def authorize():
            require(
                self.current(selected) == target,
                "Native reader authority changed before TokenRequest",
            )

        token, expires_at = self.issuer.issue(selected, target, authorize)
        require(
            self.now() + timedelta(minutes=10)
            <= timestamp(expires_at)
            <= self.now() + timedelta(minutes=16),
            "Reader token lifetime exceeds its bound",
        )
        self.issuer.verify_reader(selected, target, token)
        require(
            self.current(selected) == target,
            "Native reader identity changed after issuance",
        )
        kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": target["cluster_arn"],
            "clusters": [
                {
                    "name": "workspace",
                    "cluster": {
                        "server": target["endpoint"],
                        "certificate-authority-data": selected[
                            "certificate_authority_data"
                        ],
                    },
                }
            ],
            "contexts": [
                {
                    "name": target["cluster_arn"],
                    "context": {
                        "cluster": "workspace",
                        "namespace": target["namespace"],
                        "user": "reader",
                    },
                }
            ],
            "users": [{"name": "reader", "user": {"token": token}}],
        }
        value = base64.b64encode(
            json.dumps(kubeconfig, sort_keys=True, separators=(",", ":")).encode()
        ).decode()
        receipt = {
            "owner": self.config["installation_id"],
            "binding": target,
            "expires_at": expires_at,
            "sha256": digest(value),
        }
        changed = deepcopy(secret)
        changed.setdefault("data", {})[key] = value
        changed["metadata"].setdefault("annotations", {})[annotation] = json.dumps(
            receipt, sort_keys=True, separators=(",", ":")
        )
        self.publish(secret, changed)
        # If the claim closed during the write, the next reconciliation removes
        # the owned projection. Never report a stale claim as successful renewal.
        require(
            self.current(selected) == target,
            "Reader authority changed during publication",
        )
        return "renewed"
