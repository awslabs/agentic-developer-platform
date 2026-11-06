"""Finite owner collection for two expressly accepted local-only stdin records.

The private manifest identifies every protected exec and mutation event. Only
the two approved stdin programs have assumed, rather than protected, provenance.
All other source, actor, pod, environment and current-state checks still apply.
"""

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from harness_jobs import bootstrap_provenance_assumption as contract

from .bootstrap_diagnostics import digest, environment, instant, mutation_identity
from .provider_authority import require


class ApprovedDiagnosticAssumption:
    def __init__(self, *, manifest_path, approval_path, proposal_path):
        self.paths = {
            "manifest": Path(manifest_path),
            "approval": Path(approval_path),
            "proposal": Path(proposal_path),
        }
        self.documents = self._read()
        self.known = self.documents["manifest"]
        approval, proposal = self.documents["approval"], self.documents["proposal"]
        require(
            approval["kind"] == "user-approved-two-session-provenance-assumption-v1"
            and approval["user_response"] == "yes"
            and approval["proposal_sha256"] == contract.PROPOSAL_SHA256
        )
        require(
            all(
                approval[k] == proposal[k]
                for k in (
                    "workspace_id",
                    "operation_id",
                    "pod_uid",
                    "image_digest",
                    "audit_ids",
                    "scripts",
                )
            )
        )
        require(self.known["closure_sha256"] == contract.CLOSURE_SHA256)
        require(digest(self.known["source_binding"]) == contract.SOURCE_BINDING_SHA256)
        require(
            digest(self.known["journal_binding"]) == contract.JOURNAL_BINDING_SHA256
        )
        require(
            self.known["closed_current_journal_sha256"]
            == contract.CLOSED_JOURNAL_SHA256
        )
        require(
            digest(self.known["closed_current_journal"])
            == contract.CLOSED_JOURNAL_SHA256
        )
        require(
            approval["operation_id"] == self.known["source_binding"]["operation_id"]
            and approval["workspace_id"] == self.known["source_binding"]["workspace_id"]
            and approval["pod_uid"] == self.known["pod_uid"]
            and self.known["image"].endswith("@" + approval["image_digest"])
        )
        assumed = [e for e in self.known["execs"] if e["mode"] == "approved-stdin"]
        require(
            len(assumed) == len(approval["audit_ids"]) == len(approval["scripts"]) == 2
        )
        require({e["audit_id"] for e in assumed} == set(approval["audit_ids"]))
        require(
            {e["query_hash"] for e in assumed}
            == {s["sha256"] for s in approval["scripts"]}
        )
        for script in approval["scripts"]:
            require(
                hashlib.sha256(Path(script["path"]).read_bytes()).hexdigest()
                == script["sha256"]
            )

    def _read(self):
        values = {}
        for name, expected in (
            ("manifest", contract.MANIFEST_SHA256),
            ("approval", contract.APPROVAL_SHA256),
            ("proposal", contract.PROPOSAL_SHA256),
        ):
            raw = self.paths[name].read_bytes()
            require(hashlib.sha256(raw).hexdigest() == expected)
            values[name] = json.loads(raw)
        return values

    def bind_descriptor(self, descriptor):
        require(digest(descriptor["settled_bootstrap"]) == contract.CLOSURE_SHA256)
        require(
            all(
                descriptor[k] == self.known["source_binding"][k]
                for k in contract.SOURCE_FIELDS
                if k != "source_image"
            )
        )

    def select(self, cluster, namespace, name, uid):
        require(
            (cluster, namespace, name, uid)
            == tuple(
                self.known[k] for k in ("cluster", "namespace", "pod_name", "pod_uid")
            )
        )

    def bind_journal(self, row):
        """Only native revocation may advance beyond the approved closed row."""
        initial = self.known["closed_current_journal"]
        require(
            all(
                row[k] == initial[k]
                for k in initial
                if k not in {"progress_json", "revoked"}
            )
        )
        before, after = (json.loads(r["progress_json"]) for r in (initial, row))
        plan = json.loads(row["plan_json"])
        keys = {g["key"] for g in plan["grants"]}
        require(
            before["phase"] == "revoking" and after["phase"] in {"revoking", "revoked"}
        )
        complete = after["phase"] == "revoked"
        require(
            row["revoked"] is complete
            and (not complete or after.get("complete") is True)
        )
        require(set(after) == set(before) | ({"complete"} if complete else set()))
        for key, value in before.items():
            if key == "phase":
                continue
            if key not in keys:
                require(after[key] == value)
                continue
            require(
                set(after[key]) == set(value)
                and after[key]["identity"] == value["identity"]
            )
            phases = (
                {value["phase"]}
                if value["phase"] in {"adopted", "revoked"}
                else {value["phase"], "revoke_intent", "revoked"}
            )
            require(after[key]["phase"] in phases)
            require(
                not complete
                or key == "workspace-namespace"
                or after[key]["phase"] == "revoked"
            )

    def qualify(
        self,
        events,
        *,
        cluster,
        pod,
        creation_event,
        terminals,
        session,
        management_region,
    ):
        known = self.known
        self.select(
            cluster,
            pod["metadata"]["namespace"],
            pod["metadata"]["name"],
            pod["metadata"]["uid"],
        )
        require(self._read() == self.documents)
        protected = known["protected_event_message_sha256"]
        require(len(events) == len(protected) == 186)
        require({e["eventId"] for e, _ in events} == set(protected))
        require(
            all(
                hashlib.sha256(e["message"].encode()).hexdigest()
                == protected[e["eventId"]]
                for e, _ in events
            )
        )
        require(
            known["version"] == 1
            and creation_event["eventId"] == known["creation_event_id"]
        )
        require(
            sorted(e["eventId"] for e, _ in terminals) == known["terminal_event_ids"]
        )
        worker = pod["spec"]["containers"][0]
        require(
            len(pod["spec"]["containers"]) == 1
            and worker["image"]
            == known["image"]
            == known["source_binding"]["source_image"]
            and digest(worker) == known["container_sha256"]
            and digest(pod["spec"]["volumes"]) == known["pod_volumes_sha256"]
            and worker["securityContext"]["readOnlyRootFilesystem"] is True
        )
        actual = [
            (event, value)
            for event, value in events
            if value.get("objectRef", {}).get("subresource")
            in {"exec", "attach", "ephemeralcontainers"}
        ]
        expected = {
            stage["event_id"]: (execution, stage)
            for execution in known["execs"]
            for stage in execution["stages"]
        }
        require(len(expected) == len(actual) == 178 and len(known["execs"]) == 89)
        require({e["eventId"] for e, _ in actual} == set(expected))
        mutations = [
            (e, v) for e, v in events if v.get("verb") not in {"get", "list", "watch"}
        ]
        require(len(mutations) == len(known["mutations"]) == 8)
        require({e["eventId"] for e, _ in mutations} == set(known["mutations"]))
        require(
            all(
                mutation_identity(v) == known["mutations"][e["eventId"]]
                for e, v in mutations
            )
        )
        created = instant(pod["metadata"]["creationTimestamp"])
        ended = min(
            instant(json.loads(e["message"])["stageTimestamp"]) for e, _ in terminals
        )
        for _, value in events:
            response = value.get("responseObject", {})
            if response.get("metadata", {}).get("name") == known["pod_name"]:
                require(response["metadata"]["uid"] == known["pod_uid"])
                if response.get("kind") == "Pod":
                    require(
                        response["spec"]["containers"] == pod["spec"]["containers"]
                        and response["spec"]["volumes"] == pod["spec"]["volumes"]
                        and not response["spec"].get("ephemeralContainers")
                    )
        for event, value in actual:
            execution, stage = expected[event["eventId"]]
            require(
                value.get("auditID") == execution["audit_id"]
                and value.get("stage") == stage["stage"]
                and value.get("verb") == "get"
                and value.get("responseStatus", {}).get("code") == 101
                and value.get("requestReceivedTimestamp") == execution["time"]
                and value.get("stageTimestamp") == stage["stage_timestamp"]
                and created
                < instant(execution["time"])
                <= instant(stage["stage_timestamp"])
                < ended
                and value.get("objectRef")
                == {
                    "resource": "pods",
                    "namespace": known["namespace"],
                    "name": known["pod_name"],
                    "apiVersion": "v1",
                    "subresource": "exec",
                }
            )
            actor = known["actors"][execution["actor"]]
            user = value.get("user", {})
            extra = user.get("extra", {})
            require(
                user.get("username") == actor["user_arn"]
                and extra.get("arn") == [actor["user_arn"]]
                and extra.get("principalId") == actor["principal_id"]
                and extra.get("canonicalArn") == actor["canonical_arn"]
                and extra.get("sessionName") == [actor["user_arn"].rsplit("/", 1)[1]]
                and value.get("sourceIPs") == actor["source_ips"]
                and value.get("userAgent") == actor["user_agent"]
            )
            uri = value["requestURI"]
            require(
                hashlib.sha256(uri.encode()).hexdigest()
                == execution["request_uri_sha256"]
            )
            parsed = urlsplit(uri)
            require(
                not parsed.scheme
                and not parsed.netloc
                and not parsed.fragment
                and parsed.path
                == f"/api/v1/namespaces/{known['namespace']}/pods/{known['pod_name']}/exec"
            )
            query = parse_qs(parsed.query, strict_parsing=True)
            mode = execution["mode"]
            if mode == "process-list":
                command = ["ps", "-eo", "pid,ppid,comm,state,etime,time,wchan:24"]
                require(digest(command) == execution["query_hash"])
                require(execution["source_origin"] == "protected-request-uri")
            else:
                require(mode in {"inline-python", "approved-stdin"})
                source = (
                    self.paths["manifest"].parent
                    / known["sources"][execution["query_hash"]]
                ).read_bytes()
                require(hashlib.sha256(source).hexdigest() == execution["query_hash"])
                command = (
                    ["python", "-B", "-c", source.decode()]
                    if mode == "inline-python"
                    else ["python", "-B", "-"]
                )
                require(
                    execution["source_origin"]
                    == (
                        "local-tool-record-owner-assumed"
                        if mode == "approved-stdin"
                        else "protected-request-uri"
                    )
                )
            options = execution["query_options"]
            require(
                options
                == {
                    "container": ["paid-worker"],
                    "stderr": ["true"],
                    "stdout": ["true"],
                    **({"stdin": ["true"]} if mode == "approved-stdin" else {}),
                    **({"timeout": ["15s"]} if "timeout" in options else {}),
                }
            )
            require(query == {"command": command, **options})
        environment(
            session,
            known,
            created=created,
            management_region=management_region,
            precise=True,
        )
        require(self._read() == self.documents)
        return copy.deepcopy(contract.metadata(contract.CLOSURE_SHA256))
