import json

import pytest

from common_crawl import CrawlConfig, lookup_common_crawl, query_for
import domain_investigation as cli

CONFIG = CrawlConfig("crawl_db", "ccindex", "crawl-workgroup", ("CC-MAIN-2026-38",))


class Athena:
    def __init__(self, rows=(), state="SUCCEEDED"):
        self.rows, self.state = rows, state
        self.started = []
        self.cancelled = []
        self.cutoff = 1024**3

    def get_work_group(self, **kwargs):
        return {
            "WorkGroup": {
                "State": "ENABLED",
                "Configuration": {
                    "EnforceWorkGroupConfiguration": True,
                    "BytesScannedCutoffPerQuery": self.cutoff,
                    "ResultConfiguration": {
                        "OutputLocation": "s3://test-results/queries/"
                    },
                },
            }
        }

    def start_query_execution(self, **kwargs):
        self.started.append(kwargs)
        return {"QueryExecutionId": "query-test"}

    def get_query_execution(self, **kwargs):
        return {
            "QueryExecution": {
                "Status": {"State": self.state},
                "Statistics": {"DataScannedInBytes": 128},
            }
        }

    def get_query_results(self, **kwargs):
        columns = ["url_host_name", "url", "fetch_time", "fetch_status"]
        return {
            "ResultSet": {
                "ResultSetMetadata": {"ColumnInfo": [{"Name": n} for n in columns]},
                "Rows": [
                    {"Data": [{"VarCharValue": v} for v in row]}
                    for row in [columns, *self.rows]
                ],
            }
        }

    def stop_query_execution(self, **kwargs):
        self.cancelled.append(kwargs)


def test_archival_metadata_is_bounded_sourced_and_redacts_query_secrets():
    client = Athena(
        [
            (
                "login.public.test",
                "https://login.public.test/?token=secret",
                "2026-09-01 00:00:00",
                "200",
            )
        ]
    )
    r = lookup_common_crawl("https://public.test/", config=CONFIG, client=client)
    assert r["found"] and r["status"] == "available"
    assert r["query_id"] == "query-test" and r["bytes_scanned"] == 128
    assert "secret" not in json.dumps(r)
    assert "REDACTED" in r["captures"][0]["url"]
    assert r["verdict_effect"] == "model_assessed"
    q = client.started[0]
    assert "public.test" not in q["QueryString"]
    assert q["ExecutionParameters"] == [
        "'CC-MAIN-2026-38'",
        "'test'",
        "'public.test'",
        "'public.test'",
        "'%.public.test'",
    ]
    assert "LIMIT 30" in q["QueryString"]


def test_no_match_is_distinct_from_query_failure_and_missing_configuration(monkeypatch):
    for name in ("CYBER_CC_DATABASE", "CYBER_CC_WORKGROUP", "CYBER_CC_CRAWLS"):
        monkeypatch.delenv(name, raising=False)
    assert lookup_common_crawl("https://public.test/")["status"] == "skipped"
    r = lookup_common_crawl("https://public.test/", config=CONFIG, client=Athena())
    assert r["status"] == "available" and r["found"] is False
    r = lookup_common_crawl(
        "https://public.test/", config=CONFIG, client=Athena(state="FAILED")
    )
    assert r["status"] == "unavailable" and "found" not in r


def test_timeout_cancels_the_query_and_retains_its_id():
    now = [0]
    client = Athena(state="RUNNING")
    r = lookup_common_crawl(
        "https://public.test/",
        config=CONFIG,
        client=client,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    assert r["status"] == "unavailable" and r["query_id"] == "query-test"
    assert r["query_cleanup"]["cancellation_requested"]
    assert client.cancelled == [{"QueryExecutionId": "query-test"}]


def test_unsafe_or_unbounded_configuration_never_starts_query():
    client = Athena()
    client.cutoff = 2 * 1024**3
    r = lookup_common_crawl("https://public.test/", config=CONFIG, client=client)
    assert r["status"] == "unavailable" and not client.started
    with pytest.raises(ValueError):
        query_for(
            CrawlConfig('db"; DROP TABLE x', "ccindex", "w", CONFIG.crawls),
            "public.test",
        )
    for url in ("https://127.0.0.1/", "https://bad%27.test/", "https://bad_.test/"):
        assert (
            lookup_common_crawl(url, config=CONFIG, client=client)["status"]
            == "unavailable"
        )
    assert not client.started


def test_out_of_scope_result_is_not_used():
    r = lookup_common_crawl(
        "https://public.test/",
        config=CONFIG,
        client=Athena(
            [("evilpublic.test", "https://evilpublic.test/", "2026-09-01", "200")]
        ),
    )
    assert r["status"] == "unavailable" and "captures" not in r


def test_preparation_requires_hypothesis_before_browser_and_preserves_archive_on_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "_lease_path", lambda output: tmp_path / "private.json")
    calls = []

    def lookup(source, url):
        calls.append(source)
        return {"source": "common_crawl_athena", "status": "available", "found": False}

    path = tmp_path / "case"
    c = cli.prepare(path, "https://public.test/", "Investigate", lookup_fn=lookup)
    assert calls == ["common_crawl"] and not c["sessions"] and not c["probes"]
    assert cli.status(c)["next_operation"] == "hypothesize"
    with pytest.raises(ValueError, match="initial hypothesis"):
        cli.browse(path, request=lambda *a: pytest.fail("Premature browser start"))
    cli.hypothesize(
        path,
        {
            "hypothesis": "No archive history in the checked crawl; intent is unknown",
            "source_ids": ["corroboration-001"],
            "limitations": ["No match does not establish safety"],
            "next_question": "What does the landing page present?",
        },
    )

    def failure(*args):
        raise RuntimeError("broker unavailable")

    with pytest.raises(RuntimeError):
        cli.browse(path, request=failure)
    saved = json.loads((path / "case.json").read_text())
    assert saved["initial_hypothesis"]["source_ids"] == ["corroboration-001"]
    assert saved["corroboration"][0]["found"] is False
    assert saved["assessment"]["verdict"] == "inconclusive"
    with pytest.raises(ValueError, match="do not replay"):
        cli.browse(path, request=failure)


def test_preparation_preserves_archive_failure_explanation(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_lease_path", lambda output: tmp_path / "private.json")
    case = cli.prepare(
        tmp_path / "case",
        "https://public.test/",
        "Investigate",
        lookup_fn=lambda *args: {
            "source": "common_crawl_athena",
            "status": "unavailable",
            "reason": "Common Crawl query exceeded its time budget",
        },
    )
    assert case["corroboration"][0]["reason"] == (
        "Common Crawl query exceeded its time budget"
    )
