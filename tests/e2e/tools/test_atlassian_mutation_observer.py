"""Controlled response cases for the REST effect reader; not provider acceptance."""

from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.e2e.tools import atlassian_mutation_observer as observer


MARKER = "adp-8454-abcdefghijklmnop"
ACCOUNT = "000000:owner-one"


def body(text):
    return {
        "version": 1,
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def history(
    identifier, *, field="summary", old="initial", new="changed", author=ACCOUNT
):
    return {
        "id": identifier,
        "author": {"accountId": author},
        "items": [
            {
                "fieldId": field,
                "field": field,
                "fromString": old,
                "toString": new,
                "from": old,
                "to": new,
            }
        ],
    }


class Provider:
    def __init__(self):
        self.summary = MARKER + " before"
        self.status = "10000"
        self.rows = []
        self.calls = []
        self.page_size = 1
        self.modify_page = lambda page: page
        self.reads = 0
        self.modify_issue = lambda issue: issue

    def get(self, path):
        self.calls.append(path)
        uri = urlsplit(path)
        assert uri.path.startswith("/ex/jira/site-one/rest/api/3/issue/42")
        if uri.path.endswith("/42"):
            self.reads += 1
            return self.modify_issue(
                {
                    "id": "42",
                    "fields": {"summary": self.summary, "status": {"id": self.status}},
                }
            )
        assert uri.path.endswith(("/changelog", "/comment"))
        query = parse_qs(uri.query)
        assert query["maxResults"] == ["100"]
        start = int(query["startAt"][0])
        collection = "comments" if uri.path.endswith("/comment") else "values"
        page = {
            "startAt": start,
            "maxResults": self.page_size,
            "total": len(self.rows),
            collection: deepcopy(self.rows[start : start + self.page_size]),
        }
        if collection == "values":
            page["isLast"] = start + len(page[collection]) == len(self.rows)
        return self.modify_page(page)


def read(provider, operation="update_jira_issue", **overrides):
    arguments = {
        "site_id": "site-one",
        "issue_id": "42",
        "account_id": ACCOUNT,
        "operation": operation,
        "marker": MARKER,
        **overrides,
    }
    return observer.read_snapshot(provider.get, **arguments)


def perform(provider, operation):
    if operation == "update_jira_issue":
        old = provider.summary
        provider.summary = MARKER + " after"
        provider.rows.append(history("200", old=old, new=provider.summary))
        return {"summary": provider.summary}
    if operation == "transition_jira_issue":
        old = provider.status
        provider.status = "10001"
        provider.rows.append(
            history("200", field="status", old=old, new=provider.status)
        )
        return {"status_id": provider.status}
    document = body(MARKER + " a comment")
    provider.rows.append(
        {"id": "200", "author": {"accountId": ACCOUNT}, "body": document}
    )
    return {"body": document}


@pytest.mark.parametrize("operation", sorted(observer.OPERATIONS))
def test_actual_history_delta_and_current_content_are_both_required(operation):
    provider = Provider()
    if operation == "comment_jira_issue":
        provider.rows = [
            {
                "id": "100",
                "author": {"accountId": "other-author"},
                "body": body("old comment"),
            }
        ]
    else:
        provider.rows = [
            history(
                "100", field="summary" if operation == "update_jira_issue" else "status"
            )
        ]
    before = read(provider, operation)
    expected = perform(provider, operation)
    after = read(provider, operation)
    result = observer.verify_mutation(before, after, expected=expected)
    assert result["effect_count"] == 1 and result["provider_event_id"] == "200"
    assert result["content_sha256"] == observer._digest(expected)
    assert any("startAt=1" in path for path in provider.calls)
    assert result["before_sha256"] != result["after_sha256"]


@pytest.mark.parametrize("operation", sorted(observer.OPERATIONS))
def test_duplicates_are_rejected_even_when_final_content_looks_correct(operation):
    provider = Provider()
    before = read(provider, operation)
    expected = perform(provider, operation)
    duplicate = deepcopy(provider.rows[-1])
    duplicate["id"] = "201"
    provider.rows.append(duplicate)
    with pytest.raises(ValueError, match="Exactly one"):
        observer.verify_mutation(before, read(provider, operation), expected=expected)


@pytest.mark.parametrize("operation", sorted(observer.OPERATIONS))
def test_other_actor_cannot_satisfy_the_clients_mutation(operation):
    provider = Provider()
    before = read(provider, operation)
    expected = perform(provider, operation)
    provider.rows[-1]["author"]["accountId"] = "different-user"
    with pytest.raises(ValueError, match="author"):
        observer.verify_mutation(before, read(provider, operation), expected=expected)


@pytest.mark.parametrize("operation", sorted(observer.OPERATIONS))
def test_right_history_with_wrong_requested_content_is_rejected(operation):
    provider = Provider()
    before = read(provider, operation)
    perform(provider, operation)
    expected = (
        {"body": body(MARKER + " wrong")}
        if operation == "comment_jira_issue"
        else {"summary": MARKER + " wrong"}
        if operation == "update_jira_issue"
        else {"status_id": "999"}
    )
    with pytest.raises(ValueError, match="requested mutation"):
        observer.verify_mutation(before, read(provider, operation), expected=expected)


def test_current_summary_without_history_is_not_an_observed_write():
    provider = Provider()
    before = read(provider)
    provider.summary = MARKER + " after"
    with pytest.raises(ValueError, match="Exactly one"):
        observer.verify_mutation(
            before, read(provider), expected={"summary": provider.summary}
        )


@pytest.mark.parametrize("change", ["delete", "edit"])
def test_deleted_or_rewritten_retained_history_prevents_acceptance(change):
    provider = Provider()
    provider.rows = [history("100")]
    before = read(provider)
    expected = perform(provider, "update_jira_issue")
    if change == "delete":
        provider.rows.pop(0)
    else:
        provider.rows[0]["items"][0]["toString"] = "rewritten"
    with pytest.raises(ValueError, match="removed or changed"):
        observer.verify_mutation(before, read(provider), expected=expected)


@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "offset",
        "boolean-total",
        "total-change",
        "early-last",
        "duplicate",
        "too-large",
    ],
)
def test_incomplete_pagination_is_never_treated_as_zero_effects(fault):
    provider = Provider()
    provider.rows = [history("100"), history("101")]

    def alter(page):
        if fault == "empty":
            page["values"] = []
        elif fault == "offset":
            page["startAt"] += 1
        elif fault == "boolean-total":
            page["total"] = True
        elif fault == "total-change" and page["startAt"]:
            page["total"] += 1
        elif fault == "early-last":
            page["isLast"] = True
        elif fault == "duplicate" and page["startAt"]:
            page["values"][0]["id"] = "100"
        elif fault == "too-large":
            page["total"] = observer.MAX_ITEMS + 1
        return page

    provider.modify_page = alter
    with pytest.raises(ValueError):
        read(provider)


