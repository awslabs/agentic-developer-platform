"""Read Jira mutation effects through a separately admitted REST read grant.

The caller supplies the repository's authenticated GET transport, persists the
baseline before dispatch, and re-reads after the gateway settles the operation.
This module neither acquires grants nor treats an MCP scope as REST authority.
Only dedicated, run-marked issues are supported. Snapshots remain private.

Response contracts: Jira REST v3 Get changelogs and Get comments:
https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/
https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/
"""

import hashlib
import json
import re
import time
from urllib.parse import quote, urlencode


OPERATIONS = {"update_jira_issue", "comment_jira_issue", "transition_jira_issue"}
HANDLE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ACCOUNT = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")
MARKER = re.compile(r"adp-8454-[a-z0-9]{16,64}\Z")
MAX_ITEMS = 1000


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _identifier(value, pattern=HANDLE):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("An exact Jira observation binding is required")
    return value


def _document(value):
    if (
        type(value) is not dict
        or value.get("type") != "doc"
        or type(value.get("version")) is not int
        or value["version"] != 1
        or not isinstance(value.get("content"), list)
    ):
        raise ValueError("Jira comment content is not an ADF document")
    return value


def _text(value):
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        text = value.get("text", "") if value.get("type") == "text" else ""
        return (
            (text if isinstance(text, str) else "")
            + " "
            + _text(value.get("content", []))
        )
    return ""


def _pages(get, path, collection, deadline):
    """Require complete pagination; a truncated/changed history is not zero effects."""
    rows, identifiers, total = [], set(), None
    while True:
        if time.monotonic() >= deadline:
            raise ValueError("Jira observation deadline exceeded")
        page = get(path + "?" + urlencode({"startAt": len(rows), "maxResults": 100}))
        if (
            type(page) is not dict
            or type(page.get("total")) is not int
            or not 0 <= page["total"] <= MAX_ITEMS
            or type(page.get("startAt")) is not int
            or page["startAt"] != len(rows)
            or not isinstance(page.get(collection), list)
            or len(page[collection]) > 100
            or total is not None
            and total != page["total"]
        ):
            raise ValueError("Jira observation pagination is incomplete or changed")
        total = page["total"]
        entries = page[collection]
        if (
            len(rows) + len(entries) > total
            or not entries
            and len(rows) < total
            or "isLast" in page
            and (
                type(page["isLast"]) is not bool
                or page["isLast"] != (len(rows) + len(entries) == total)
            )
        ):
            raise ValueError("Jira observation page does not establish completeness")
        for entry in entries:
            if type(entry) is not dict:
                raise ValueError("Jira observation contains an invalid event")
            event_id = _identifier(entry.get("id"))
            if event_id in identifiers:
                raise ValueError("Jira observation repeats a provider event")
            identifiers.add(event_id)
            rows.append(entry)
        if len(rows) == total:
            return rows


def read_snapshot(get, *, site_id, issue_id, account_id, operation, marker):
    """GET the marked issue and complete operation-specific history, without writes."""
    for value in (site_id, issue_id):
        _identifier(value)
    _identifier(account_id, ACCOUNT)
    _identifier(marker, MARKER)
    if operation not in OPERATIONS:
        raise ValueError("Unsupported Jira mutation observer")
    deadline = time.monotonic() + 30
    prefix = f"/ex/jira/{quote(site_id, safe='')}/rest/api/3/issue/{quote(issue_id, safe='')}"

    def resource():
        issue = get(prefix + "?fields=summary,status")
        fields = issue.get("fields") if isinstance(issue, dict) else None
        if (
            type(issue) is not dict
            or issue.get("id") != issue_id
            or type(fields) is not dict
            or not isinstance(fields.get("summary"), str)
            or marker not in fields["summary"]
        ):
            raise ValueError(
                "The independently read issue is not the run-owned fixture"
            )
        if operation == "transition_jira_issue":
            status = fields.get("status")
            return {
                "status_id": _identifier(
                    status.get("id") if isinstance(status, dict) else None
                )
            }
        return {"summary": fields["summary"]}

    projection = resource()
    comments = operation == "comment_jira_issue"
    rows = _pages(
        get,
        prefix + ("/comment" if comments else "/changelog"),
        "comments" if comments else "values",
        deadline,
    )
    events = {}
    for row in rows:
        author = row.get("author")
        author_id = author.get("accountId") if isinstance(author, dict) else None
        if comments:
            event = {"author_id": author_id, "body": _document(row.get("body"))}
        else:
            items = row.get("items")
            if not isinstance(items, list) or any(
                type(item) is not dict for item in items
            ):
                raise ValueError("Jira change history is malformed")
            field = "summary" if operation == "update_jira_issue" else "status"
            changes = [
                item
                for item in items
                if item.get("fieldId", item.get("field")) == field
            ]
            if not changes:
                continue
            if len(changes) != 1:
                raise ValueError("A Jira history entry contains ambiguous changes")
            change = changes[0]
            suffix = "String" if field == "summary" else ""
            event = {
                "author_id": author_id,
                "from": change.get("from" + suffix),
                "to": change.get("to" + suffix),
            }
        events[row["id"]] = event
    if resource() != projection or time.monotonic() >= deadline:
        raise ValueError("The Jira fixture changed during observation")
    result = {
        "version": 1,
        "binding": {
            "site_id": site_id,
            "issue_id": issue_id,
            "account_id": account_id,
            "operation": operation,
            "marker": marker,
        },
        "resource": projection,
        "events": events,
    }
    return {**result, "sha256": _digest(result)}


