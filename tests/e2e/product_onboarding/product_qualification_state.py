"""Keep a Product rollback attempt private and bound to its exact Gateway run."""

import copy
import re

from tests.e2e.cli_uplift import statestore


def recovery_key(run_id):
    statestore.product_run_key(run_id)
    return f"{statestore.PREFIX}/product-runs/{run_id}/recovery.json"


def held_evaluation(store, run_id, binding=None):
    statestore.product_run_key(run_id)
    key = f"{statestore.PREFIX}/product-runs/{run_id}/hold-evaluation.json"
    existing = store._get(key)
    if binding is not None:
        if (
            not isinstance(binding, dict)
            or binding.get("workflow_run_id") != run_id
            or not isinstance(binding.get("evaluation_id"), str)
        ):
            raise statestore.StateStoreError("Recovery hold has no original evaluation")
        if existing is not None and existing != binding:
            raise statestore.StateStoreError(
                "Recovery hold belongs to another evaluation"
            )
        if existing is None:
            store._put(key, binding)
        existing = store._get(key)
        if existing != binding:
            raise statestore.StateStoreError("Recovery hold was not durably retained")
    return existing


def held_release(store, run_id, native=None):
    """Persist the independently observed candidate privately, never as acceptance."""
    statestore.product_run_key(run_id)
    original = held_evaluation(store, run_id)
    if not isinstance(original, dict):
        raise statestore.StateStoreError(
            "Recovery release has no bound held evaluation"
        )
    key = f"{statestore.PREFIX}/product-runs/{run_id}/hold-release.json"
    existing = store._get(key)
    if native is not None:
        release = native.get("release_artifact") if isinstance(native, dict) else None
        runtime = native.get("runtime_receipt") if isinstance(native, dict) else None
        target = native.get("target") if isinstance(native, dict) else None
        try:
            selected = f"product-{release['run_id']}-{release['run_attempt']}"
        except (KeyError, TypeError):
            selected = None
        if (
            not isinstance(release, dict)
            or not isinstance(runtime, dict)
            or not isinstance(target, dict)
            or selected != run_id
            or release.get("source_revision") != runtime.get("actual_revision")
            or not re.fullmatch(r"[a-f0-9]{40}", str(release.get("workflow_revision")))
            or release.get("source_revision") != original.get("expected_revision")
            or release.get("image_digest") != runtime.get("image_digest")
            or release.get("migration_head") != runtime.get("migration_head")
            or any(
                release.get(field) != target.get(field) or not target.get(field)
                for field in ("account_id", "region", "resource_id", "repository_id")
            )
            or native.get("source_revision") != release.get("source_revision")
            or not isinstance(native.get("release_ref"), str)
            or not native["release_ref"].startswith("s3://")
            or "versionId=" not in native["release_ref"]
            or not isinstance(native.get("observation_ref"), str)
            or not native["observation_ref"].startswith("runtime:sha256:")
        ):
            raise statestore.StateStoreError(
                "Recovery release differs from held run or observed Gateway"
            )
        receipt = {
            "evaluation": original,
            "release_artifact": release,
            "runtime_receipt": runtime,
            "target": target,
            "release_ref": native["release_ref"],
            "observation_ref": native["observation_ref"],
        }
        if existing is not None and existing != receipt:
            raise statestore.StateStoreError(
                "Recovery candidate proof belongs to another observation"
            )
        if existing is None:
            store._put(key, receipt)
        existing = store._get(key)
        if existing != receipt:
            raise statestore.StateStoreError(
                "Recovery candidate proof was not durably retained"
            )
    if existing is not None and (
        not isinstance(existing, dict) or existing.get("evaluation") != original
    ):
        raise statestore.StateStoreError(
            "Recovery candidate proof belongs to another evaluation"
        )
    return copy.deepcopy(existing)


