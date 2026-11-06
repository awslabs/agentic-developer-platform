"""Finite historical envFrom qualification after reviewed runtime maintenance.

Private retained bytes are authenticated by reviewed fingerprints and a protected
predecessor. Its expiry is never extended or used as present authority. Current
owner/runtime and complete original audit are collected again on every call.
"""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json

from .bootstrap_diagnostics import ROOT, digest, instant
from .bootstrap_history import current_context
from .provider_authority import contract, require
from . import runtime_maintenance as runtime


class HistoricalEnvironment:
    def __init__(
        self, installer, enrollment, original, descriptor, session, references
    ):
        from .bootstrap_recovery import source_contract
        from .bootstrap_revoked import revoked_contract

        require(installer is not None)
        self.installer, self.enrollment, self.session = installer, enrollment, session
        self.references = copy.deepcopy(references)
        self.fingerprints = json.loads(
            (ROOT / "environment-history-fingerprints.json").read_text()
        )
        pins, refs = self.fingerprints, self.references
        require(
            pins["version"] == 1
            and set(refs) == {"snapshot", "maintenance"}
            and len(refs["maintenance"]) == len(pins["maintenance"]) == 2
            and digest(original) == pins["original_receipt_sha256"]
            and digest(refs["snapshot"]) == pins["snapshot_sha256"]
        )
        for item, pin in zip(refs["maintenance"], pins["maintenance"]):
            require(set(item) == {"plan", "receipt"})
            plan, receipt = item["plan"], item["receipt"]
            require(
                digest(plan) == pin["plan_sha256"]
                and digest(receipt) == pin["receipt_sha256"]
                and receipt["runtime_maintenance"]["plan_sha256"] == pin["plan_sha256"]
                and receipt["status"] == "runtime-maintenance-complete"
                and receipt["runtime_maintenance"]["state"] == "observed-paused"
                and receipt["installation_id"]
                == plan["installation_id"]
                == original["installation_id"]
                == installer.owner
                and receipt["release_id"] == plan["release_id"]
                and digest(receipt["environment"]) == plan["environment_sha256"]
            )
        self.plan = refs["maintenance"][-1]["plan"]
        previous = runtime.payload(refs["snapshot"]["configmap"])
        for item in refs["maintenance"]:
            before = self.config(item["plan"]["before"])
            after = self.config(item["plan"]["after"])
            require(before == previous)
            previous = after
        self.record_id = "sppr1:bootstrap:" + digest(
            {
                k: descriptor[k]
                for k in (
                    "org_id",
                    "workspace_id",
                    "operation_id",
                    "plan_digest",
                    "source_key",
                    "source_fence",
                )
            }
        )
        self.prior = self.read_predecessor()
        source = source_contract().qualified_source(self.prior["source_image"])
        authority = enrollment["authority"]
        require(
            self.prior["version"] == 1
            and self.prior["record_id"] == self.record_id
            and source is not None
            and self.prior["source_revision"] == source["revision"]
            and self.prior["source_hashes"] == source["hashes"]
            and self.prior["source_pod_uid"] == self.prior["workload_binding"]
            and all(
                self.prior[k] == authority[k]
                for k in (
                    "org_id",
                    "workspace_id",
                    "credential_id",
                    "installation_id",
                    "adp_org_id",
                    "account_id",
                    "region",
                )
            )
            and all(
                self.prior[k] == descriptor[k]
                for k in (
                    "operation_id",
                    "plan_digest",
                    "source_key",
                    "source_fence",
                    "source_principal",
                    "source_acquired_at",
                    "source_pod_name",
                    "source_pod_uid",
                    "workload_binding",
                )
            )
            and self.prior.get("settled_bootstrap")
            == descriptor.get("settled_bootstrap")
            and instant(self.prior["checked_at"])
            < instant(self.prior["expires_at"])
            <= instant(self.prior["checked_at"]) + timedelta(hours=1)
        )
        revoked_contract().validate_revoked(self.prior)
        self.current = self.read_current()

    def config(self, values):
        name = self.references["snapshot"]["configmap"]["metadata"]["name"]
        selected = [
            v
            for v in values
            if v["kind"] == "ConfigMap" and v["metadata"]["name"] == name
        ]
        require(len(selected) == 1)
        return selected[0]

    def read_predecessor(self):
        item = (
            self.session.client(
                "dynamodb",
                region_name=self.enrollment.get(
                    "management_region", self.enrollment["authority"]["region"]
                ),
            )
            .get_item(
                TableName=f"adp-{self.enrollment['environment']}-superplane-provider-authorities",
                Key={"record_id": {"S": self.record_id}},
                ConsistentRead=True,
            )
            .get("Item")
        )
        require(item is not None and item["record_id"] == {"S": self.record_id})
        value = json.loads(item["document"]["S"])
        require(
            item["revision"] == {"S": self.fingerprints["predecessor_revision"]}
            and digest(value) == self.fingerprints["predecessor_revision"]
            and item["document"]["S"] == contract().canonical(value)
        )
        return value

    def read_current(self):
        installer, plan = self.installer, self.plan
        current = current_context(installer, self.enrollment, self.session)
        require(
            digest(installer.env) == plan["environment_sha256"]
            and installer.release == plan["release_id"] == digest(installer.lock)
            and installer.lock
            == self.references["maintenance"][-1]["receipt"]["release_lock"]
        )
        values = runtime.objects(installer)
        require([runtime.payload(v) for v in values] == plan["after"])
        runtime.paused(installer, values)
        runtime.verify_rollout(installer, plan)
        require(runtime.verify_schema(installer, values[0]) == plan["database"])
        current["runtime_plan_sha256"] = digest(plan)
        return current

    def qualify(self, known, pod, created, ended, audit):
        expected = known["environment_configmap"]
        snapshot = self.references["snapshot"]
        original = snapshot["configmap"]
        meta, worker = original["metadata"], pod["spec"]["containers"][0]
        require(
            original["kind"] == "ConfigMap"
            and original["apiVersion"] == "v1"
            and not original.get("binaryData")
            and all(meta[k] == expected[k] for k in ("name", "namespace", "uid"))
            and meta["resourceVersion"] == expected["resource_version"]
            and instant(meta["creationTimestamp"])
            < created
            < ended
            < instant(snapshot["observed_at"])
            < instant(self.prior["checked_at"])
            and digest(original["data"]) == expected["data_sha256"]
            and not any(k.startswith("PYTHON") for k in original["data"])
            and worker.get("envFrom") == [{"configMapRef": {"name": expected["name"]}}]
            and all(
                not e.get("valueFrom", {}).get("configMapKeyRef")
                for e in worker.get("env", [])
            )
            and all(
                not m.get("subPath") and not m.get("subPathExpr")
                for m in worker.get("volumeMounts", [])
            )
        )
        for volume in pod["spec"]["volumes"]:
            require(volume.get("configMap", {}).get("name") != expected["name"])
            require(
                all(
                    s.get("configMap", {}).get("name") != expected["name"]
                    for s in volume.get("projected", {}).get("sources", [])
                )
            )
        prior_audit = self.prior["source_audit"]
        require(
            self.prior["source_image"] == worker["image"]
            and prior_audit["log_group"] == audit["log_group"]
            and prior_audit["creation_event_id"] == audit["creation_event_id"]
            and set(prior_audit["terminal_event_ids"])
            <= set(audit["terminal_event_ids"])
        )
        client = self.session.client("logs", region_name=self.installer.env["region"])
        now, count, reads, mutations = datetime.now(timezone.utc), 0, 0, []
        approved = {
            p["mutation_message_sha256"] for p in self.fingerprints["maintenance"]
        }
        require(len(approved) == 2)
        for page in client.get_paginator("filter_log_events").paginate(
            logGroupName=audit["log_group"],
            logStreamNamePrefix="kube-apiserver-audit-",
            startTime=int((created - timedelta(minutes=5)).timestamp() * 1000),
            endTime=int(now.timestamp() * 1000),
            filterPattern=json.dumps(expected["name"]),
        ):
            for event in page.get("events", []):
                count += 1
                require(
                    count <= 10000
                    and event["logStreamName"].startswith("kube-apiserver-audit-")
                )
                value = json.loads(event["message"])
                ref = value.get("objectRef", {})
                if (
                    ref.get("resource") != "configmaps"
                    or ref.get("namespace") != expected["namespace"]
                ):
                    continue
                if value.get("verb") in {"get", "list", "watch"}:
                    if (
                        value.get("stage") == "ResponseComplete"
                        and ref.get("name") == expected["name"]
                        and value.get("responseStatus", {}).get("code") == 200
                        and created
                        <= instant(value["stageTimestamp"])
                        <= instant(self.prior["checked_at"])
                    ):
                        reads += 1
                    continue
                fingerprint = hashlib.sha256(event["message"].encode()).hexdigest()
                extra = value.get("user", {}).get("extra", {})
                require(
                    fingerprint in approved
                    and fingerprint not in mutations
                    and ref.get("name") == expected["name"]
                    and value.get("verb") == "patch"
                    and value.get("stage") == "ResponseComplete"
                    and value.get("responseStatus", {}).get("code") == 200
                    and ended
                    < instant(self.prior["checked_at"])
                    < instant(value["requestReceivedTimestamp"])
                    <= instant(value["stageTimestamp"])
                    <= now
                    and extra.get("canonicalArn")
                    == [self.enrollment["operator_role_arn"]]
                    and extra.get("principalId")
                    == [self.enrollment["operator_role_id"]]
                )
                mutations.append(fingerprint)
        require(reads > 0 and set(mutations) == approved)
        return {
            "kind": "qualified-historical-envfrom-v1",
            "snapshot_sha256": self.fingerprints["snapshot_sha256"],
            "predecessor_record_id": self.record_id,
            "predecessor_revision": self.fingerprints["predecessor_revision"],
            "terminal_at": ended.isoformat(),
            "audit_through": now.isoformat(),
            "maintenance": copy.deepcopy(self.fingerprints["maintenance"]),
            "current": copy.deepcopy(self.current),
        }

    def finish(self):
        require(
            self.read_predecessor() == self.prior
            and self.read_current() == self.current
        )
