import base64
from html.parser import HTMLParser

from cyber_tools.task_report import render_report


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_offline_report_separates_sources_and_records_actual_steps():
    context = {
        "task_id": "tsk_example",
        "started_at": "2026-09-25T16:00:00Z",
        "inputs": {"url": "https://example.com/<script>alert(1)</script>"},
        "steps": [],
    }

    def step(tool, aid, result, payload=None, status="confirmed"):
        context["steps"].append(
            {
                "tool": tool,
                "artifact": {"artifact_id": aid},
                "operation_status": status,
                "payload": payload or {},
                "result": result,
                "started_at": "2026-09-25T16:00:01Z",
                "finished_at": "2026-09-25T16:00:02Z",
            }
        )

    step(
        "cyber.common_crawl_result",
        "art_archive",
        {
            "captures": [
                {
                    "fetch_time": "2025-01-01",
                    "url": "https://example.com",
                    "fetch_status": "200",
                }
            ]
        },
    )
    step(
        "cyber.browser_inspect",
        "art_browser",
        {
            "image": {
                "media_type": "image/jpeg",
                "data": base64.b64encode(b"\xff\xd8\xfftest").decode(),
            }
        },
        {"section": "screenshot"},
    )
    step("cyber.browser_close", "art_close", {"cleanup_status": "stopped"})
    step("cyber.browser_step", "art_denied", {}, {"action": "navigate"}, "rejected")
    report = {
        "summary": "Verdict: inconclusive. <img src=x onerror=alert(1)>",
        "findings": [
            {"statement": "Historical observation", "evidence_refs": ["art_archive"]},
            {"statement": "Current observation", "evidence_refs": ["art_browser"]},
        ],
        "uncertainties": ["Only one page inspected."],
        "recommendations": [],
        "evidence_refs": [],
    }
    result = render_report(report=report, context=context)
    assert result["content_type"] == "text/html"
    text = result["content"].decode()
    archive = text.split('id="archive"')[1].split("</section>")[0]
    browser = text.split('id="browser"')[1].split("</section>")[0]
    assert "Historical observation" in archive and "Current observation" not in archive
    assert "Current observation" in browser and "Historical observation" not in browser
    assert "2025-01-01" in archive
    assert "browser close" in text and "stopped" in text and "rejected" in text
    assert "This is not confirmed evidence" in text
    assert "&lt;script&gt;" in text and "&lt;img" in text
    parsed = Elements()
    parsed.feed(text)
    assert not any(
        tag in {"script", "iframe", "object", "form", "link"} for tag, _ in parsed.tags
    )
    images = [attrs for tag, attrs in parsed.tags if tag == "img"]
    assert len(images) == 1 and images[0]["src"].startswith("data:image/jpeg;base64,")
    assert all(
        attrs["href"].startswith("#") for tag, attrs in parsed.tags if tag == "a"
    )
    assert "default-src" in text and "Only one page inspected." in text


def test_missing_sources_are_explicit_and_untrusted_image_types_are_never_embedded():
    report = {"summary": "Verdict: inconclusive", "findings": []}
    text = render_report(report=report, context={"inputs": {}, "steps": []})[
        "content"
    ].decode()
    assert text.count("This source was not investigated") == 3
    assert "No tool operations were recorded" in text


def test_verdict_banner_promotes_only_explicit_assessment():
    cases = [
        ("Verdict: malicious. Evidence follows.", "MALICIOUS", "danger"),
        ("Verdict: suspicious with medium confidence.", "SUSPICIOUS", "warning"),
        (
            "Verdict: no malicious behavior observed.",
            "NO MALICIOUS BEHAVIOR OBSERVED",
            "clear",
        ),
        ("Verdict: inconclusive", "INCONCLUSIVE", "neutral"),
        ("No malicious behavior was mentioned.", "VERDICT NOT SPECIFIED", "neutral"),
        ("Verdict: maliciousness unclear", "VERDICT NOT SPECIFIED", "neutral"),
    ]
    for summary, label, tone in cases:
        text = render_report(report={"summary": summary}, context={})[
            "content"
        ].decode()
        assert "<h1>DOMAIN MRI</h1>" in text
        assert f'class="verdict-banner {tone}"' in text
        assert f"<h2>{label}</h2>" in text
        assert summary in text