class RetainedQualification:
    def __init__(self, store, candidate, baseline):
        if not isinstance(candidate, dict) or not isinstance(baseline, dict):
            raise ValueError("Product recovery requires two qualified releases")
        run_id = f"product-{candidate.get('run_id')}-{candidate.get('run_attempt')}"
        self.key = recovery_key(run_id)
        self.store = store
        self.binding = {
            "run_id": run_id,
            "source_revision": candidate.get("source_revision"),
            "workflow_revision": candidate.get("workflow_revision"),
            "account_id": candidate.get("account_id"),
            "region": candidate.get("region"),
            "resource_id": candidate.get("resource_id"),
            "candidate_image_digest": candidate.get("image_digest"),
            "baseline_source_revision": baseline.get("source_revision"),
            "baseline_image_digest": baseline.get("image_digest"),
        }
        if (
            not all(isinstance(value, str) and value for value in self.binding.values())
            or not re.fullmatch(r"[a-f0-9]{40}", self.binding["source_revision"])
            or not re.fullmatch(r"[a-f0-9]{40}", self.binding["workflow_revision"])
            or not re.fullmatch(
                r"[a-f0-9]{40}", self.binding["baseline_source_revision"]
            )
            or not all(
                re.fullmatch(r"sha256:[a-f0-9]{64}", self.binding[field])
                for field in ("candidate_image_digest", "baseline_image_digest")
            )
        ):
            raise ValueError("Product recovery attempt has incomplete release binding")

    def load(self):
        stored = self.store._get(self.key)
        if stored is None:
            return None
        if (
            not isinstance(stored, dict)
            or stored.get("binding") != self.binding
            or not isinstance(stored.get("report"), dict)
        ):
            raise statestore.StateStoreError(
                "Product recovery attempt belongs to another release"
            )
        return copy.deepcopy(stored.get("report"))

    def held_fixture(self, *, begun=False):
        run_id = self.binding["run_id"]
        report = self.load()
        binding = held_evaluation(self.store, run_id)
        proof = held_release(self.store, run_id)
        resumed = (
            begun
            and isinstance(report, dict)
            and isinstance(report.get("owned"), dict)
            and [item.get("stage") for item in report.get("attempts", [])]
            == ["created", "quiesce_started"]
            and report["attempts"][0].get("receipt") == report["owned"]
            and report["attempts"][1].get("receipt") == report.get("candidate")
        )
        if (
            not isinstance(report, dict)
            or not (resumed or (report.get("owned") is None and report.get("attempts") == []))
            or not isinstance(binding, dict)
            or not isinstance(proof, dict)
            or proof.get("release_artifact", {}).get("source_revision")
            != self.binding["source_revision"]
            or proof.get("release_artifact", {}).get("image_digest")
            != self.binding["candidate_image_digest"]
            or report.get("candidate_ref") != proof.get("release_ref")
            or not isinstance(report.get("baseline_ref"), str)
            or "versionId=" not in report["baseline_ref"]
        ):
            raise statestore.StateStoreError("Recovery has no original qualified hold")
        document, manifest = self.store.load(binding["evaluation_id"])
        held = (
            (document.get("product_evidence") or {}).get("held")
            if isinstance(document, dict)
            else None
        )
        entries = manifest.get("resources") if isinstance(manifest, dict) else None
        matching = (
            [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("kind") == "product_fixture"
            ]
            if isinstance(entries, list)
            else []
        )
        fields = {
            "run_id",
            "product_id",
            "project_id",
            "tenant_id",
            "actor_id",
            "source_revision",
            "history_ref",
            "idempotency_key",
            "idempotency_receipt",
        }
        if (
            not isinstance(held, dict)
            or (resumed and report["owned"] != held)
            or set(held) != fields
            or any(not held[field] for field in fields)
            or held["run_id"] != run_id
            or held["source_revision"] != self.binding["source_revision"]
            or not all(
                re.fullmatch(r"sha256:[a-f0-9]{64}", held[field])
                for field in ("history_ref", "idempotency_receipt")
            )
            or document.get("cleanup_ok") is not False
            or document.get("product_run_id") != run_id
            or document["product_evidence"].get("run_id") != run_id
            or manifest.get("prefix") != binding["evaluation_id"]
            or len(matching) != 1
            or matching[0].get("prefix") != binding["evaluation_id"]
            or matching[0].get("id") != run_id
            or matching[0].get("status") != "pending"
            or matching[0].get("account") != self.binding["account_id"]
            or matching[0].get("region") != self.binding["region"]
            or any(
                matching[0].get("detail", {}).get(field) != held[value]
                for field, value in (
                    ("tenant_id", "tenant_id"),
                    ("actor_id", "actor_id"),
                    ("source_revision", "source_revision"),
                    ("idempotency_key", "idempotency_key"),
                )
            )
        ):
            raise statestore.StateStoreError(
                "Recovery fixture differs from durable held ownership"
            )
        return copy.deepcopy(held)

    def begin_hold(self):
        report = self.load()
        owned = self.held_fixture(begun=True)
        if report["attempts"]:
            return report
        report["owned"] = owned
        report["attempts"] = [
            {"stage": "created", "receipt": owned},
            {"stage": "quiesce_started", "receipt": report["candidate"]},
        ]
        self.retain(report)
        return self.load()

    def retain(self, report):
        if (
            not isinstance(report, dict)
            or report.get("candidate") is None
            or report.get("baseline") is None
        ):
            raise statestore.StateStoreError("Product recovery report is incomplete")
        candidate = report["candidate"]
        expected = {
            "run_id": int(self.binding["run_id"].split("-")[1]),
            "run_attempt": int(self.binding["run_id"].split("-")[2]),
            "workflow_revision": self.binding["workflow_revision"],
            "account_id": self.binding["account_id"],
            "region": self.binding["region"],
            "resource_id": self.binding["resource_id"],
        }
        if not isinstance(candidate, dict) or any(
            candidate.get(key) != value for key, value in expected.items()
        ):
            raise statestore.StateStoreError(
                "Product recovery report changed the selected Gateway run"
            )
        owned = report.get("owned")
        if owned is not None and (
            not isinstance(owned, dict)
            or owned.get("run_id") != self.binding["run_id"]
            or set(owned)
            - {
                "run_id",
                "product_id",
                "project_id",
                "tenant_id",
                "actor_id",
                "source_revision",
                "history_ref",
                "idempotency_key",
                "idempotency_receipt",
            }
        ):
            raise statestore.StateStoreError(
                "Product recovery has foreign or unsafe fixture ownership"
            )
        for release, prefix in (
            (report["candidate"], "candidate_"),
            (report["baseline"], "baseline_"),
        ):
            if (
                release.get("source_revision")
                != self.binding[
                    "source_revision"
                    if prefix == "candidate_"
                    else "baseline_source_revision"
                ]
                or release.get("image_digest") != self.binding[prefix + "image_digest"]
            ):
                raise statestore.StateStoreError(
                    "Product recovery report changed immutable artifacts"
                )
        existing = self.load()
        if existing is not None:
            if (
                existing.get("owned") != report.get("owned")
                and existing.get("owned") is not None
            ):
                raise statestore.StateStoreError(
                    "Product recovery changed durable fixture ownership"
                )
            previous = existing.get("attempts") or []
            if report.get("attempts", [])[: len(previous)] != previous:
                raise statestore.StateStoreError(
                    "Product recovery cannot discard an earlier attempt"
                )
        self.store._put(self.key, {"binding": self.binding, "report": report})
        if self.load() != report:
            raise statestore.StateStoreError(
                "Product recovery attempt was not retained"
            )
