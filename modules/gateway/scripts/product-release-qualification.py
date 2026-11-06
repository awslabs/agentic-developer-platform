#!/usr/bin/env python3
"""Bound a Product rollback to reviewed, digest-pinned releases and durable readback."""

import re

ENGINE_FLOOR = "a044c73d98b6d89670b39d933581acaf2d163206"
RUNTIME_FLOOR = "7c59980125ae38e5e5558df8c636547fb64f3242"
SOURCE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class QualificationError(RuntimeError):
    def __init__(self, report):
        super().__init__("Product rollback qualification or recovery is incomplete")
        self.report = report


def require(condition, message):
    if not condition:
        raise ValueError(message)


def release_identity(release):
    require(isinstance(release, dict), "Signed Product release is missing")
    for field in ("source_revision", "workflow_revision"):
        require(bool(SOURCE.fullmatch(str(release.get(field)))), "Invalid immutable release revision")
    require(bool(DIGEST.fullmatch(str(release.get("image_digest")))), "Invalid immutable release image")
    require(
        release.get("component") == "gateway-backend"
        and release.get("workflow_path") == ".github/workflows/gateway-deploy.yml"
        and type(release.get("run_id")) is int
        and release["run_id"] > 0
        and type(release.get("run_attempt")) is int
        and release["run_attempt"] > 0,
        "Product release has no matched Gateway run",
    )
    require(
        release.get("product_onboarding_enabled") is False
        and isinstance(release.get("migration_head"), str)
        and bool(release["migration_head"])
        and re.fullmatch(r"[0-9a-f]{64}", str(release.get("cli_manifest_sha256"))),
        "Product release has no compatible schema or CLI receipt",
    )
    for field in ("account_id", "region", "resource_id"):
        require(isinstance(release.get(field), str) and bool(release[field]), "Product release target is missing")
    return {
        field: release[field]
        for field in (
            "run_id",
            "run_attempt",
            "source_revision",
            "workflow_revision",
            "account_id",
            "region",
            "resource_id",
            "image_digest",
            "migration_head",
        )
    }


def compatible(candidate, baseline, runtime, *, is_ancestor, resolve_image):
    candidate_id = release_identity(candidate)
    baseline_id = release_identity(baseline)
    require(
        candidate.get("product_profile_enabled") is True and baseline.get("product_profile_enabled") is True,
        "Both Product artifacts must support saved reads",
    )
    differences = [
        f"{field}: candidate={candidate_id[field]!r}, baseline={baseline_id[field]!r}"
        for field in ("account_id", "region", "resource_id", "migration_head")
        if candidate_id[field] != baseline_id[field]
    ]
    require(not differences, "Product rollback target or schema differs: " + "; ".join(differences))
    require(
        candidate_id["source_revision"] != baseline_id["source_revision"]
        and is_ancestor(ENGINE_FLOOR, baseline_id["source_revision"])
        and is_ancestor(baseline_id["source_revision"], candidate_id["source_revision"])
        and is_ancestor(RUNTIME_FLOOR, candidate_id["source_revision"]),
        "Product rollback requires reviewed compatible main artifacts with engine fixes",
    )
    for artifact in (candidate_id, baseline_id):
        require(resolve_image(artifact["source_revision"]) == artifact["image_digest"], "Immutable source tag differs from signed Product image")
    require(
        isinstance(runtime, dict)
        and runtime.get("source_revision") == candidate_id["source_revision"]
        and runtime.get("workflow_revision") == candidate_id["workflow_revision"]
        and runtime.get("image_digest") == candidate_id["image_digest"]
        and runtime.get("migration_head") == candidate_id["migration_head"]
        and runtime.get("product_profile_enabled") is True
        and runtime.get("product_onboarding_enabled") is False
        and runtime.get("account_id") == candidate_id["account_id"]
        and runtime.get("region") == candidate_id["region"]
        and runtime.get("resource_id") == candidate_id["resource_id"],
        "Current serving Product/schema observation differs from candidate",
    )
    return candidate_id, baseline_id


def check_readback(owned, observed, artifact, *, admissions):
    require(
        isinstance(observed, dict)
        and observed.get("source_revision") == artifact["source_revision"]
        and observed.get("workflow_revision") == artifact["workflow_revision"]
        and observed.get("image_digest") == artifact["image_digest"]
        and observed.get("migration_head") == artifact["migration_head"]
        and observed.get("product_profile_enabled") is admissions
        and observed.get("product_onboarding_enabled") is False
        and observed.get("account_id") == artifact["account_id"]
        and observed.get("region") == artifact["region"]
        and observed.get("resource_id") == artifact["resource_id"]
        and observed.get("supported_routes_ok") is True
        and observed.get("non_product_available") is True,
        "Serving release, admissions or supported routes differ",
    )
    if owned is not None:
        require(
            all(
                owned.get(field) == observed.get(field) and owned.get(field)
                for field in ("run_id", "product_id", "history_ref", "idempotency_key", "idempotency_receipt")
            ),
            "Product, history or exact idempotency receipt changed",
        )


