"""Run-owned operation reservations and direct Atlassian provider readback."""

import fcntl
import hashlib
import http.client
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkstemp
from urllib.parse import quote, urlencode
from uuid import UUID

from tests.e2e.tools.atlassian_mutation_observer import read_snapshot, verify_mutation

READS = frozenset({"search_jira", "get_jira_issue", "search_confluence", "get_confluence_page"})
WRITES = frozenset({"create_jira_issue", "update_jira_issue", "comment_jira_issue", "transition_jira_issue"})
CLIENTS = frozenset({"claude-code", "codex"})
OPERATION_ORDER = ("create_jira_issue", "get_jira_issue", "search_jira", "get_confluence_page", "search_confluence",
                   "update_jira_issue", "comment_jira_issue", "transition_jira_issue")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MARKER = re.compile(r"adp-8454-[a-z0-9]{16,64}\Z")
HANDLE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ACCOUNT_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def valid_native_binding(binding, tool):
    return (isinstance(tool, str) and tool in READS | WRITES and type(binding) is dict
            and set(binding) == {"transport_sha256", "reference", "tool", "arguments_sha256", "max_requests"}
            and all(isinstance(binding[key], str) and SHA256.fullmatch(binding[key])
                    for key in ("transport_sha256", "arguments_sha256"))
            and binding["tool"] == "atlassian___" + tool and type(binding["max_requests"]) is int
            and binding["max_requests"] == 1 and type(binding["reference"]) is dict
            and set(binding["reference"]) == {"run_id", "tenant_id", "approval_revision", "deployment_id",
                "gateway_origin", "source_revision", "definition_revision", "github_run_id", "github_run_attempt"}
            and all(isinstance(value, str) and 0 < len(value) <= 512 for value in binding["reference"].values())
            and all(canonical_uuid(binding["reference"][key]) for key in ("run_id", "approval_revision")))


def valid_native_receipt(receipt, binding):
    if (type(receipt) is not dict
            or set(receipt) != {"source", "binding", "operation_ref", "transport_operation_id", "request_id", "tool",
                               "execution_id", "receipt_id", "request_sha256", "request_value_sha256", "response_sha256", "journal_entry_sha256"}
            or receipt["source"] != "native-transport-journal" or receipt["binding"] != binding
            or receipt["tool"] != binding["tool"] or receipt["request_id"] != receipt["operation_ref"]
            or not all(canonical_uuid(receipt[key]) for key in ("operation_ref", "transport_operation_id", "execution_id"))
            or not all(isinstance(receipt[key], str) and SHA256.fullmatch(receipt[key])
                       for key in ("request_sha256", "request_value_sha256", "response_sha256", "journal_entry_sha256"))):
        return False
    return receipt["receipt_id"] == "tools-test-effect:" + digest({
        "tenant_id": binding["reference"]["tenant_id"], "run_id": binding["reference"]["run_id"],
        "operation_id": receipt["transport_operation_id"]})


def valid_snapshot(snapshot, run_id, operation_id, row, *, baseline):
    if type(snapshot) is not dict or set(snapshot) != {
        "source", "run_id", "client", "tool", "operation_id", "marker", "provider_id",
        "site_id", "account_id", "content_sha256", "effect_count",
    }:
        return False
    if (snapshot["source"] != "direct-atlassian-provider" or snapshot["run_id"] != run_id
            or snapshot["operation_id"] != operation_id
            or any(snapshot[key] != row[key] for key in ("client", "tool", "marker"))
            or not isinstance(snapshot["site_id"], str) or not HANDLE.fullmatch(snapshot["site_id"])
            or not isinstance(snapshot["account_id"], str) or not ACCOUNT_ID.fullmatch(snapshot["account_id"])):
        return False
    missing_create = row["tool"] == "create_jira_issue" and snapshot["provider_id"] is None
    if (not missing_create and (not isinstance(snapshot["provider_id"], str)
                                or not HANDLE.fullmatch(snapshot["provider_id"]))):
        return False
    if (snapshot["content_sha256"] is None and not (missing_create and baseline)
            or snapshot["content_sha256"] is not None and (not isinstance(snapshot["content_sha256"], str)
                                                         or not SHA256.fullmatch(snapshot["content_sha256"]))):
        return False
    return type(snapshot["effect_count"]) is int and snapshot["effect_count"] == (
        0 if baseline or row["tool"] in READS else 1)