@pytest.mark.parametrize(
    "key,value",
    [
        ("site_id", "other/site"),
        ("issue_id", "../42"),
        ("marker", "someone-elses-marker"),
    ],
)
def test_invalid_targets_are_refused_before_network_access(key, value):
    provider = Provider()
    with pytest.raises(ValueError):
        read(provider, **{key: value})
    assert provider.calls == []


def test_missing_owned_issue_marker_blocks_history_read():
    provider = Provider()
    provider.summary = "a shared issue"
    with pytest.raises(ValueError, match="run-owned"):
        read(provider)
    assert len(provider.calls) == 1


def test_issue_change_during_collection_is_not_a_consistent_snapshot():
    provider = Provider()

    def alter(issue):
        if provider.reads > 1:
            issue["fields"]["summary"] += " concurrent change"
        return issue

    provider.modify_issue = alter
    with pytest.raises(ValueError, match="during observation"):
        read(provider)


@pytest.mark.parametrize("key", ["account_id", "issue_id", "site_id", "marker"])
def test_snapshot_cannot_be_rebound_to_another_user_or_fixture(key):
    provider = Provider()
    before = read(provider)
    expected = perform(provider, "update_jira_issue")
    after = read(provider)
    after["binding"][key] += "other"
    after["sha256"] = observer._digest(
        {k: v for k, v in after.items() if k != "sha256"}
    )
    with pytest.raises(ValueError, match="binding changed"):
        observer.verify_mutation(before, after, expected=expected)


def test_torn_private_snapshot_is_rejected():
    provider = Provider()
    before = read(provider)
    expected = perform(provider, "update_jira_issue")
    after = read(provider)
    before["events"]["invented"] = {}
    with pytest.raises(ValueError, match="retained independent"):
        observer.verify_mutation(before, after, expected=expected)


def test_provider_failure_propagates_without_a_zero_effect_claim():
    def unavailable(path):
        raise OSError("provider unavailable")

    with pytest.raises(OSError):
        observer.read_snapshot(
            unavailable,
            site_id="site-one",
            issue_id="42",
            account_id=ACCOUNT,
            operation="update_jira_issue",
            marker=MARKER,
        )
