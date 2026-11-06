"""Harness-controller authority for bounded, run-owned assistant fault actions."""

from . import cleanup, ports

CONTROLLER_FAULTS = frozenset(
    {
        "assistant_duplicate_delivery",
        "assistant_worker_loss",
        "assistant_queue_unavailable",
        "assistant_supervisor_restart",
    }
)
ASSISTANT_FAULTS = CONTROLLER_FAULTS | {"assistant_disconnect"}


class AssistantFaultController:
    def __init__(self, *, inspect=None, mutate=None, inspect_reservations=None):
        self.inspect = inspect
        self.mutate = mutate
        self.inspect_reservations = inspect_reservations

    def reservation_limitation(self, manifest, evaluation_id):
        if not callable(self.inspect_reservations):
            return None
        if manifest is None or manifest.prefix != evaluation_id or not evaluation_id:
            raise ports.PortError(
                "Assistant reservation inspection requires the run manifest"
            )
        if manifest.read().get("prefix") != evaluation_id:
            raise ports.PortError(
                "Assistant reservation inspection belongs to another run"
            )
        proof = self.inspect_reservations(evaluation_id=evaluation_id)
        if proof is None:
            return None
        if (
            not isinstance(proof, dict)
            or proof.get("controller_identity") != "harness_controller"
            or proof.get("evaluation_id") != evaluation_id
            or proof.get("creation_run_owned") is not True
            or proof.get("reservation_persisted") is not True
            or proof.get("pod_observed") is not False
            or proof.get("observation_source") != "reservation_and_pod_readback"
            or not isinstance(proof.get("reservation_id"), str)
            or not proof["reservation_id"]
        ):
            raise ports.PortError(
                "Assistant reservation inspection did not prove an unobserved run-owned pod"
            )
        return {
            "criterion": "QUAL03",
            "status": "known_limitation",
            "reservation_recorded": True,
            "pod_observed": False,
            "observation_source": "reservation_and_pod_readback",
            "reservation_id": proof["reservation_id"],
        }

    def _scope(self, manifest, evaluation_id, fault, target, fixture):
        if (
            manifest is None
            or not evaluation_id
            or manifest.prefix != evaluation_id
            or not isinstance(target, dict)
            or target.get("evaluation_id") != evaluation_id
            or not isinstance(fixture, dict)
            or not isinstance(target.get("session_id"), str)
            or not target["session_id"]
            or not isinstance(target.get("worker_id"), str)
            or not target["worker_id"]
            or not isinstance(fixture.get("canonical_user_id"), str)
            or target.get("user_label") not in {"a1", "a2", "b1"}
        ):
            raise ports.PortError(
                "Assistant fault refused: target and run identity must match"
            )
        if fault not in ASSISTANT_FAULTS:
            raise ports.PortError(
                "Assistant fault refused: no explicit assistant fault selected"
            )
        if not callable(self.inspect):
            raise ports.PortError(
                "Assistant fault controller lacks a verified run-owned inspection or "
                "mutation capability; approve scoped controller IAM separately"
            )
        inspected = self.inspect(
            evaluation_id=evaluation_id,
            session_id=target["session_id"],
            worker_id=target["worker_id"],
        )
        if (
            not isinstance(inspected, dict)
            or inspected.get("controller_identity") != "harness_controller"
            or inspected.get("evaluation_id") != evaluation_id
            or inspected.get("session_id") != target["session_id"]
            or inspected.get("worker_id") != target["worker_id"]
            or inspected.get("owner_user_id") != fixture["canonical_user_id"]
            or inspected.get("session_run_owned") is not True
            or inspected.get("worker_run_owned") is not True
            or fault == "assistant_queue_unavailable"
            and (
                inspected.get("consumer_run_owned") is not True
                or inspected.get("consumer_id") != target.get("consumer_id")
                or not target.get("consumer_id")
            )
            or fault == "assistant_supervisor_restart"
            and inspected.get("supervisor_run_owned") is not True
        ):
            raise ports.PortError(
                "Assistant fault refused: controller did not prove run-owned scope"
            )
        return (
            ("assistant_session", target["session_id"]),
            ("assistant_worker", target["worker_id"]),
        )

    def register(self, manifest, evaluation_id, fault, target, fixture):
        resources = self._scope(manifest, evaluation_id, fault, target, fixture)
        recorded = manifest.read()
        if recorded.get("prefix") != evaluation_id:
            raise ports.PortError(
                "Assistant fault refused: manifest belongs to another run"
            )
        for kind, identifier in resources:
            if any(
                row.get("kind") == kind and row.get("id") == identifier
                for row in recorded.get("resources", [])
            ):
                raise ports.PortError(
                    "Assistant fault refused: target already registered"
                )
        for kind, identifier in resources:
            manifest.record(
                kind,
                identifier,
                detail={
                    "case": "E47",
                    "session_id": target["session_id"],
                    "worker_id": target["worker_id"],
                    "owner_user_id": fixture["canonical_user_id"],
                },
            )

    def authorize(self, manifest, evaluation_id, fault, target, fixture):
        resources = self._scope(manifest, evaluation_id, fault, target, fixture)
        recorded = manifest.read()
        if recorded.get("prefix") != evaluation_id:
            raise ports.PortError(
                "Assistant fault refused: manifest belongs to another run"
            )
        for kind, identifier in resources:
            prior = next(
                (
                    row
                    for row in recorded.get("resources", [])
                    if row.get("kind") == kind and row.get("id") == identifier
                ),
                None,
            )
            if prior is None or (
                not cleanup.owned(prior, evaluation_id)
                or prior.get("status") != cleanup.PENDING
                or prior.get("detail", {}).get("session_id") != target["session_id"]
                or prior.get("detail", {}).get("worker_id") != target["worker_id"]
                or prior.get("detail", {}).get("owner_user_id")
                != fixture["canonical_user_id"]
            ):
                raise ports.PortError(
                    "Assistant fault refused: unowned or missing manifest resource"
                )
        if fault == "assistant_disconnect":
            return None
        if not callable(self.mutate):
            raise ports.PortError(
                "Assistant fault controller mutation capability is unavailable; "
                "approve scoped controller IAM separately"
            )
        intent = "assistant_fault_" + fault
        if intent + ":" + evaluation_id in recorded.get("diagnostic_intents", {}):
            raise ports.PortError(
                "Assistant fault action was already attempted; reconcile before repeating it"
            )
        manifest.record_diagnostic(
            intent,
            {
                "evaluation_id": evaluation_id,
                "session_id": target["session_id"],
                "worker_id": target["worker_id"],
                "fault": fault,
            },
        )
        receipt = self.mutate(
            evaluation_id=evaluation_id,
            fault=fault,
            session_id=target["session_id"],
            worker_id=target["worker_id"],
            **(
                {"consumer_id": target["consumer_id"]}
                if fault == "assistant_queue_unavailable"
                else {}
            ),
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("controller_identity") != "harness_controller"
            or receipt.get("evaluation_id") != evaluation_id
            or receipt.get("fault") != fault
            or receipt.get("session_id") != target["session_id"]
            or receipt.get("worker_id") != target["worker_id"]
            or receipt.get("injected") is not True
        ):
            raise ports.PortError(
                "Assistant fault controller mutation lacks a matching receipt"
            )
        return receipt