def qualify(candidate, baseline, runtime, *, is_ancestor, resolve_image, create, quiesce, switch, observe, cleanup, retain):
    candidate_id, baseline_id = compatible(candidate, baseline, runtime, is_ancestor=is_ancestor, resolve_image=resolve_image)
    report = {"status": "incomplete", "candidate": candidate_id, "baseline": baseline_id, "attempts": [], "owned": None}
    retain(report)
    owned = None
    quiesced = False
    restored = False
    failures = []

    def record(stage, receipt):
        report["attempts"].append({"stage": stage, "receipt": receipt})
        retain(report)

    try:
        owned = create()
        require(
            isinstance(owned, dict)
            and owned.get("run_id")
            and owned.get("product_id")
            and owned.get("history_ref")
            and owned.get("idempotency_key")
            and owned.get("idempotency_receipt"),
            "Product fixture must have durable original ownership and receipts",
        )
        require(
            set(owned)
            <= {
                "run_id",
                "product_id",
                "project_id",
                "tenant_id",
                "actor_id",
                "source_revision",
                "history_ref",
                "idempotency_key",
                "idempotency_receipt",
            },
            "Product recovery receipt cannot retain credentials",
        )
        report["owned"] = owned
        record("created", owned)
        quiesced = True
        quiesce_receipt = quiesce()
        require(isinstance(quiesce_receipt, dict) and quiesce_receipt.get("admissions_disabled") is True, "Product admissions are not quiesced")
        record("quiesced", quiesce_receipt)
        check_readback(owned, observe(owned), candidate_id, admissions=False)
        record("candidate_quiesced", {"source_revision": candidate_id["source_revision"]})
        record("baseline_restore_started", baseline_id)
        switch(baseline_id, admissions=False)
        baseline_observed = observe(owned)
        check_readback(owned, baseline_observed, baseline_id, admissions=False)
        record("baseline_verified", baseline_observed)
    except Exception as error:
        failures.append(type(error).__name__ + ": " + str(error))
    finally:
        if quiesced:
            try:
                record("candidate_restore_started", candidate_id)
                switch(candidate_id, admissions=True)
                candidate_observed = observe(owned)
                check_readback(owned, candidate_observed, candidate_id, admissions=True)
                record("candidate_verified", candidate_observed)
                restored = True
            except Exception as error:
                failures.append("candidate restoration: " + type(error).__name__ + ": " + str(error))
        if owned is not None and restored:
            try:
                cleaned = cleanup(owned)
                require(
                    isinstance(cleaned, dict)
                    and cleaned.get("owned_absent") is True
                    and cleaned.get("shared_unchanged") is True
                    and cleaned.get("run_id") == owned["run_id"],
                    "Owned Product cleanup is incomplete",
                )
                record("cleaned", cleaned)
            except Exception as error:
                failures.append("owned cleanup: " + type(error).__name__ + ": " + str(error))
    report["failures"] = failures
    report["status"] = (
        "passed"
        if not failures
        and restored
        and any(attempt["stage"] == "baseline_verified" for attempt in report["attempts"])
        and any(attempt["stage"] == "cleaned" for attempt in report["attempts"])
        else "incomplete"
    )
    retain(report)
    if report["status"] != "passed":
        raise QualificationError(report)
    return report


