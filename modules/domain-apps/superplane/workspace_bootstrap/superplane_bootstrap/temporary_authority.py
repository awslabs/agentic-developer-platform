"""Generation-fenced installation authority with durable, irreversible revocation.

Provider composition compiles the pinned grant inventory. Requests cannot supply
RBAC, policy ARNs or principals. Credential composition belongs to #5534/35.
"""

from __future__ import annotations

from .errors import BootstrapRefused


class TemporaryAuthority:
    def __init__(self, journal, backend):
        self.journal = journal
        self.backend = backend
        self.revoked = False

    def acquire(self):
        plan = self.backend.plan(self.journal)
        grants = plan.get("grants") if isinstance(plan, dict) else None
        if not isinstance(grants, list) or not grants:
            raise BootstrapRefused("temporary authority requires a pinned grant plan")
        keys = [s.get("key") for s in grants if isinstance(s, dict)]
        if (
            len(keys) != len(grants)
            or not all(
                isinstance(k, str) and k not in {"phase", "complete", ""} for k in keys
            )
            or len(set(keys)) != len(keys)
        ):
            raise BootstrapRefused("temporary authority grant keys are invalid")
        if not self.journal.begin(plan):
            # Even an empty prior plan belongs to a different execution. Recovery
            # must revoke it; two processes must never acquire the same generation.
            raise BootstrapRefused(
                "interrupted temporary authority requires revocation"
            )
        for spec in grants:
            key = spec["key"]
            with self.journal.fenced():
                _, progress = self.journal.read_locked()
                self._require_phase(progress, "acquiring")
                observed = self.backend.observe(spec)
                if observed is not None:
                    if spec.get("lifetime") in {"workspace", "resource"}:
                        self.backend.verify_adoption(spec, observed)
                        progress[key] = {"phase": "adopted", "identity": observed}
                        self.journal.write_locked(progress)
                        continue
                    raise BootstrapRefused(
                        "temporary grant would modify adopted access"
                    )
                if spec.get("adopted_identity") is not None:
                    raise BootstrapRefused("recorded workspace grant is missing")
                progress[key] = {"phase": "grant_intent"}
                self.journal.write_locked(progress)
            # Intent is durable before I/O. Recovery can win this gap, so check
            # phase again under the lock before any external mutation.
            with self.journal.fenced():
                _, progress = self.journal.read_locked()
                self._require_phase(progress, "acquiring")
                identity = self.backend.create(spec)
                self.backend.verify(spec, identity)
                progress[key] = {"phase": "granted", "identity": identity}
                self.journal.write_locked(progress)
        with self.journal.fenced():
            _, progress = self.journal.read_locked()
            self._require_phase(progress, "acquiring")
            self.backend.verify_worker_permissions()
            progress["phase"] = "active"
            self.journal.write_locked(progress)

    @staticmethod
    def _require_phase(progress, phase):
        if progress.get("phase") != phase:
            raise BootstrapRefused("temporary authority is not " + phase)

    def revoke(self, *, retain_workspace=False):
        """Retry every boundary, retaining the parent if dependent cleanup fails."""
        with self.journal.fenced(recovery=True):
            plan, progress = self.journal.read_locked()
            if retain_workspace:
                if not (
                    progress.get("phase") == "revoked"
                    and progress.get("retain_workspace")
                ):
                    self._require_phase(progress, "active")
                # This choice is committed before removing anything. Recovery must
                # retain the same service-owned interlock needed after revocation.
                progress["retain_workspace"] = True
            if progress.get("phase") != "revoked":
                progress["phase"] = "revoking"
                self.journal.write_locked(progress)
        for spec in reversed(plan["grants"]):
            key = spec["key"]
            try:
                with self.journal.fenced(recovery=True):
                    _, progress = self.journal.read_locked()
                    previous = progress.get(key)
                    if previous is None:
                        continue  # No durable intent: adopted objects are untouched.
                    if previous.get("phase") == "adopted":
                        continue
                    if (
                        previous.get("phase") == "revoked"
                        or spec.get("lifetime") == "resource"
                    ):
                        continue
                    if spec.get("lifetime") == "workspace" and progress.get(
                        "retain_workspace"
                    ):
                        # Namespace ownership and the bounded operational service
                        # remain in this immutable journal for retirement/adoption.
                        # Neither retains installer or registrar administration.
                        self.backend.verify(spec, self.backend.observe(spec))
                        continue
                    progress[key] = {**previous, "phase": "revoke_intent"}
                    self.journal.write_locked(progress)
                with self.journal.fenced(recovery=True):
                    _, progress = self.journal.read_locked()
                    previous = progress[key]
                    observed = self.backend.observe(spec)
                    if observed is not None:
                        expected = previous.get("identity")
                        if expected is not None and observed != expected:
                            raise BootstrapRefused(
                                "temporary grant immutable identity changed"
                            )
                        # Lost acknowledgements require the backend to prove the
                        # exact generation on the provider object, never its name alone.
                        self.backend.verify(spec, observed)
                        self.backend.delete(spec, observed)
                    if self.backend.observe(spec) is not None:
                        raise BootstrapRefused(
                            "temporary authority remains after revocation"
                        )
                    progress[key] = {
                        "phase": "revoked",
                        "identity": observed or previous.get("identity"),
                    }
                    self.journal.write_locked(progress)
            except Exception as exc:
                raise BootstrapRefused(
                    "temporary authority revocation is unresolved; registration refused"
                ) from exc
        with self.journal.fenced(recovery=True):
            _, progress = self.journal.read_locked()
            # Only durable intents authorize cleanup. A grant observed as adopted
            # before its intent was written must neither be deleted nor keep this
            # operation's completed revocation pending forever.
            self.backend.verify_revoked(plan, progress)
            progress.update(phase="revoked", complete=True)
            self.journal.write_locked(progress)
        self.revoked = True

    def mutate(self, call, *args, **kwargs):
        """Fence every worker mutation against acquisition and concurrent recovery."""
        with self.journal.fenced():
            _, progress = self.journal.read_locked()
            self._require_phase(progress, "active")
            self.backend.verify_worker_binding()
            return call(*args, **kwargs)