def verify_mutation(before, after, *, expected):
    """Verify one new provider event and exact requested content against the baseline.

    Hashes detect accidental/torn ledger changes; authenticated collection and
    protected artifact provenance are still the caller's responsibility.
    """
    for snapshot in (before, after):
        if (
            type(snapshot) is not dict
            or set(snapshot) != {"version", "binding", "resource", "events", "sha256"}
            or type(snapshot["version"]) is not int
            or snapshot["version"] != 1
            or type(snapshot["binding"]) is not dict
            or set(snapshot["binding"])
            != {"site_id", "issue_id", "account_id", "operation", "marker"}
            or type(snapshot["resource"]) is not dict
            or type(snapshot["events"]) is not dict
            or len(snapshot["events"]) > MAX_ITEMS
            or snapshot["sha256"]
            != _digest(
                {key: value for key, value in snapshot.items() if key != "sha256"}
            )
        ):
            raise ValueError("A retained independent Jira snapshot is required")
    if before["binding"] != after["binding"]:
        raise ValueError("The Jira owner, operation or fixture binding changed")
    binding = before["binding"]
    for key in ("site_id", "issue_id"):
        _identifier(binding[key])
    _identifier(binding["account_id"], ACCOUNT)
    _identifier(binding["marker"], MARKER)
    operation = binding["operation"]
    if operation not in OPERATIONS:
        raise ValueError("Unsupported Jira mutation observer")
    old, new = before["events"], after["events"]
    if any(key not in new or old[key] != new[key] for key in old):
        raise ValueError("Retained Jira history was removed or changed")
    added = {key: value for key, value in new.items() if key not in old}
    if len(added) != 1:
        raise ValueError("Exactly one new provider effect has not been established")
    event_id, event = next(iter(added.items()))
    _identifier(event_id)
    if (
        type(event) is not dict
        or event.get("author_id") != binding["account_id"]
        or type(expected) is not dict
    ):
        raise ValueError("The mutation author or requested content differs")
    if operation == "comment_jira_issue":
        valid = (
            set(expected) == {"body"}
            and set(event) == {"author_id", "body"}
            and _document(expected["body"]) == event["body"]
            and binding["marker"] in _text(expected["body"])
            and before["resource"] == after["resource"]
        )
    else:
        field = "summary" if operation == "update_jira_issue" else "status_id"
        valid = (
            set(expected) == {field}
            and set(before["resource"]) == set(after["resource"]) == {field}
            and set(event) == {"author_id", "from", "to"}
            and isinstance(expected[field], str)
            and event["from"] == before["resource"][field]
            and event["to"] == expected[field]
            and event["from"] != event["to"]
            and after["resource"] == expected
            and (field != "summary" or binding["marker"] in expected[field])
        )
    if not valid:
        raise ValueError("The provider effect does not match the requested mutation")
    return {
        "operation": operation,
        "issue_id": binding["issue_id"],
        "effect_count": 1,
        "provider_event_id": event_id,
        "content_sha256": _digest(expected),
        "before_sha256": before["sha256"],
        "after_sha256": after["sha256"],
    }