class QualificationStages:
    """Persist each handoff before another protected identity changes the Gateway."""

    def __init__(self, candidate, baseline, runtime, *, is_ancestor, resolve_image, state):
        self.candidate, self.baseline = compatible(
            candidate,
            baseline,
            runtime,
            is_ancestor=is_ancestor,
            resolve_image=resolve_image,
        )
        self.state = state
        require(
            all(
                self.state.binding.get(key) == value
                for key, value in {
                    "run_id": f"product-{self.candidate['run_id']}-{self.candidate['run_attempt']}",
                    "source_revision": self.candidate["source_revision"],
                    "workflow_revision": self.candidate["workflow_revision"],
                    "account_id": self.candidate["account_id"],
                    "region": self.candidate["region"],
                    "resource_id": self.candidate["resource_id"],
                    "candidate_image_digest": self.candidate["image_digest"],
                    "baseline_source_revision": self.baseline["source_revision"],
                    "baseline_image_digest": self.baseline["image_digest"],
                }.items()
            ),
            "Recovery state differs from selected releases or target",
        )

    def report(self):
        report = self.state.load() or {
            "status": "incomplete",
            "candidate": self.candidate,
            "baseline": self.baseline,
            "attempts": [],
            "owned": None,
        }
        require(
            report.get("candidate") == self.candidate
            and report.get("baseline") == self.baseline
            and report.get("status") in ("incomplete", "passed")
            and isinstance(report.get("attempts"), list),
            "Recovery state differs from qualified artifacts",
        )
        return report

    def advance(self, current, stage, receipt):
        report = self.report()
        require([attempt["stage"] for attempt in report["attempts"]][-1:] == [current], "Recovery attempt is not at the expected protected stage")
        report["attempts"].append({"stage": stage, "receipt": receipt})
        self.state.retain(report)
        return report

    def begin(self, create):
        report = self.report()
        require(not report["attempts"] and report["owned"] is None, "Recovery fixture already has a retained intent")
        self.state.retain(report)
        owned = create()
        require(
            isinstance(owned, dict)
            and owned.get("run_id") == self.state.binding["run_id"]
            and all(owned.get(field) for field in ("product_id", "history_ref", "idempotency_key", "idempotency_receipt"))
            and set(owned)
            <= {
                "run_id",
                "product_id",
                "project_id",
                "tenant_id",
                "actor_id",
                "source_revision",
                "history_ref",
                "idempotency_key",
                "idempotency_receipt",
            },
            "Recovery fixture lacks verified durable ownership",
        )
        report["owned"] = owned
        report["attempts"].append({"stage": "created", "receipt": owned})
        self.state.retain(report)
        return report

    def adopt(self, owned):
        report = self.report()
        require(not report["attempts"] and report["owned"] is None, "Recovery cannot adopt a second owned Product")
        require(
            isinstance(owned, dict)
            and owned.get("run_id") == self.state.binding["run_id"]
            and owned.get("source_revision") == self.candidate["source_revision"]
            and all(
                owned.get(field)
                for field in ("product_id", "project_id", "tenant_id", "actor_id", "history_ref", "idempotency_key", "idempotency_receipt")
            )
            and set(owned)
            <= {
                "run_id",
                "product_id",
                "project_id",
                "tenant_id",
                "actor_id",
                "source_revision",
                "history_ref",
                "idempotency_key",
                "idempotency_receipt",
            },
            "Recovery requires the original held Product and exact receipts",
        )
        report["owned"] = owned
        report["attempts"].append({"stage": "created", "receipt": owned})
        self.state.retain(report)
        return report

    def quiesce(self, disable, observe):
        report = self.advance("created", "quiesce_started", self.candidate)
        receipt = disable()
        require(isinstance(receipt, dict) and receipt.get("admissions_disabled") is True, "Product admissions did not close")
        observed = observe(report["owned"])
        check_readback(report["owned"], observed, self.candidate, admissions=False)
        return self.advance("quiesce_started", "quiesced", observed)

    def rollback(self, switch):
        self.advance("quiesced", "baseline_restore_started", self.baseline)
        switch(self.baseline, admissions=False)

    def verify_baseline(self, observe):
        report = self.report()
        require(report["attempts"][-1]["stage"] == "baseline_restore_started", "Baseline restore was not started in this run")
        observed = observe(report["owned"])
        check_readback(report["owned"], observed, self.baseline, admissions=False)
        return self.advance("baseline_restore_started", "baseline_verified", observed)

    def restore(self, switch):
        report = self.report()
        require(
            report["attempts"]
            and report["attempts"][-1]["stage"]
            in (
                "quiesce_started",
                "quiesced",
                "baseline_restore_started",
                "baseline_verified",
                "candidate_restore_started",
            ),
            "Candidate restoration has no qualified rollback attempt",
        )
        if report["attempts"][-1]["stage"] != "candidate_restore_started":
            report["attempts"].append({"stage": "candidate_restore_started", "receipt": self.candidate})
            self.state.retain(report)
        switch(self.candidate, admissions=True)

    def verify_candidate(self, observe):
        report = self.report()
        require(report["attempts"][-1]["stage"] == "candidate_restore_started", "Candidate restore was not started in this run")
        observed = observe(report["owned"])
        check_readback(report["owned"], observed, self.candidate, admissions=True)
        return self.advance("candidate_restore_started", "candidate_verified", observed)

    def finish(self, cleanup):
        report = self.report()
        require(
            any(attempt["stage"] == "baseline_verified" for attempt in report["attempts"]), "Interrupted rollback cannot pass as completed recovery"
        )
        require(
            report["attempts"][-1]["stage"] in ("candidate_verified", "cleanup_started"),
            "Candidate must be independently verified before owned cleanup",
        )
        if report["attempts"][-1]["stage"] == "candidate_verified":
            report = self.advance("candidate_verified", "cleanup_started", report["owned"])
        receipt = cleanup(report["owned"])
        require(
            isinstance(receipt, dict)
            and receipt.get("run_id") == report["owned"]["run_id"]
            and receipt.get("owned_absent") is True
            and receipt.get("shared_unchanged") is True,
            "Owned cleanup was not independently verified",
        )
        report = self.advance("cleanup_started", "cleaned", receipt)
        report["status"] = "passed"
        self.state.retain(report)
        return report
