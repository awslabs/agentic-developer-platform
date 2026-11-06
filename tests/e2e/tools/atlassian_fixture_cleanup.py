"""Durable, run-owned Jira fixture cleanup; not an advertised mutation tool."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from secrets import token_urlsafe
from tempfile import mkstemp


class JiraFixtureLedger:
    def __init__(self, path, *, tenant_id, owner_id, run_id, site_id, project_key, operation_id):
        self.path = Path(path)
        self.identity = {
            "tenant_id": tenant_id, "owner_id": owner_id, "run_id": run_id,
            "site_id": site_id, "project_key": project_key, "operation_id": operation_id,
        }
        if (any(type(value) is not str or not value or len(value) > 512
                for value in self.identity.values())
                or self.path.parent.is_symlink() or self.path.parent.stat().st_mode & 0o077
                or self.path.is_symlink()):
            raise ValueError("A private, run-owned Jira fixture ledger is required")

    @contextmanager
    def _locked(self):
        handle = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(handle).st_mode & 0o077:
                raise ValueError("The Jira fixture lock must be private")
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def _read(self):
        if self.path.is_symlink():
            raise ValueError("The Jira fixture ledger cannot be a symlink")
        try:
            handle = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(handle) as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError("The Jira fixture ledger must be private")
            record = json.load(stream)
        if (type(record) is not dict or any(record.get(key) != value for key, value in self.identity.items())
                or type(record.get("marker")) is not str or not record["marker"].startswith("adp-jira-")
                or type(record.get("operation_ids")) is not list
                or self.identity["operation_id"] not in record["operation_ids"]
                or any(type(operation_id) is not str or not operation_id
                       for operation_id in record["operation_ids"])
                or len(set(record["operation_ids"])) != len(record["operation_ids"])
                or type(record.get("issue_ids")) is not list
                or any(type(issue_id) is not str or not issue_id for issue_id in record["issue_ids"])
                or len(set(record["issue_ids"])) != len(record["issue_ids"])
                or record.get("create_status") not in {"pending", "confirmed"}
                or record.get("cleanup_status") not in {"pending", "confirmed"}):
            raise ValueError("A different run or invalid record owns this Jira fixture")
        return record

    def _save(self, record):
        descriptor, temporary = mkstemp(dir=self.path.parent, prefix=".jira-fixture-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def reserve(self):
        with self._locked():
            if self._read() is not None:
                raise ValueError("The Jira fixture operation is already reserved")
            record = {**self.identity, "marker": "adp-jira-" + token_urlsafe(32),
                      "operation_ids": [self.identity["operation_id"]], "issue_ids": [],
                      "create_status": "pending", "cleanup_status": "pending"}
            self._save(record)
            return dict(record)

    def state(self):
        with self._locked():
            return self._read()

    def track_operation(self, operation_id):
        if type(operation_id) is not str or not operation_id or len(operation_id) > 512:
            raise ValueError("A bounded Jira fixture operation ID is required")
        with self._locked():
            record = self._read()
            if record is None or record["cleanup_status"] != "pending":
                raise ValueError("Original Jira fixture reservation required")
            if operation_id not in record["operation_ids"]:
                record["operation_ids"].append(operation_id)
                self._save(record)

    def _observed(self, provider, record):
        result = provider.read_issues(site_id=record["site_id"], project_key=record["project_key"],
                                      marker=record["marker"])
        if (type(result) is not dict or result.get("complete") is not True
                or type(result.get("issues")) is not list):
            raise ValueError("Independent Jira fixture readback is incomplete")
        issues = {}
        for issue in result["issues"]:
            if (type(issue) is not dict or type(issue.get("id")) is not str or not issue["id"]
                    or any(issue.get(key) != record[key] for key in
                           ("tenant_id", "owner_id", "run_id", "site_id", "project_key", "marker"))
                    or issue["id"] in issues):
                raise ValueError("Unowned or duplicate Jira fixture cannot be deleted")
            issues[issue["id"]] = issue
        for issue_id in set(record["issue_ids"]) | set(issues):
            direct = provider.read_issue(issue_id, site_id=record["site_id"])
            if (direct is not None and (type(direct) is not dict or direct != issues.get(issue_id))
                    or direct is None and issue_id in issues):
                raise ValueError("Known Jira fixture changed or independent readback disagrees")
        return issues

    def confirm_created(self, provider, issue_id):
        with self._locked():
            record = self._read()
            if record is None or record["cleanup_status"] != "pending":
                raise ValueError("Original Jira fixture reservation required")
            if issue_id not in self._observed(provider, record):
                raise ValueError("Jira fixture creation lacks independent ownership readback")
            record["issue_ids"] = sorted(set(record["issue_ids"]) | {issue_id})
            record["create_status"] = "confirmed"
            self._save(record)

    def cleanup(self, provider, *, pending_operations):
        if not callable(pending_operations):
            raise ValueError("Original operation journal is required")
        with self._locked():
            record = self._read()
            if record is None:
                raise ValueError("Original Jira fixture reservation required")
            observed = self._observed(provider, record)
            record["issue_ids"] = sorted(set(record["issue_ids"]) | set(observed))
            pending = [pending_operations(operation_id) for operation_id in record["operation_ids"]]
            if any(type(value) is not bool for value in pending):
                raise ValueError("Original operation journal status is unavailable")
            if any(pending):
                record["cleanup_status"] = "pending"
                self._save(record)
                return {"status": "pending", "owned_absent": not observed}
            if record["cleanup_status"] == "confirmed" and not observed:
                return {"status": "confirmed", "owned_absent": True}
            record["cleanup_status"] = "pending"
            self._save(record)
            for issue_id in observed:
                provider.delete_owned_issue(issue_id, **{
                    field: record[field] for field in
                    ("tenant_id", "owner_id", "run_id", "site_id", "project_key", "marker")
                })
            remaining = self._observed(provider, record)
            pending = [pending_operations(operation_id) for operation_id in record["operation_ids"]]
            if any(type(value) is not bool for value in pending):
                raise ValueError("Original operation journal status is unavailable")
            if remaining or any(pending) or record["create_status"] != "confirmed":
                return {"status": "pending", "owned_absent": not remaining}
            record["cleanup_status"] = "confirmed"
            self._save(record)
            return {"status": "confirmed", "owned_absent": True}