def valid_operation_fields(row):
    if "provider_observation" in row:
        if (row.get("status") != "confirmed" or type(row.get("expected")) is not dict
                or type(row["provider_observation"]) is not dict
                or digest(row["provider_observation"]) != digest({**row["expected"], "provider_id": row.get("provider_id")})):
            return False
        row = {key: value for key, value in row.items() if key != "provider_observation"}
    if "native_binding" in row:
        binding = row["native_binding"]
        if not valid_native_binding(binding, row.get("tool")):
            return False
        if row["status"] == "confirmed":
            if not valid_native_receipt(row.get("native_receipt"), binding):
                return False
        elif "native_receipt" in row:
            return False
        row = {key: value for key, value in row.items() if key not in {"native_binding", "native_receipt"}}
    fields = {"client", "tool", "marker", "status", "expected", "baseline", "provider_id"}
    if set(row) == fields:
        return True
    before = row.get("mutation_baseline")
    expected = row.get("expected")
    baseline = row.get("baseline")
    change = row.get("mutation_expected")
    return (set(row) == fields | {"mutation_baseline", "mutation_expected"}
            and row["tool"] in WRITES - {"create_jira_issue"}
            and type(before) is dict and set(before) == {"version", "binding", "resource", "events", "sha256"}
            and type(before["version"]) is int and before["version"] == 1
            and type(before["resource"]) is dict and type(before["events"]) is dict
            and before["sha256"] == digest({key: value for key, value in before.items() if key != "sha256"})
            and type(expected) is dict and type(baseline) is dict and type(change) is dict
            and before["binding"] == {"site_id": expected.get("site_id"), "issue_id": expected.get("provider_id"),
                                      "account_id": expected.get("account_id"), "operation": row["tool"], "marker": row["marker"]}
            and baseline.get("content_sha256") == digest(before["resource"])
            and expected.get("content_sha256") == digest(change))


