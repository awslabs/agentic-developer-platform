"""Reconcile an owned onboarding step after its response is lost."""

import re
from copy import deepcopy


class SessionRecovery:
    def __init__(self, fixture, read_session, change_ids):
        self.fixture = fixture
        self.store = fixture["state_store"]
        self.read_session = read_session
        self.change_ids = change_ids

    def owned(self):
        document, manifest = self.store.load(self.fixture["run_id"])
        if (not isinstance(document, dict) or not isinstance(manifest, dict)
                or document.get("receipt") != {"product_id": self.fixture["product_id"],
                                               "project_id": self.fixture["project_id"]}
                or any(document.get(name) != self.fixture[name] or manifest.get(name) != self.fixture[name]
                       for name in ("run_id", "tenant_id", "actor_id", "source_revision"))
                or any(manifest.get(name) != self.fixture[name] for name in ("product_id", "project_id"))):
            raise ValueError("Recovery has no original owned Product receipt")
        return document, manifest

    def record(self, session_id, revision, key, body):
        document, manifest = self.owned()
        if (document.get("pending_onboarding") is not None
                or not isinstance(session_id, str) or not session_id
                or type(revision) is not int or revision < 1
                or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key)
                or not isinstance(body, dict) or body.get("action") != "later"
                or not isinstance(body.get("reason"), str) or not body["reason"].strip()):
            raise ValueError("Recovery needs one bounded original step intent")
        session = self.read_session(session_id)
        before = self.change_ids()
        if (session.get("revision") != revision or session.get("steps", {}).get("financials", {}).get("status") != "in_progress"
                or not isinstance(before, set) or not before or not all(isinstance(item, str) and item for item in before)):
            raise ValueError("Recovery baseline differs from the pending step")
        pending = {"session_id": session_id, "revision": revision, "key": key, "body": deepcopy(body),
                   "before_change_ids": sorted(before), "attempts": [{"attempt_id": "1", "result": "intent_saved"}]}
        document["pending_onboarding"] = pending
        self.store.save(self.fixture["run_id"], document, manifest=manifest)
        if self.owned()[0].get("pending_onboarding") != pending:
            raise RuntimeError("Pending onboarding reference was not durable")
        return pending

    def reconcile(self, replay):
        document, manifest = self.owned()
        pending = document.get("pending_onboarding")
        if not isinstance(pending, dict) or document.get("onboarding_result") is not None:
            raise ValueError("Original pending onboarding step is missing or already reconciled")
        session = self.read_session(pending["session_id"])
        if session["revision"] == pending["revision"]:
            if session["steps"]["financials"]["status"] != "in_progress":
                raise AssertionError("Uncommitted step differs from its baseline")
            replay(deepcopy(pending))
            pending["attempts"].append({"attempt_id": "2", "result": "original_key_replayed"})
            session = self.read_session(pending["session_id"])
        else:
            pending["attempts"].append({"attempt_id": "2", "result": "readback_after_lost_ack"})
        before = set(pending["before_change_ids"])
        after = self.change_ids()
        new_changes = after - before
        if (session["id"] != pending["session_id"] or session["revision"] != pending["revision"] + 1
                or session["steps"]["financials"]["status"] != "later"
                or session["steps"]["financials"].get("reason") != pending["body"]["reason"]
                or not isinstance(after, set) or not before.issubset(after) or len(new_changes) != 1):
            raise AssertionError("Interrupted onboarding step did not commit exactly once")
        result = {"session_id": pending["session_id"], "revision": session["revision"],
                  "change_id": new_changes.pop(), "attempts": pending["attempts"]}
        document["onboarding_result"] = result
        self.store.save(self.fixture["run_id"], document, manifest=manifest)
        if self.owned()[0].get("onboarding_result") != result:
            raise RuntimeError("Onboarding recovery result was not durable")
        return result


def assert_cleanup_receipt(receipt, fixture):
    if (not isinstance(receipt, dict)
            or any(receipt.get(name) != fixture[name] for name in ("run_id", "product_id", "project_id"))
            or receipt.get("fixture_removed") is not True or receipt.get("owned_absent") is not True
            or receipt.get("shared_unchanged") is not True):
        raise AssertionError("Recovery cleanup did not prove owned absence and shared preservation")