class IndependentProviderReader:
    def __init__(self, lifecycle, owner_id):
        self.lifecycle = lifecycle
        self.owner_id = owner_id

    @staticmethod
    def _get(path, token):
        connection = http.client.HTTPSConnection("api.atlassian.com", timeout=5)
        try:
            connection.request("GET", path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("Independent Atlassian provider read was denied")
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("Independent provider response exceeds bound")
            return json.loads(body)
        finally:
            connection.close()

    def mutation_snapshot(self, *, tool, marker, site_id, provider_id):
        if tool not in WRITES - {"create_jira_issue"}:
            raise ValueError("An operation-specific mutation reader is required")

        def observe(token):
            account_id = self._authorize(token, site_id, "jira")
            return read_snapshot(lambda path: self._get(path, token), site_id=site_id, issue_id=provider_id,
                                 account_id=account_id, operation=tool, marker=marker)

        return self.lifecycle.with_access_token(self.owner_id, observe)

    def _authorize(self, token, site_id, product):
        resources = self._get("/oauth/token/accessible-resources", token)
        required_scope = "read:jira-work" if product == "jira" else "read:confluence-content.all"
        if (not isinstance(resources, list) or len(resources) > 32
                or not any(isinstance(site, dict) and site.get("id") == site_id
                           and isinstance(site.get("scopes"), list) and required_scope in site["scopes"] for site in resources)):
            raise ValueError("No independently admitted REST read grant for the selected site")
        account = self._get("/me", token)
        if not isinstance(account, dict) or not isinstance(account.get("account_id"), str) or not account["account_id"]:
            raise ValueError("Independently admitted provider identity unavailable")
        return account["account_id"]

    def readback(self, *, run_id, client, tool, operation_id, marker, site_id, provider_id,
                 mutation_baseline=None, mutation_expected=None):
        if (client not in CLIENTS or tool not in READS | WRITES or not MARKER.fullmatch(marker)
                or not HANDLE.fullmatch(site_id)
                or (provider_id is None and tool != "create_jira_issue")
                or (provider_id is not None and not HANDLE.fullmatch(provider_id))):
            raise ValueError("Run-owned provider query requires an exact site and resource")
        if tool in WRITES - {"create_jira_issue"}:
            if mutation_baseline is None or mutation_expected is None:
                raise ValueError("A marker search cannot count update, comment or transition effects")
            after = self.mutation_snapshot(tool=tool, marker=marker, site_id=site_id, provider_id=provider_id)
            result = verify_mutation(mutation_baseline, after, expected=mutation_expected)
            return {"source": "direct-atlassian-provider", "run_id": run_id, "client": client,
                    "tool": tool, "operation_id": operation_id, "marker": marker,
                    "provider_id": provider_id, "site_id": site_id, "account_id": after["binding"]["account_id"],
                    "content_sha256": result["content_sha256"], "effect_count": result["effect_count"]}
        product = "jira" if "jira" in tool else "confluence"
        prefix = f"/ex/{product}/{quote(site_id, safe='')}"
        def observe(token):
            account_id = self._authorize(token, site_id, product)
            count = 0
            resource_id = provider_id
            if tool in WRITES:
                query = urlencode({"jql": f'text ~ "{marker}"', "maxResults": 2})
                search = self._get(f"{prefix}/rest/api/3/search/jql?{query}", token)
                issues = search.get("issues") if isinstance(search, dict) else None
                if (not isinstance(issues, list) or search.get("isLast") is not True or len(issues) > 1
                        or any(not isinstance(issue, dict) or not HANDLE.fullmatch(str(issue.get("id")))
                               or (resource_id is not None and str(issue["id"]) != resource_id) for issue in issues)):
                    raise ValueError("Independent marker search is incomplete or ambiguous")
                count = len(issues)
                if resource_id is None and issues:
                    resource_id = str(issues[0]["id"])
            projection = None
            if resource_id is not None:
                path = (f"{prefix}/rest/api/3/issue/{quote(resource_id, safe='')}"
                        if product == "jira" else f"{prefix}/wiki/api/v2/pages/{quote(resource_id, safe='')}?body-format=storage")
                resource = self._get(path, token)
                if not isinstance(resource, dict) or str(resource.get("id")) != resource_id:
                    raise ValueError("Provider resource differs from independent readback")
                if product == "jira":
                    fields = resource.get("fields")
                    if not isinstance(fields, dict) or not isinstance(fields.get("summary"), str):
                        raise ValueError("Jira sentinel content is unavailable")
                    projection = {"summary": fields["summary"], "description": fields.get("description")}
                    if tool == "create_jira_issue" and (
                        not isinstance(fields.get("creator"), dict)
                        or fields["creator"].get("accountId") != account_id
                        or marker not in fields["summary"] and marker not in json.dumps(fields.get("description"))
                    ):
                        raise ValueError("The created Jira issue lacks the owned marker or author")
                else:
                    body = resource.get("body")
                    storage = body.get("storage") if isinstance(body, dict) else None
                    if (not isinstance(resource.get("title"), str) or not isinstance(storage, dict)
                            or not isinstance(storage.get("value"), str)):
                        raise ValueError("Confluence sentinel content is unavailable")
                    projection = {"title": resource["title"], "body": storage["value"]}
            return {"source": "direct-atlassian-provider", "run_id": run_id, "client": client,
                    "tool": tool, "operation_id": operation_id, "marker": marker,
                    "provider_id": resource_id, "site_id": site_id, "account_id": account_id,
                    "content_sha256": digest(projection) if projection is not None else None, "effect_count": count}

        return self.lifecycle.with_access_token(self.owner_id, observe)


class ClientOperationLedger:
    def __init__(self, path, *, run_id):
        self.path = Path(path)
        self.run_id = run_id
        if (not isinstance(run_id, str) or not HANDLE.fullmatch(run_id) or not self.path.parent.is_dir()
                or self.path.parent.is_symlink() or self.path.parent.stat().st_mode & 0o077
                or self.path.is_symlink()):
            raise ValueError("A private, run-owned client operation ledger is required")

    @contextmanager
    def _locked(self):
        descriptor = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(descriptor).st_mode & 0o077:
                raise ValueError("The operation lock must be private")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self):
        if self.path.is_symlink():
            raise ValueError("The operation ledger cannot be a symlink")
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"run_id": self.run_id, "operations": {}}
        with os.fdopen(descriptor) as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077 or os.fstat(stream.fileno()).st_size > 1024 * 1024:
                raise ValueError("The operation ledger must be bounded and private")
            record = json.load(stream)
        if (type(record) is not dict or set(record) != {"run_id", "operations"} or record["run_id"] != self.run_id
                or type(record["operations"]) is not dict or len(record["operations"]) > 128
                or any(not isinstance(operation_id, str) or not HANDLE.fullmatch(operation_id)
                       or type(row) is not dict or not valid_operation_fields(row)
                       or row["client"] not in CLIENTS or row["tool"] not in READS | WRITES
                       or row["status"] not in {"pending", "confirmed"}
                       or not isinstance(row["marker"], str) or not MARKER.fullmatch(row["marker"])
                       or (row["provider_id"] is not None and (not isinstance(row["provider_id"], str)
                                                               or not HANDLE.fullmatch(row["provider_id"])))
                       or (row["status"] == "pending" and row["provider_id"] is not None)
                       or (row["status"] == "confirmed" and row["provider_id"] is None)
                       or not valid_snapshot(row["expected"], self.run_id, operation_id, row, baseline=False)
                       or not valid_snapshot(row["baseline"], self.run_id, operation_id, row, baseline=True)
                       or any(row["expected"][key] != row["baseline"][key] for key in ("provider_id", "site_id", "account_id"))
                       for operation_id, row in record["operations"].items())):
            raise ValueError("A different run or invalid record owns the client operations")
        return record

    def _write(self, record):
        encoded = json.dumps(record, sort_keys=True)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError("The operation ledger exceeds its durable storage bound")
        descriptor, temporary = mkstemp(dir=self.path.parent, prefix=".atlassian-operation-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(encoded)
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

    def state(self):
        with self._locked():
            return self._read()

    def reserve(self, *, client, tool, operation_id, marker, provider_id, site_id, expected, observer, mutation_expected=None, native_binding=None):
        if (client not in CLIENTS or tool not in READS | WRITES or not isinstance(operation_id, str)
                or not HANDLE.fullmatch(operation_id) or not isinstance(marker, str) or not MARKER.fullmatch(marker)
                or not isinstance(observer, IndependentProviderReader)):
            raise ValueError("A distinct installed client, reviewed operation and direct provider reader are required")
        if native_binding is not None and not valid_native_binding(native_binding, tool):
            raise ValueError("The original native transport binding is invalid")
        fields = {"run_id": self.run_id, "client": client, "tool": tool, "operation_id": operation_id,
                  "marker": marker, "provider_id": provider_id, "site_id": site_id}
        mutation = {}
        if mutation_expected is not None:
            before = observer.mutation_snapshot(tool=tool, marker=marker, site_id=site_id, provider_id=provider_id)
            snapshot = {"source": "direct-atlassian-provider", **fields, "account_id": before["binding"]["account_id"],
                        "content_sha256": digest(before["resource"]), "effect_count": 0}
            if not isinstance(mutation_expected, dict) or expected.get("content_sha256") != digest(mutation_expected):
                raise ValueError("The reserved mutation must match its expected content")
            mutation = {"mutation_baseline": before, "mutation_expected": mutation_expected}
        else:
            snapshot = observer.readback(**fields)
        row = {"client": client, "tool": tool, "marker": marker}
        if (not valid_snapshot(snapshot, self.run_id, operation_id, row, baseline=True)
                or not valid_snapshot(expected, self.run_id, operation_id, row, baseline=False)
                or set(expected) != set(snapshot)
                or any(expected[key] != snapshot[key] for key in fields)
                or expected["account_id"] != snapshot["account_id"]
                or (tool in READS and expected["content_sha256"] != snapshot["content_sha256"])):
            raise ValueError("Independent provider baseline and run-owned sentinel are required")
        with self._locked():
            record = self._read()
            if operation_id in record["operations"] or any(row["marker"] == marker for row in record["operations"].values()):
                raise ValueError("Client operation or provider marker is already reserved")
            if len(record["operations"]) >= 128:
                raise ValueError("Client operation ledger is full")
            record["operations"][operation_id] = {"client": client, "tool": tool, "marker": marker,
                                                   "status": "pending", "baseline": snapshot, "expected": expected,
                                                   "provider_id": None, **mutation,
                                                   **({"native_binding": native_binding} if native_binding is not None else {})}
            self._write(record)
        return operation_id

    def confirm(self, *, operation_id, client_result, receipt, observer):
        if not isinstance(observer, IndependentProviderReader):
            raise ValueError("A direct provider reader is required")
        with self._locked():
            record = self._read()
            row = record["operations"].get(operation_id)
            if row is None or row["status"] != "pending":
                raise ValueError("An unresolved, run-owned operation is required")
            expected = row["expected"]
            if "native_binding" in row and not valid_native_receipt(receipt, row["native_binding"]):
                raise ValueError("The actual native request, execution and receipt must match its original reservation")
            fields = {key: expected[key] for key in ("run_id", "client", "tool", "operation_id", "marker", "provider_id", "site_id")}
            fields.update({key: row[key] for key in ("mutation_baseline", "mutation_expected") if key in row})
            after = observer.readback(**fields)
            qualified = {**expected, "provider_id": after["provider_id"]} if expected["provider_id"] is None else expected
            if (after != qualified or not isinstance(client_result, dict)
                    or client_result != {key: qualified[key] for key in ("provider_id", "site_id", "account_id", "content_sha256")}
                    or "native_binding" not in row and receipt != {"source": "gateway-operation-receipt", "operation_id": operation_id,
                                   "client": row["client"], "status": "committed",
                                   "account_id": qualified["account_id"], "provider_id": qualified["provider_id"]}):
                raise ValueError("Client result, admitted identity, receipt or provider effect differs")
            row["status"] = "confirmed"
            row["provider_id"] = qualified["provider_id"]
            row["provider_observation"] = after
            if "native_binding" in row:
                row["native_receipt"] = receipt
            self._write(record)
            return {"status": "confirmed", "client": row["client"], "tool": row["tool"],
                    "provider_id": qualified["provider_id"], "effect_count": expected["effect_count"]}


def observe_cell(ledger, observer, *, client, tool):
    if not isinstance(ledger, ClientOperationLedger) or not isinstance(observer, IndependentProviderReader):
        raise ValueError("A private operation ledger and independent provider reader are required")
    operations = [(operation_id, row) for operation_id, row in ledger.state()["operations"].items()
                  if row["client"] == client and row["tool"] == tool]
    if len(operations) != 1 or operations[0][1]["status"] != "confirmed":
        raise ValueError("Exactly one confirmed installed-client operation is required for each cell")
    operation_id, row = operations[0]
    expected = {**row["expected"], "provider_id": row["provider_id"]}
    fields = {key: expected[key] for key in (
        "run_id", "client", "tool", "operation_id", "marker", "site_id", "provider_id")}
    fields.update({key: row[key] for key in ("mutation_baseline", "mutation_expected") if key in row})
    if observer.readback(**fields) != expected:
        raise ValueError("Independent provider readback differs from the confirmed client operation")
    return {"source": "confirmed-client-operation-and-direct-provider", "operation_id": operation_id,
            "content_sha256": expected["content_sha256"],
            "effect_count": expected["effect_count"]}


def validate_isolated_fixtures(fixtures):
    if type(fixtures) is not dict or set(fixtures) != CLIENTS:
        raise ValueError("Both isolated installed-client fixtures are required")
    paths, roots, run_ids, owners = set(), set(), set(), set()
    for client in sorted(CLIENTS):
        fixture = fixtures[client]
        if type(fixture) is not dict or set(fixture) not in ({"ledger", "reader", "owner_id"}, {"ledger", "reader", "owner_id", "cleanup_provider"}):
            raise ValueError("Each client needs a private ledger, owner and provider reader binding")
        ledger, reader, owner_id = fixture["ledger"], fixture["reader"], fixture["owner_id"]
        if "cleanup_provider" in fixture:
            from tests.e2e.tools.atlassian_fixture_provider import JiraFixtureProvider

            provider = fixture["cleanup_provider"]
            if type(provider) is not JiraFixtureProvider or provider.owner_id != owner_id or provider.run_id != ledger.run_id:
                raise ValueError("Separate cleanup authority must belong to the same client owner and run")
        if (not isinstance(ledger, ClientOperationLedger)
                or ledger.path.parent.name != client and recovery_roots(ledger.path.parent.parent).get(client) != ledger.path.parent
                or ledger.path.parent.is_symlink()
                or not isinstance(owner_id, str) or not HANDLE.fullmatch(owner_id)
                or reader is not None and (not isinstance(reader, IndependentProviderReader)
                                           or getattr(reader, "owner_id", None) != owner_id)):
            raise ValueError("Client ledger and provider identity must be isolated and bound")
        root = ledger.path.parent.parent
        if (root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid()
                or root.stat().st_mode & 0o077 or ledger.path.parent.stat().st_uid != os.getuid()
                or any(row["client"] != client for row in ledger.state()["operations"].values())):
            raise ValueError("Client ledger belongs to another run or client")
        paths.add(ledger.path)
        roots.add(root)
        run_ids.add(ledger.run_id)
        owners.add(owner_id)
    if len(paths) != len(CLIENTS) or len(roots) != 1 or len(run_ids) != 1 or len(owners) != len(CLIENTS):
        raise ValueError("Distinct client ledgers and owners must share one private run root")
    return fixtures


def recovery_roots(root):
    from tests.e2e.tools.atlassian_setup_live import private_file

    path = root / "recovery-roots.json"
    if not path.exists() and not path.is_symlink():
        return {}
    value = json.loads(private_file(path, max_bytes=8192))
    if type(value) is not dict or not set(value) <= CLIENTS:
        raise ValueError("Original recovery roots are invalid")
    result = {}
    for client, name in value.items():
        if type(name) is not str or not re.fullmatch(r"restored-native-[A-Za-z0-9_-]+", name):
            raise ValueError("Original recovery root must remain in its private manifest directory")
        destination = root / name
        if (destination.is_symlink() or destination.resolve(strict=True) != destination
                or not destination.is_dir() or destination.stat().st_uid != os.getuid()
                or destination.stat().st_mode & 0o077):
            raise ValueError("Original recovery root must be private")
        result[client] = destination
    if len(set(result.values())) != len(result):
        raise ValueError("Restored client roots must remain isolated")
    return result


def load_isolated_fixtures(manifest_file, *, require_readers=True):
    path = Path(manifest_file)
    root = path.parent
    if (not path.is_absolute() or path.is_symlink() or root.is_symlink() or not root.is_dir()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise ValueError("A private, run-owned client manifest is required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or not 0 < metadata.st_size <= 8192):
            raise ValueError("Client manifest must be a bounded private regular file")
        manifest = json.loads(stream.read(8193))
    if (type(manifest) is not dict or set(manifest) not in ({"run_id", "owners"}, {"run_id", "owners", "readback"}, {"run_id", "owners", "readback", "cleanup"})
            or not isinstance(manifest["run_id"], str) or not HANDLE.fullmatch(manifest["run_id"])
            or type(manifest["owners"]) is not dict or set(manifest["owners"]) != CLIENTS):
        raise ValueError("Client manifest must name exactly two owners in one run")
    readers = {client: None for client in CLIENTS}
    if "readback" in manifest:
        from tests.e2e.tools.atlassian_readback_grant import IdentityReadbackGrant

        if type(manifest["readback"]) is not dict or set(manifest["readback"]) != CLIENTS:
            raise ValueError("Both independent MCP readback bindings are required")
        grants = {client: IdentityReadbackGrant(manifest["readback"][client], manifest["owners"][client]) for client in CLIENTS}
        if len({grant.config["provider_account_id"] for grant in grants.values()}) != len(CLIENTS):
            raise ValueError("Independent provider subjects must be distinct")
        readers = {client: grant.reader() for client, grant in grants.items()}
    elif require_readers:
        raise ValueError("Protected execution requires independently admitted MCP readback")
    cleanup = {}
    if "cleanup" in manifest:
        from tests.e2e.tools.atlassian_fixture_provider import JiraFixtureProvider

        if type(manifest["cleanup"]) is not dict or set(manifest["cleanup"]) != CLIENTS:
            raise ValueError("Separate cleanup authority for both clients is required")
        cleanup = {client: JiraFixtureProvider(manifest["cleanup"][client], readback=manifest["readback"][client],
                                              owner_id=manifest["owners"][client], run_id=manifest["run_id"])
                   for client in CLIENTS}
    restored = recovery_roots(root)
    return validate_isolated_fixtures({
        client: {"ledger": ClientOperationLedger(restored.get(client, root / client) / "operations.json", run_id=manifest["run_id"]),
                 "owner_id": manifest["owners"][client], "reader": readers[client],
                 **({"cleanup_provider": cleanup[client]} if client in cleanup else {})}
        for client in sorted(CLIENTS)
    })
